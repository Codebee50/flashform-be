"""Production settings (DEBUG off): HTTPS redirect, and the health check that must dodge it."""

import json
import os
import subprocess
import sys

import pytest
from django.test import override_settings

PROD_SETTINGS_PROBE = """
import json
from django.conf import settings
print(json.dumps({
    "ssl_redirect": settings.SECURE_SSL_REDIRECT,
    "exempt": settings.SECURE_REDIRECT_EXEMPT,
    "proxy_header": list(settings.SECURE_PROXY_SSL_HEADER),
    "session_secure": settings.SESSION_COOKIE_SECURE,
    "csrf_secure": settings.CSRF_COOKIE_SECURE,
    "hsts": settings.SECURE_HSTS_SECONDS,
    "middleware": settings.MIDDLEWARE,
}))
"""


def load_prod_settings(**env) -> dict:
    """Import flashform.settings in a fresh interpreter with DEBUG off."""
    environ = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "flashform.settings",
        "DJANGO_DEBUG": "False",
        "DJANGO_SECRET_KEY": "test-only",
        **env,
    }
    out = subprocess.run(
        [sys.executable, "-c", "import django; django.setup();" + PROD_SETTINGS_PROBE],
        env=environ,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def test_debug_off_turns_on_https_settings():
    prod = load_prod_settings()
    assert prod["ssl_redirect"] is True
    assert prod["proxy_header"] == ["HTTP_X_FORWARDED_PROTO", "https"]
    assert prod["session_secure"] is True
    assert prod["csrf_secure"] is True
    assert prod["hsts"] > 0
    assert "whitenoise.middleware.WhiteNoiseMiddleware" in prod["middleware"]


def test_ssl_redirect_can_be_switched_off():
    assert load_prod_settings(SECURE_SSL_REDIRECT="False")["ssl_redirect"] is False


@pytest.mark.django_db
def test_health_check_answers_over_http_when_redirect_is_on(api_client):
    exempt = load_prod_settings()["exempt"]
    with override_settings(SECURE_SSL_REDIRECT=True, SECURE_REDIRECT_EXEMPT=exempt):
        assert api_client.get("/api/health").status_code == 200
        assert api_client.get("/api/rooms").status_code == 301


@pytest.mark.django_db
def test_no_redirect_behind_https_proxy(api_client):
    exempt = load_prod_settings()["exempt"]
    with override_settings(
        SECURE_SSL_REDIRECT=True,
        SECURE_REDIRECT_EXEMPT=exempt,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    ):
        response = api_client.get("/api/rooms", HTTP_X_FORWARDED_PROTO="https")
    assert response.status_code == 401  # reached the view: JWT required
