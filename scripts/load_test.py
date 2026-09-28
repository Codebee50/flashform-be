#!/usr/bin/env python
"""Load test: N simulated students in one room (PRD §15 "Load test").

Run inside the backend container, against the running server:

    docker compose exec backend python scripts/load_test.py --base-url http://localhost:8000

Phase 1 (class answers a question): a teacher creates a room and a Quick MC question; N
students join within --join-window seconds, each fetching its state and opening its
WebSocket; then every student submits an answer within --submit-window seconds.
Checks: N participants and N responses in teacher-state (each with the choice that student
sent), p95 submit latency under --p95-ms, zero HTTP errors, and the teacher's socket gets
a `responses_updated` for the final version (the throttle's trailing event).

Phase 2 (Wi-Fi blip): every student socket is killed at once and all reconnect at once.
Each refetches its state (its answer must be intact), then the teacher ends the activity
and every reconnected socket must receive `activity_ended`.

Teacher: pass --teacher-email/--teacher-password, or the script creates (or reuses) the
verified teacher load-test@flashform.test directly in the database, with a new random
password each run. That needs the Django settings, i.e. running in the backend container;
no email is sent.

Source IPs: joins are rate-limited per client IP (JOIN_RATE_PER_IP, default 10/min), which
one machine running 50 students would trip at once. Against a loopback server each student
therefore sends from its own address in 127.0.0.0/8, like its own phone. Use
--no-spread-ips to send everything from one address (then raise JOIN_RATE_PER_IP).
The per-room limit (JOIN_RATE_PER_ROOM, default 60/min) still applies: keep --students at
or below it.

Exit status: 0 on PASS, 1 on FAIL, 2 if the test could not be set up.
"""

import argparse
import asyncio
import ipaddress
import json
import math
import os
import random
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, WebSocketException

DEFAULT_TEACHER_EMAIL = "load-test@flashform.test"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1"}
FIRST_STUDENT_IP = ipaddress.IPv4Address("127.1.0.1")
TIMEOUT_S = 10.0
# How long the teacher may wait for `responses_updated` after the last answer: the
# throttle sends at most one event per 500 ms, then a trailing one (PRD §6 rule 6).
TEACHER_EVENT_TIMEOUT_S = 3.0


class SetupError(Exception):
    """The test could not be set up (teacher login, room, activity); nothing was measured."""


# --- Results ----------------------------------------------------------------


@dataclass
class Report:
    http_errors: list[str] = field(default_factory=list)
    ws_errors: list[str] = field(default_factory=list)
    submit_ms: list[float] = field(default_factory=list)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    metrics: list[tuple[str, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))

    def metric(self, name: str, value: str) -> None:
        self.metrics.append((name, value))

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(ok for _, ok, _ in self.checks)

    def print(self) -> None:
        print()
        print("Metrics")
        for name, value in self.metrics:
            print(f"  {name:<34} {value}")
        print()
        print("Checks")
        for name, ok, detail in self.checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
        for title, errors in (("HTTP errors", self.http_errors), ("WebSocket errors", self.ws_errors)):
            if errors:
                print()
                print(f"{title} ({len(errors)}, first 10)")
                for error in errors[:10]:
                    print(f"  {error}")
        print()
        print("RESULT: PASS" if self.passed else "RESULT: FAIL")


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def ms(value: float) -> str:
    return f"{value:.0f} ms"


# --- HTTP -------------------------------------------------------------------


def make_client(base_url: str, local_address: str | None = None) -> httpx.AsyncClient:
    # One client (and so one connection) per simulated device; no silent retries.
    transport = httpx.AsyncHTTPTransport(local_address=local_address, retries=0)
    return httpx.AsyncClient(base_url=f"{base_url}/api", transport=transport, timeout=TIMEOUT_S)


