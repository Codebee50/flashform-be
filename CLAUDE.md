# Flashform backend

Read ../PRD.md before any task. Follow its Defaults & Decisions (§17) instead of asking.

Django 5.2 + DRF + Channels 4, served over ASGI by Daphne, with Celery for background tasks.
Project package is `flashform/` (the PRD calls it `config/`); apps are `accounts`, `rooms`,
`quizzes`, `activities`.

## Running

Everything runs in Docker Compose (`docker-compose.yml` in this directory): `db` (Postgres
16), `redis` (Redis 7), `backend` (Django via `runserver`, which migrates on start) and
`worker` (Celery). This directory is mounted at `/app`, so code edits reload `backend`
without a rebuild. **All commands run from this directory (`flashform-be/`) via
`docker compose exec backend ...`**; never use a host venv.

```bash
cp .env.example .env                              # once; see PRD §14
docker compose up                                 # add -d to detach; --build after requirements change
docker compose exec backend python manage.py <command>
docker compose logs -f worker
```

- `DATABASE_URL` points at the `db` service and `REDIS_URL` at `redis` (channels_redis + Redis
  cache). The SQLite / in-memory / LocMem fallbacks exist only for when those are unset (CI).
- Celery broker: `CELERY_BROKER_URL`, default `redis://redis:6379/1`. Tasks go in each app's
  `tasks.py` (autodiscovered) and must be idempotent (`task_acks_late=True`).
- **Queue tasks with `flashform.background.submit(task, kwargs, countdown=...)`, never
  `.delay()` / `.apply_async()`.** With `USE_CELERY=False` (no worker, e.g. a single Railway
  service), or when the broker is down, it runs them in a thread pool in the web process
  with the task's own retry policy (`autoretry_for`, `max_retries`, `retry_backoff`...).
  Tests: the `no_celery` fixture switches it off and runs that work inline.
- **After editing Celery tasks, `docker compose restart worker`**: the worker does not autoreload.
- Smoke-test the worker: `docker compose exec worker celery -A flashform call flashform.ping`,
  then look for `succeeded ... 'pong'` in `docker compose logs worker`.
- Email: send through `accounts.email_service.EmailService` (Celery, after commit). With
  `BREVO_API_KEY` empty, emails are logged in `docker compose logs worker` instead of sent.
  Tests never hit Brevo (use the `brevo` fixture). Skip verification locally with
  `docker compose exec backend python manage.py verify_teacher <email>`.
- API docs: http://localhost:8000/api/docs/ (Swagger UI), schema at `/api/schema/`.
- Health check: `GET /api/health` → `{"status": "ok"}`.

## Tests

```bash
docker compose exec backend pytest                         # all tests, against Postgres
docker compose exec backend pytest tests/test_health.py -q
```

Tests use `flashform.settings_test` (set in pytest.ini): Celery tasks run inline
(`CELERY_TASK_ALWAYS_EAGER`), and the cache/channel layer are in-process so tests never
flush the dev server's Redis. Use pytest-django (`db` fixture / `@pytest.mark.django_db`)
and the `api_client` fixture in `tests/conftest.py`. Async/Channels tests use
pytest-asyncio (`@pytest.mark.asyncio`) and `channels.testing.WebsocketCommunicator`.

## Load test

`scripts/load_test.py` (PRD §15) simulates a class against the running server: 50 students
join within 3 s, open WebSockets and answer a Quick MC question within 5 s. It then kills all
their sockets at once, reconnects them, and checks that each one still gets events. It prints
PASS/FAIL and exits 0 on PASS, 1 on FAIL and 2 if setup failed.

```bash
docker compose exec backend python scripts/load_test.py --base-url http://localhost:8000
docker compose exec backend python scripts/load_test.py --help   # --students, windows, --p95-ms...
```

- Without `--teacher-email/--teacher-password`, it creates or reuses the verified teacher
  `load-test@flashform.test` directly in the DB, with a new random password each run and no
  email sent. On PASS the test room is deleted; on FAIL it is kept for inspection.
- Against a loopback server, each student sends from its own `127.1.x.x` address so the
  per-IP join limit (`JOIN_RATE_PER_IP`, 10/min) is not hit. The per-room limit
  (`JOIN_RATE_PER_ROOM`, 60/min) still applies, so keep `--students` at or below it.
- The WebSocket `Origin` defaults to the first `CORS_ALLOWED_ORIGINS` entry.
- `--join-window 0 --submit-window 0` (everyone at the same instant) goes beyond the PRD
  scenario and fails the p95 check: submits serialize on the activity row lock.

## Schema export

The schema lives at the repo root (`../schema.yml`), next to the frontend. The container
only sees this directory, so it is written to `/app/schema.yml` and then moved up:

```bash
make schema            # or, on Windows without make: ./schema.ps1
```

## Conventions

- **Business logic goes in `services.py`; views stay thin.** Views parse input with a
  serializer, call a service function, and serialize the result. Consumers and views never
  contain business rules (PRD §13). Broadcasting goes through `activities/broadcast.py`,
  after commit (`transaction.on_commit`).
- **Serializers for every request AND response.** No hand-built dicts returned from views.
- **`@extend_schema` wherever inference is not enough**: APIViews, custom actions, non-200
  status codes, error responses (use `flashform.serializers.ErrorSerializer`), query params,
  and the `X-Participant-Token` header on student endpoints.
- **Every teacher endpoint checks ownership.** Filter querysets by `owner=request.user`
  (or `room__owner=request.user`) so another teacher's object returns **404**, not 403.
- **Auth defaults:** JWT (simplejwt) plus `IsAuthenticated` for every view. Public endpoints
  (health, student endpoints) must set `permission_classes = [AllowAny]` explicitly, and
  `authentication_classes = []` when a JWT should never be required.
- **Errors:** always `{"detail": "...", "code": "..."}` (see `flashform/exceptions.py`);
  field validation errors also carry `fields`. Raise DRF exceptions with a specific `code`
  (e.g. `ValidationError("Room is locked.", code="room_locked")` or a custom
  `APIException` subclass for 409/423) instead of building error responses by hand.
- **URLs:** API paths have no trailing slash (`/api/rooms/{id}`), matching PRD §8. Use
  `DefaultRouter(trailing_slash=False)` for routers.
- **JSON only:** the only renderer and parser is JSON. Views that return CSV set their own
  renderer.
- **Store UTC** (`USE_TZ = True`).

## Definition of done (every task)

All commands run from `flashform-be/` against the running compose stack.

1. Tests written and passing (`docker compose exec backend pytest`).
2. `docker compose exec backend python manage.py check` is clean, and
   `docker compose exec backend python manage.py makemigrations --check --dry-run` shows no
   missing migrations.
3. Schema regenerated: `make schema` (or `./schema.ps1`), which updates `../schema.yml`.
4. If Celery tasks changed: `docker compose restart worker` and confirm it starts cleanly in
   `docker compose logs worker`.
5. Any notes for the frontend that the schema does not capture (WebSocket events, header
   usage, retry/idempotency semantics, error codes to handle) written to
   `../docs/api/<feature>.md`.
