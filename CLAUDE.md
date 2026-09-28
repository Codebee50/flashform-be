# Flashform backend

Read ../PRD.md before any task. Follow its Defaults & Decisions (§17) instead of asking.

Django 5.2 + DRF + Channels 4, served over ASGI by Daphne, with Celery for background tasks.
Project package is `flashform/` (the PRD calls it `config/`); apps are `accounts`, `rooms`,
`quizzes`, `activities`.

## Running

Everything runs in Docker Compose (`../docker-compose.yml`): `db` (Postgres 16), `redis`
(Redis 7), `backend` (Django via `runserver`, which migrates on start) and `worker` (Celery).
`flashform-be/` is mounted at `/app`, so code edits reload `backend` without a rebuild.
**All commands run from the repo root via `docker compose exec backend ...`**; never use a
host venv.

```bash
cp flashform-be/.env.example flashform-be/.env    # once; see PRD §14
docker compose up                                 # add -d to detach; --build after requirements change
docker compose exec backend python manage.py <command>
docker compose logs -f worker
```

- `DATABASE_URL` points at the `db` service and `REDIS_URL` at `redis` (channels_redis + Redis
  cache). The SQLite / in-memory / LocMem fallbacks exist only for when those are unset (CI).
- Celery broker: `CELERY_BROKER_URL`, default `redis://redis:6379/1`. Tasks go in each app's
  `tasks.py` (autodiscovered) and must be idempotent (`task_acks_late=True`).
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

## Schema export

From the repo root (the container only sees `flashform-be/`, so the schema is written to
`/app/schema.yml` and then moved to `../schema.yml`):

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

All commands run from the repo root against the running compose stack.

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
