from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=False)
# Other apps mount routers under /api/ too; none of them should own the /api/ root.
router.include_root_view = False
router.register("rooms", views.RoomViewSet, basename="room")

urlpatterns = [
    path("rooms/<str:code>/public", views.PublicRoomView.as_view(), name="room-public"),
    *router.urls,
]
