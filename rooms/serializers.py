from rest_framework import serializers

from activities.models import ActivityMode, ActivityType

from .models import Room


class LiveActivitySummarySerializer(serializers.Serializer):
    """The room's LIVE activity in the teacher's room list (PRD R2)."""

    id = serializers.IntegerField()
    type = serializers.ChoiceField(choices=ActivityType.choices)
    mode = serializers.ChoiceField(choices=ActivityMode.choices)
    quiz_title = serializers.CharField(allow_blank=True)
    started_at = serializers.DateTimeField()


class RoomSerializer(serializers.ModelSerializer):
    live_activity = LiveActivitySummarySerializer(
        read_only=True,
        allow_null=True,
        help_text="The LIVE activity, or null when nothing is running.",
    )

    class Meta:
        model = Room
        fields = ["id", "name", "code", "is_locked", "live_activity", "created_at", "updated_at"]
        read_only_fields = fields


class RoomCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    code = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        help_text="Optional custom code: 4–10 characters A–Z 0–9, stored uppercase, unique "
        "case-insensitively. Omit it (or send null or \"\") to get a generated 6-character code.",
    )


class RoomUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=False)
    is_locked = serializers.BooleanField(required=False)


class PublicRoomSerializer(serializers.ModelSerializer):
    live_activity_id = serializers.IntegerField(
        source="live_activity.id",
        read_only=True,
        allow_null=True,
        help_text="The LIVE activity's id, or null while students should wait.",
    )

    class Meta:
        model = Room
        fields = ["code", "name", "is_locked", "live_activity_id"]
        read_only_fields = fields
