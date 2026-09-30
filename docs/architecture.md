# Architecture

How Flashform's parts fit together, and the design choices that keep it reliable when a
whole class answers at once.

[← Back to README](../README.md)

## The parts

| Part | What it does |
|---|---|
| **Frontend** | A Next.js web app ([separate repo](https://github.com/Codebee50/flashform-fe)). Students and teachers use it in the browser. It talks to the backend over HTTPS and WebSockets. |
| **API** | Django with Django REST Framework. Every read and every write (joining, answering, starting a quiz) is a normal HTTP request that returns JSON. |
| **WebSockets** | Django Channels, in the same server process as the API. The server uses them only to tell browsers "something changed, fetch again". They carry no answers or other data. |
| **Database** | PostgreSQL. The only source of truth. If something is not in the database, it did not happen. |
| **Redis** | Passes WebSocket messages between server processes, holds rate-limit counters, and briefly holds the keys that limit how often the teacher is notified. It is also the queue for the Celery worker. |
| **Celery worker** | Runs background jobs: sending emails, and the delayed "last" notification after a burst of answers. It is optional: with `USE_CELERY=False` the same jobs run in a small thread pool inside the web process. |

The server runs over ASGI with Daphne, so one process serves both HTTP and WebSockets.

## How a student's answer flows

1. The student taps an option. The frontend sends
   `PUT /api/participant/responses/{question_id}` with the student's token in the
   `X-Participant-Token` header.
2. The API finds the student by the hash of the token. In one database transaction it
   locks the activity's row, checks the rules (the activity is running, this question is
   open, the answer is not locked), and saves the answer. There is exactly one answer row
   per student and question, so a second submit updates it instead of adding a new one.
   It grades the answer and increases the activity's version number.
3. The transaction commits and the API answers `200` with the saved answer. The student
   sees that it was saved.
4. Only after the commit, the server sends a `responses_updated` message through Redis to
   the teacher's WebSocket. When 40 answers arrive together, the teacher gets at most one
   message every 500 ms, and a final message is always sent after the burst.
5. The teacher's browser receives the message and fetches the full live view with one
   `GET /api/activities/{id}/teacher-state` call.

Teacher actions (next question, end activity) work the same way. The HTTP request changes
the database, then a message tells students' browsers to fetch their state again.

## Reliability

These choices are implemented and tested:

- **The database is the only source of truth.** WebSocket messages never contain state
  that exists nowhere else. A lost message only makes a screen update later.
- **Every write is a plain HTTP request.** HTTP gives clear status codes and makes
  retrying simple.
- **Answers are safe to retry.** The database allows one answer per student and question,
  and submitting again updates it. Other actions are safe to repeat too: moving to the
  current question, ending an ended activity, finishing twice, or removing a student
  twice changes nothing.
- **Writes to one activity happen one at a time.** Each one locks the activity's row
  first, so a double-tap cannot create two answers and two starts cannot leave two
  activities running. The database also enforces at most one running activity per room.
- **Every change gets a version number.** Messages carry it. A browser that sees a gap in
  the numbers, or that reconnects, fetches the full state again.
- **One request rebuilds any screen.** `GET /api/participant/state` for students and
  `GET /api/activities/{id}/teacher-state` for teachers return everything the screen
  needs. After a page refresh or a dropped connection, the screen is rebuilt from scratch.
- **Messages go out only after the database commits,** so a browser never fetches before
  the change is visible.
- **Teacher notifications are throttled** to one per 500 ms per activity, with a
  guaranteed final one, so a burst of answers does not flood the teacher's screen.
- **Failures in the notification path do not break writes.** If Redis cannot deliver a
  message, the error is logged and the answer is still saved. If the Celery queue is
  down, the background job runs in the web process instead of being dropped.
- **Questions are copied when an activity starts,** so editing or deleting a quiz never
  changes a running activity or a past report.

In the frontend:

- **Automatic reconnection.** A dropped WebSocket reconnects after 1 s, 2 s, 4 s and so
  on, up to 15 s, with ±30% random jitter. This stops 40 phones from reconnecting at the
  same instant after a Wi-Fi blip. A ping every 25 s detects dead connections.
- **Polling fallback.** If the WebSocket has been down for more than 5 seconds, the app
  fetches the state every 3 seconds until it reconnects. The app still works, more
  slowly, on networks that block WebSockets.
- **Answer retries.** A failed save (network error, timeout, server error, rate limit) is
  retried with the same backoff. An answer still waiting to be sent is kept in the
  browser, so closing the tab does not lose it.

### Load test

[`scripts/load_test.py`](../scripts/load_test.py) simulates a class against a running
server. 50 students join within 3 seconds, open WebSockets, and answer a multiple choice
question within 5 seconds. Then every student's WebSocket is closed at once and all of
them reconnect at once. It checks that:

- all 50 students joined and all 50 answers are in the database, each with the choice
  that student sent;
- 95% of answer saves took under 300 ms;
- there were no HTTP errors;
- the teacher was notified of the last answer;
- after reconnecting, every student's answer is intact and every socket still receives
  messages.

A run on the local Docker stack on 2026-09-30 passed every check. The 95th-percentile
save time was 76 ms and all 50 sockets reconnected in about 1 second. See
[development.md](development.md#load-test) to run it.

The test has one known limit. If all 50 students answer at the very same instant
(`--join-window 0 --submit-window 0`, which goes beyond the scenario above), the
300 ms check fails, because saves to one activity happen one at a time.

## Tech stack

Versions are the ranges allowed by [`requirements.txt`](../requirements.txt) and the
images in [`docker-compose.yml`](../docker-compose.yml) and
[`Dockerfile`](../Dockerfile).

| Component | Version |
|---|---|
| Python | 3.12 (`python:3.12-slim` image) |
| Django | >=5.2, <6.0 |
| Django REST Framework | >=3.16, <4.0 |
| djangorestframework-simplejwt (teacher JWTs) | >=5.5, <6.0 |
| drf-spectacular (OpenAPI schema, Swagger UI) | >=0.28, <1.0 |
| django-cors-headers | >=4.7, <5.0 |
| Django Channels | >=4.2, <5.0 |
| channels-redis | >=4.2, <5.0 |
| Daphne (ASGI server) | >=4.1, <5.0 |
| Celery (with Redis) | >=5.5, <6.0 |
| redis (Python client) | >=5.0, <7.0 |
| psycopg (PostgreSQL driver) | >=3.2, <4.0 |
| WhiteNoise (admin static files) | >=6.9, <7.0 |
| requests (Brevo email API) | >=2.32, <3.0 |
| dj-database-url, python-dotenv | >=3.0, <4.0 and >=1.1, <2.0 |
| PostgreSQL | 16 (`postgres:16` image) |
| Redis | 7 (`redis:7` image) |
| pytest, pytest-django, pytest-asyncio | >=8.3, >=4.11, >=1.0 (dev only) |
| httpx, websockets (load test clients) | >=0.28, >=15.0 (dev only) |

The frontend uses Next.js 16, React 19, TypeScript and Tailwind CSS 4.

In production (the live instance on Railway), the image built from the
[`Dockerfile`](../Dockerfile) runs Daphne. [`railway.json`](../railway.json) runs database
migrations before each deploy and waits for `GET /api/health` before sending traffic.

## Project structure

```
flashform-be/
  flashform/        project settings, URL and WebSocket routing, error format, Celery app,
                    background job runner (Celery or threads)
  accounts/         teacher registration, login and JWTs, email verification, password
                    reset, email sending through Brevo
  rooms/            rooms and room codes
  quizzes/          quizzes and their questions, with the validation rules
  activities/       activities, participants, answers, reports and CSV; WebSocket
                    consumers and notifications; all live-class rules are in services.py
  templates/emails/ verification and password reset email templates
  tests/            pytest suite (models, permissions, answer leaks, WebSockets, reports)
  scripts/          load_test.py, the 50-student load test
  docker-compose.yml  local stack: Postgres, Redis, backend, worker
  Dockerfile        production image (Daphne)
  Makefile, schema.ps1  export the OpenAPI schema
```

Business rules live in each app's `services.py`. Views only read the request with a
serializer, call a service function, and return a serialized result.
