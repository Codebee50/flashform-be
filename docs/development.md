# Development

How to run the backend locally, configure it, test it, and contribute.

[← Back to README](../README.md)

Everything runs in Docker Compose. There is no host virtualenv. Run every command below
from the repository root (`flashform-be/`).

## Requirements

- Docker with Docker Compose.
- Optional: `make` for `make schema`. On Windows without `make`, use `./schema.ps1` in
  PowerShell.

## 1. Configure

```bash
git clone https://github.com/Codebee50/flashform-be.git
cd flashform-be
cp .env.example .env
```

Compose reads `.env` for both the `backend` and `worker` containers, so the file must
exist. The values in `.env.example` work for local development as they are. `.env` is
listed in `.gitignore`. Never commit it.

### Environment variables

| Variable | Default if unset | What it does |
|---|---|---|
| `DJANGO_SECRET_KEY` | none | Django's signing key (teacher JWTs, email links, admin sessions). Required when `DJANGO_DEBUG` is off; the server refuses to start without it. In debug mode an insecure dev key is used. |
| `DJANGO_DEBUG` | `False` | `True` for local development only. When off, HTTPS redirect, HSTS and secure cookies are turned on. |
| `DATABASE_URL` | SQLite file `db.sqlite3` | PostgreSQL URL. `.env.example` points at the compose `db` service. The SQLite fallback is for CI only. On Railway an empty value stops the server from starting. |
| `REDIS_URL` | in-process | Redis for the WebSocket channel layer, the cache (rate-limit counters) and the notification throttle. Without it, WebSocket messages and rate limits only work inside a single process. Required in production. |
| `CELERY_BROKER_URL` | `redis://redis:6379/1` | Queue for the Celery worker. Keep it on a different Redis database from `REDIS_URL`. |
| `USE_CELERY` | `True` | `False` runs background jobs (emails, delayed teacher notifications) in a thread pool in the web process, so no worker or broker is needed. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated host names the server answers to. |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5000` | Comma-separated frontend origins allowed to call the API. Also the only origins allowed to open WebSockets. `.env.example` sets `http://localhost:3000`. |
| `CSRF_TRUSTED_ORIGINS` | `http://localhost:3000` | Only matters for the Django admin (`/admin/`). |
| `NUM_PROXIES` | `0` | Number of trusted reverse proxies in front of the app, so rate limits see the real client IP. `0` locally, `1` behind one load balancer (Railway). |
| `JOIN_RATE_PER_IP` | `10/min` | Student joins allowed per IP address. A class behind one school network shares an IP, so raise it for real classrooms. |
| `JOIN_RATE_PER_ROOM` | `60/min` | Student joins allowed per room code. |
| `SECURE_SSL_REDIRECT` | `True` | Only read with debug off. Redirect HTTP to HTTPS (`/api/health` is exempt). |
| `SECURE_HSTS_SECONDS` | `2592000` (30 days) | Only read with debug off. HSTS max-age. |
| `APP_NAME` | `Flashform` | Name used in emails. |
| `FRONTEND_URL` | `http://localhost:3000` | Base URL of the links in verification and reset emails. |
| `SUPPORT_EMAIL` | empty | Contact address shown in emails. |
| `BREVO_API_KEY` | empty | Brevo API key. Empty: emails are written to the log instead of sent. |
| `BREVO_FROM_EMAIL` | `no-reply@example.com` | Sender address. Must be a verified sender in Brevo. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | `localhost`, `587`, empty, empty, `True` | Only for the optional SMTP method `EmailService.send_smtp_mail`. Verification and reset emails use Brevo. |
| `RAILWAY_ENVIRONMENT_ID` | not set | Set by Railway itself. Only used to refuse starting with an empty `DATABASE_URL`. |
| `PORT` | `8000` | Port for the production server (Dockerfile command). Compose does not use it. |

## 2. Start the stack

```bash
docker compose up
```

This starts four containers:

- `db`: PostgreSQL 16, with data in the `flashform_pgdata` volume;
- `redis`: Redis 7;
- `backend`: runs migrations, then Django's `runserver` on port 8000;
- `worker`: the Celery worker.

