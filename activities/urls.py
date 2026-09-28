from django.urls import path

from . import views

urlpatterns = [
    # Teacher
    path("rooms/<int:room_id>/activities", views.ActivityCreateView.as_view(), name="activity-create"),
    path("activities/<int:pk>/teacher-state", views.TeacherStateView.as_view(), name="activity-teacher-state"),
    path("activities/<int:pk>/navigate", views.ActivityNavigateView.as_view(), name="activity-navigate"),
    path("activities/<int:pk>/end", views.ActivityEndView.as_view(), name="activity-end"),
    # Student
    path("rooms/<str:code>/join", views.JoinView.as_view(), name="room-join"),
    path("participant/state", views.ParticipantStateView.as_view(), name="participant-state"),
    path(
        "participant/responses/<int:question_id>",
        views.ParticipantResponseView.as_view(),
        name="participant-response",
    ),
    path("participant/leave", views.ParticipantLeaveView.as_view(), name="participant-leave"),
]
