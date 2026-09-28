from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .services import get_profile, normalize_email

User = get_user_model()

# The email is stored in `username`, which is limited to 150 characters.
EMAIL_MAX_LENGTH = 150


def password_field():
    return serializers.CharField(
        write_only=True, trim_whitespace=False, max_length=128, style={"input_type": "password"}
    )


class UserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="first_name", read_only=True)
    email_verified = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "name", "email", "email_verified"]
        read_only_fields = fields

    def get_email_verified(self, user) -> bool:
        return get_profile(user).email_verified


class DetailSerializer(serializers.Serializer):
    detail = serializers.CharField()


class RegisterResponseSerializer(DetailSerializer):
    user = UserSerializer()


class RegisterSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    email = serializers.EmailField(max_length=EMAIL_MAX_LENGTH)
    password = password_field()

    def validate_email(self, value):
        return normalize_email(value)

    def validate(self, attrs):
        candidate = User(username=attrs["email"], email=attrs["email"], first_name=attrs["name"])
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})
        return attrs


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)
    password = password_field()


class RefreshSerializer(serializers.Serializer):
    refresh = serializers.CharField()


class TokenPairSerializer(serializers.Serializer):
    access = serializers.CharField()
    refresh = serializers.CharField()


class AuthResponseSerializer(TokenPairSerializer):
    user = UserSerializer()


class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField(max_length=254)


class VerifyEmailSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=512)


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField(max_length=64)
    token = serializers.CharField(max_length=128)
    new_password = password_field()
