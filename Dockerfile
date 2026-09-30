FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Production installs requirements.txt; docker-compose builds with requirements-dev.txt
# (adds pytest and the load-test clients).
ARG REQUIREMENTS=requirements.txt
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r ${REQUIREMENTS}

RUN useradd --create-home --uid 1000 app
COPY --chown=app:app . .
# Admin static files, served by WhiteNoise. The key only lets settings load here.
RUN DJANGO_SECRET_KEY=collectstatic-only python manage.py collectstatic --noinput
USER app

EXPOSE 8000

# Production server; the host sets PORT (Railway does). docker-compose overrides this with
# `runserver`, which autoreloads. Migrations run separately (railway.json preDeployCommand).
CMD ["sh", "-c", "exec daphne -b 0.0.0.0 -p ${PORT:-8000} flashform.asgi:application"]
