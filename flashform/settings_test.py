"""Test settings: the dev settings plus inline Celery and in-process Redis stand-ins."""

from .settings import *  # noqa: F403

# Run tasks inline; `.delay()` never touches the broker.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# tests/conftest.py clears the cache around every test; on Redis that is a FLUSHDB,
# which would wipe the running dev server's data. Keep tests in-process.
CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
BROADCAST_REDIS_URL = ""  # in-process broadcast throttle store

# Never reach Brevo from tests, even if a developer's .env has a real key. Tests that
# exercise the Brevo path set a fake key and mock requests.post.
BREVO_API_KEY = ""
FRONTEND_URL = "http://frontend.test"
SUPPORT_EMAIL = "support@flashform.test"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