Add `-d` to run in the background. Add `--build` after changing `requirements.txt` or
`requirements-dev.txt`. The repository is mounted at `/app`, so `backend` reloads when you
edit code. The worker does not (see [Background tasks](#background-tasks)).

Once it is up:

- API: http://localhost:8000/api
- Health check: http://localhost:8000/api/health returns `{"status": "ok"}`
- API docs (Swagger UI): http://localhost:8000/api/docs/
- OpenAPI schema: http://localhost:8000/api/schema/
- WebSockets: `ws://localhost:8000/ws/room/{code}/` and
  `ws://localhost:8000/ws/teacher/room/{room_id}/`
- Django admin: http://localhost:8000/admin/

Run any Django command inside the container:

```bash
docker compose exec backend python manage.py <command>
```

### Migrations

`backend` applies migrations every time it starts. To run them by hand, or to create new
ones after changing a model:

```bash
docker compose exec backend python manage.py migrate
```

```bash
docker compose exec backend python manage.py makemigrations
```

### Frontend

The frontend lives in its own repository:
https://github.com/Codebee50/flashform-fe. Its README explains how to run it. Make sure
its origin (by default `http://localhost:3000`) is in `CORS_ALLOWED_ORIGINS`, or its
requests and WebSockets are refused.

## 3. Create a teacher account

Register through the frontend or `POST /api/auth/register`. A new account cannot log in
until its email is verified.

With `BREVO_API_KEY` empty, emails are not sent. The worker prints them, including the
verification or reset link:

```bash
docker compose logs -f worker
```

To skip verification for a local account:

```bash
docker compose exec backend python manage.py verify_teacher you@example.com
```

For the Django admin, create a superuser:

```bash
docker compose exec backend python manage.py createsuperuser
```

## 4. Run the tests

```bash
docker compose exec backend pytest
```

```bash
docker compose exec backend pytest tests/test_security.py -q
```

Tests use `flashform.settings_test` (set in `pytest.ini`) against the compose Postgres.
Celery tasks run inline, the cache and channel layer are in-process (so tests never clear
the dev server's Redis), and Brevo is never called. The suite had 603 passing tests on
2026-09-30.

`tests/test_security.py` checks the whole API: every `/api` route must be declared public
or have an ownership test, every teacher endpoint answers 404 for another teacher's
object and 401 without a login, every text input has a length limit, and nothing a
student can reach reveals a correct answer or a classmate's answer. A new endpoint fails
the suite until it is added there.

Also run these checks before calling a change done:

```bash
docker compose exec backend python manage.py check
```

```bash
docker compose exec backend python manage.py makemigrations --check --dry-run
```

## 5. Regenerate the OpenAPI schema

```bash
make schema
```

On Windows without `make`, in PowerShell:

```powershell
./schema.ps1
```

Both run `manage.py spectacular --validate` in the container and then move the file one
directory up, to `../schema.yml`. This assumes the backend and frontend repositories are
checked out side by side in one parent folder, where the frontend reads the schema.

## Background tasks

Celery runs two kinds of jobs: sending email, and the delayed final notification after a
burst of answers. Tasks live in each app's `tasks.py`. Queue them with
`flashform.background.submit(task, kwargs, countdown=...)`, never with `.delay()` or
`.apply_async()`. That function falls back to a thread in the web process when
`USE_CELERY=False` or when the broker is down.

The worker does not reload on code changes. After editing a task:

```bash
docker compose restart worker
```

```bash
docker compose logs worker
```

To check that the worker is consuming jobs:

```bash
docker compose exec worker celery -A flashform call flashform.ping
```

Then look for `succeeded ... 'pong'` in `docker compose logs worker`.

## Load test

[`scripts/load_test.py`](../scripts/load_test.py) runs the 50-student scenario described
in [architecture.md](architecture.md#load-test) against the running server. It prints
PASS or FAIL and exits with 0 on PASS, 1 on FAIL and 2 if setup failed.

```bash
docker compose exec backend python scripts/load_test.py --base-url http://localhost:8000
```

```bash
docker compose exec backend python scripts/load_test.py --help
```

- Without `--teacher-email` and `--teacher-password`, it creates or reuses the verified
  teacher `load-test@flashform.test` directly in the database, with a new random password
  each run. No email is sent.
- On PASS the test room is deleted. On FAIL it is kept for inspection.
- Against a local server, each simulated student uses its own `127.1.x.x` address, so the
  per-IP join limit is not hit. The per-room limit still applies, so keep `--students` at
  or below `JOIN_RATE_PER_ROOM` (60 by default).

## Deploying

The live instance runs on Railway as one service built from the
[`Dockerfile`](../Dockerfile), plus Railway's PostgreSQL and Redis.
[`railway.json`](../railway.json) runs `python manage.py migrate --noinput` before each
deploy and waits for `GET /api/health`. The image's default command is the production
server:

```bash
daphne -b 0.0.0.0 -p ${PORT:-8000} flashform.asgi:application
```

For production, set at least `DJANGO_SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`,
`ALLOWED_HOSTS`, `CORS_ALLOWED_ORIGINS`, `CSRF_TRUSTED_ORIGINS` and `FRONTEND_URL`, leave
`DJANGO_DEBUG` off, and set `NUM_PROXIES=1` behind Railway's proxy. Set `USE_CELERY=False`
if there is no separate worker service. On Railway, `ALLOWED_HOSTS` must also include
`healthcheck.railway.app`, the host its health check uses. Deploys close open
WebSockets. Clients reconnect on their own, but avoid deploying during a class.

## Contributing

The coding conventions are in [CLAUDE.md](../CLAUDE.md). In short:

- Business rules go in each app's `services.py`. Views stay thin: read input with a
  serializer, call a service, serialize the result.
- Use serializers for every request and response, and `@extend_schema` where the schema
  cannot be inferred.
- Every teacher endpoint filters by owner, so another teacher's object is a 404.
- Errors are always `{"detail": "...", "code": "..."}`.
- API paths have no trailing slash.

A change is done when the tests pass, `check` and `makemigrations --check --dry-run` are
clean, the schema is regenerated, and the worker has been restarted if a task changed.