async def call(
    report: Report, client: httpx.AsyncClient, method: str, path: str, *, label: str, **kwargs
) -> tuple[httpx.Response | None, float]:
    """Send a request, recording any failure (network error or status >= 400) as an HTTP
    error. Returns (response or None on failure, latency in ms)."""
    started = time.perf_counter()
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        report.http_errors.append(f"{label}: {type(exc).__name__}: {exc}")
        return None, (time.perf_counter() - started) * 1000
    elapsed_ms = (time.perf_counter() - started) * 1000
    if response.status_code >= 400:
        report.http_errors.append(f"{label}: {response.status_code} {response.text[:200]}")
        return None, elapsed_ms
    return response, elapsed_ms


async def setup_call(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    response = await client.request(method, path, **kwargs)
    if response.status_code >= 400:
        raise SetupError(f"{method} {path} -> {response.status_code} {response.text[:300]}")
    return response.json()


# --- WebSockets -------------------------------------------------------------


class Socket:
    """A notification WebSocket that records every event it receives."""

    def __init__(self, url: str, origin: str, label: str):
        self.url, self.origin, self.label = url, origin, label
        self.events: list[dict] = []
        self.close_code: int | None = None
        self._changed = asyncio.Event()
        self._connection = None
        self._reader: asyncio.Task | None = None

    async def open(self, report: Report) -> bool:
        try:
            self._connection = await connect(
                self.url, origin=self.origin, open_timeout=TIMEOUT_S, ping_interval=None
            )
        except (OSError, TimeoutError, WebSocketException) as exc:
            report.ws_errors.append(f"{self.label}: connect failed: {type(exc).__name__}: {exc}")
            return False
        self._reader = asyncio.create_task(self._read())
        return True

    async def _read(self) -> None:
        try:
            async for raw in self._connection:
                self.events.append(json.loads(raw))
                self._changed.set()
        except ConnectionClosed:
            pass
        finally:
            self.close_code = self._connection.close_code or 1006
            self._changed.set()

    @property
    def is_open(self) -> bool:
        return self._reader is not None and not self._reader.done()

    async def wait_for(self, type: str, timeout: float, min_version: int = 0) -> dict | None:
        """The first `type` event (with version >= min_version), waiting up to `timeout`."""
        deadline = time.monotonic() + timeout
        while True:
            self._changed.clear()
            for event in self.events:
                if event.get("type") == type and event.get("version", 0) >= min_version:
                    return event
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.is_open:
                return None
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                return None

    async def kill(self) -> None:
        """Drop the connection without a closing handshake, like a phone losing Wi-Fi."""
        if self._connection is not None:
            self._connection.transport.abort()
        if self._reader is not None:
            await self._reader

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        if self._reader is not None:
            await self._reader


# --- Participants -----------------------------------------------------------


@dataclass
class Student:
    index: int
    http: httpx.AsyncClient
    token: str | None = None
    participant_id: str | None = None
    question_id: int | None = None
    choice: int | None = None
    socket: Socket | None = None

    @property
    def name(self) -> str:
        return f"Student {self.index + 1:02d}"


@dataclass
class Setup:
    base_url: str
    ws_base: str
    origin: str
    teacher: httpx.AsyncClient
    room: dict
    activity_id: int

    def student_socket(self, student: Student) -> Socket:
        query = urlencode({"token": student.token})
        return Socket(
            f"{self.ws_base}/ws/room/{self.room['code']}/?{query}", self.origin, student.name
        )


def ensure_teacher_in_db(email: str) -> str:
    """Create or reset a verified teacher directly in the database; returns its new
    password. Needs the Django settings, so it only works inside the backend container."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flashform.settings")
    import django

    django.setup()
    from django.contrib.auth import get_user_model

    from accounts.services import mark_email_verified, normalize_email

    email = normalize_email(email)
    user, _ = get_user_model().objects.get_or_create(
        username=email, defaults={"email": email, "first_name": "Load Test"}
    )
    password = secrets.token_urlsafe(24)
    user.set_password(password)
    user.is_active = True
    user.save()
    mark_email_verified(user)
    return password


def ws_base_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, "", "", ""))


def student_addresses(count: int, spread: bool) -> list[str | None]:
    if not spread:
        return [None] * count
    return [str(FIRST_STUDENT_IP + index) for index in range(count)]


# --- Phases -----------------------------------------------------------------


async def set_up(args, report: Report) -> Setup:
    base_url = args.base_url
    teacher = make_client(base_url)
    login = await setup_call(
        teacher,
        "POST",
        "/auth/login",
        json={"email": args.teacher_email, "password": args.teacher_password},
    )
    teacher.headers["Authorization"] = f"Bearer {login['access']}"
    room = await setup_call(
        teacher, "POST", "/rooms", json={"name": f"Load test {time.strftime('%H:%M:%S')}"}
    )
    state = await setup_call(
        teacher,
        "POST",
        f"/rooms/{room['id']}/activities",
        json={"type": "QUICK", "question": {"type": "MC", "prompt": "Load test: pick any option"}},
    )
    print(f"Room {room['code']} (id {room['id']}), activity {state['activity']['id']}")
    return Setup(
        base_url=base_url,
        ws_base=ws_base_url(base_url),
        origin=args.origin,
        teacher=teacher,
        room=room,
        activity_id=state["activity"]["id"],
    )


async def teacher_state(setup: Setup, report: Report) -> dict | None:
    response, _ = await call(
        report,
        setup.teacher,
        "GET",
        f"/activities/{setup.activity_id}/teacher-state",
        label="teacher-state",
    )
    return response.json() if response else None


async def join(setup: Setup, report: Report, student: Student, window: float) -> None:
    """Join, fetch state, open the socket: what a phone does after the student types the
    code and their name."""
    await asyncio.sleep(random.uniform(0, window))
    response, _ = await call(
        report,
        student.http,
        "POST",
        f"/rooms/{setup.room['code']}/join",
        label=f"{student.name} join",
        json={"name": student.name},
    )
    if response is None:
        return
    body = response.json()
    student.token, student.participant_id = body["token"], str(body["participant_id"])
    student.http.headers["X-Participant-Token"] = student.token

    response, _ = await call(
        report, student.http, "GET", "/participant/state", label=f"{student.name} state"
    )
    if response is not None:
        questions = response.json()["questions"]
        student.question_id = questions[0]["id"] if questions else None

    student.socket = setup.student_socket(student)
    await student.socket.open(report)


async def submit(report: Report, student: Student, window: float) -> None:
    await asyncio.sleep(random.uniform(0, window))
    student.choice = random.randrange(4)  # default MC options A–D (PRD §17)
    response, latency_ms = await call(
        report,
        student.http,
        "PUT",
        f"/participant/responses/{student.question_id}",
        label=f"{student.name} submit",
        json={"choice_index": student.choice},
    )
    if response is not None:
        report.submit_ms.append(latency_ms)


async def phase_one(args, setup: Setup, report: Report, students: list[Student]) -> Socket:
    n = len(students)
    teacher_socket = Socket(
        f"{setup.ws_base}/ws/teacher/room/{setup.room['id']}/?"
        + urlencode({"auth": setup.teacher.headers["Authorization"].removeprefix("Bearer ")}),
        setup.origin,
        "teacher",
    )
    if not await teacher_socket.open(report):
        raise SetupError("Teacher WebSocket could not connect: " + report.ws_errors[-1])

    print(f"Phase 1: {n} students join within {args.join_window:g}s…")
    started = time.perf_counter()
    await asyncio.gather(*(join(setup, report, s, args.join_window) for s in students))
    report.metric("join phase (join + state + socket)", f"{time.perf_counter() - started:.2f} s")

    joined = [s for s in students if s.token]
    report.check("all students joined", len(joined) == n, f"{len(joined)}/{n}")
    open_sockets = sum(1 for s in students if s.socket and s.socket.is_open)
    report.check("all student sockets open", open_sockets == n, f"{open_sockets}/{n}")
    ready = [s for s in joined if s.question_id is not None]

    print(f"Phase 1: {len(ready)} students answer within {args.submit_window:g}s…")
    started = time.perf_counter()
    await asyncio.gather(*(submit(report, s, args.submit_window) for s in ready))
    answers_done = time.monotonic()
    report.metric("submit phase", f"{time.perf_counter() - started:.2f} s")

    if report.submit_ms:
        p95 = percentile(report.submit_ms, 95)
        report.metric(
            "submit latency p50 / p95 / max",
            f"{ms(percentile(report.submit_ms, 50))} / {ms(p95)} / {ms(max(report.submit_ms))}",
        )
        report.check(f"p95 submit latency < {args.p95_ms:g} ms", p95 < args.p95_ms, ms(p95))
    else:
        report.check(f"p95 submit latency < {args.p95_ms:g} ms", False, "no successful submits")

    state = await teacher_state(setup, report)
    if state is None:
        report.check("teacher-state readable", False)
        return teacher_socket
    report.check(
        f"{n} participants in teacher-state",
        state["participant_count"] == n,
        str(state["participant_count"]),
    )
    saved = {str(r["participant_id"]): r["choice_index"] for r in state["responses"]}
    sent = {s.participant_id: s.choice for s in joined if s.choice is not None}
    report.check(f"{n} responses in DB", len(saved) == n, str(len(saved)))
    mismatched = [pid for pid, choice in sent.items() if saved.get(pid) != choice]
    report.check(
        "every saved answer matches what was sent",
        not mismatched and len(sent) == n,
        f"{len(mismatched)} missing or different" if mismatched else "",
    )

    # The teacher's live view must catch up: a responses_updated carrying the final version.
    final_version = state["activity"]["version"]
    event = await teacher_socket.wait_for(
        "responses_updated", TEACHER_EVENT_TIMEOUT_S, min_version=final_version
    )
    if event:
        report.metric(
            "teacher notified of last answer", f"≤ {ms((time.monotonic() - answers_done) * 1000)}"
        )
    updates = sum(1 for e in teacher_socket.events if e.get("type") == "responses_updated")
    report.metric("teacher responses_updated events", f"{updates} for {len(report.submit_ms)} answers")
    report.check(
        "teacher got responses_updated for the final version",
        event is not None,
        "" if event else f"none with version >= {final_version} within {TEACHER_EVENT_TIMEOUT_S:g}s",
    )
    return teacher_socket


async def reconnect(setup: Setup, report: Report, student: Student) -> None:
    """Reopen the socket with the same token, as the client does after a drop."""
    student.socket = setup.student_socket(student)
    await student.socket.open(report)


async def phase_two(args, setup: Setup, report: Report, students: list[Student]) -> None:
    connected = [s for s in students if s.socket and s.socket.is_open]
    n = len(connected)
    print(f"Phase 2: killing {n} student sockets at once, then reconnecting them all…")
    await asyncio.gather(*(s.socket.kill() for s in connected))

    started = time.perf_counter()
    await asyncio.gather(*(reconnect(setup, report, s) for s in connected))
    report.metric("reconnect all sockets", ms((time.perf_counter() - started) * 1000))
    reopened = [s for s in connected if s.socket.is_open]
    report.check("all sockets reconnected", len(reopened) == n, f"{len(reopened)}/{n}")

    async def refetch(student: Student) -> bool:
        response, _ = await call(
            report, student.http, "GET", "/participant/state", label=f"{student.name} refetch"
        )
        if response is None:
            return False
        questions = response.json()["questions"]
        saved = questions[0]["response"] if questions else None
        return saved is not None and saved["choice_index"] == student.choice

    intact = sum(await asyncio.gather(*(refetch(s) for s in reopened)))
    report.check("answers intact after reconnect", intact == len(reopened), f"{intact}/{len(reopened)}")

    # Something every student must hear about: the teacher ends the activity.
    started = time.monotonic()
    response, _ = await call(
        report, setup.teacher, "POST", f"/activities/{setup.activity_id}/end", label="end activity"
    )
    if response is None:
        report.check("every reconnected socket got activity_ended", False, "could not end activity")
        return
    events = await asyncio.gather(
        *(s.socket.wait_for("activity_ended", args.event_timeout) for s in reopened)
    )
    received = sum(1 for e in events if e)
    report.metric("activity_ended delivered to all", ms((time.monotonic() - started) * 1000))
    report.check(
        "every reconnected socket got activity_ended",
        received == len(reopened) == n,
        f"{received}/{n} within {args.event_timeout:g}s",
    )


async def run(args, report: Report) -> None:
    setup = await set_up(args, report)
    addresses = student_addresses(args.students, args.spread_ips)
    students = [Student(i, make_client(args.base_url, addresses[i])) for i in range(args.students)]
    teacher_socket = None
    try:
        teacher_socket = await phase_one(args, setup, report, students)
        await phase_two(args, setup, report, students)
    finally:
        report.check("zero HTTP errors", not report.http_errors, str(len(report.http_errors)))
        sockets = [s.socket for s in students if s.socket]
        if teacher_socket:
            sockets.append(teacher_socket)
        await asyncio.gather(*(socket.close() for socket in sockets), return_exceptions=True)
        await asyncio.gather(*(s.http.aclose() for s in students))
        if report.passed and not args.keep:
            await setup.teacher.delete(f"/rooms/{setup.room['id']}")
        else:
            print(f"Kept room {setup.room['code']} (id {setup.room['id']}) for inspection.")
        await setup.teacher.aclose()


# --- CLI --------------------------------------------------------------------


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Simulate a class answering a Quick Question (PRD §15 load test).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", required=True, help="Server root, e.g. http://localhost:8000")
    parser.add_argument("--students", type=int, default=50)
    parser.add_argument("--teacher-email", help=f"Existing verified teacher (default: {DEFAULT_TEACHER_EMAIL}, created)")
    parser.add_argument("--teacher-password")
    parser.add_argument(
        "--origin",
        default=(os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")[0].strip() or "http://localhost:3000"),
        help="WebSocket Origin header; must be in CORS_ALLOWED_ORIGINS (default: its first entry)",
    )
    parser.add_argument("--join-window", type=float, default=3.0, help="Seconds over which students join")
    parser.add_argument("--submit-window", type=float, default=5.0, help="Seconds over which students answer")
    parser.add_argument("--p95-ms", type=float, default=300.0, help="Max p95 submit latency")
    parser.add_argument("--event-timeout", type=float, default=5.0, help="Seconds to wait for WS events")
    parser.add_argument(
        "--spread-ips",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Give each student its own 127.x source address (default: on for loopback servers)",
    )
    parser.add_argument("--keep", action="store_true", help="Keep the room afterwards (always kept on FAIL)")
    parser.add_argument("--seed", type=int, help="Random seed, to replay the same timings")
    args = parser.parse_args(argv)

    args.base_url = args.base_url.rstrip("/")
    host = urlsplit(args.base_url).hostname or ""
    if args.spread_ips is None:
        args.spread_ips = host in LOOPBACK_HOSTS
    if args.spread_ips:
        if host not in LOOPBACK_HOSTS:
            parser.error("--spread-ips only works against a loopback server")
        # Connect over IPv4, so 127.x source addresses can reach it.
        parts = urlsplit(args.base_url)
        args.base_url = urlunsplit(parts._replace(netloc=parts.netloc.replace(host, "127.0.0.1", 1)))
    if bool(args.teacher_email) != bool(args.teacher_password):
        parser.error("pass both --teacher-email and --teacher-password, or neither")
    if args.students < 1:
        parser.error("--students must be at least 1")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.seed is not None:
        random.seed(args.seed)
    if not args.teacher_email:
        args.teacher_email = DEFAULT_TEACHER_EMAIL
        args.teacher_password = ensure_teacher_in_db(DEFAULT_TEACHER_EMAIL)

    print(
        f"Load test: {args.students} students against {args.base_url} "
        f"(origin {args.origin}, {'one source IP per student' if args.spread_ips else 'one source IP'})"
    )
    report = Report()
    try:
        asyncio.run(run(args, report))
    except SetupError as exc:
        print(f"\nSETUP FAILED: {exc}")
        return 2
    report.print()
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
