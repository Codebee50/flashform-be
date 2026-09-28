"""
ASGI entrypoint: HTTP goes to Django, WebSockets to Channels (PRD §6, §9).

Served by Daphne; `manage.py runserver` uses it too because "daphne" is first
in INSTALLED_APPS.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flashform.settings")

# Initialise Django before importing anything that touches models.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import OriginValidator  # noqa: E402
from django.conf import settings  # noqa: E402

from activities.ws_auth import JWTAuthMiddleware  # noqa: E402

from .routing import websocket_urlpatterns  # noqa: E402

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        # Only accept sockets opened from the frontend origin(s). The JWT middleware sets
        # scope["user"] from ?auth= (teacher sockets); students use ?token= instead.
        "websocket": OriginValidator(
            JWTAuthMiddleware(URLRouter(websocket_urlpatterns)),
            settings.CORS_ALLOWED_ORIGINS,
        ),
    }
)
