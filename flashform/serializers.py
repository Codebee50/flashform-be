from rest_framework import serializers


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()


class ErrorSerializer(serializers.Serializer):
    """Shape of every error response (see flashform/exceptions.py). Use in @extend_schema."""

    detail = serializers.CharField()
    code = serializers.CharField()
    fields = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        required=False,
        help_text="Per-field validation messages; present only on field validation errors.",
    )
