from django.http import Http404
from rest_framework import exceptions

from flashform.exceptions import api_exception_handler


def handle(exc):
    return api_exception_handler(exc, {})


def test_simple_api_exception():
    response = handle(exceptions.NotAuthenticated())

    assert response.status_code == 401
    assert response.data == {
        "detail": "Authentication credentials were not provided.",
        "code": "not_authenticated",
    }


def test_django_404_is_normalised():
    response = handle(Http404("Room not found."))

    assert response.status_code == 404
    assert response.data == {"detail": "Room not found.", "code": "not_found"}


def test_field_validation_error_includes_fields():
    response = handle(exceptions.ValidationError({"email": ["This field is required."]}))

    assert response.status_code == 400
    assert response.data == {
        "detail": "email: This field is required.",
        "code": "validation_error",
        "fields": {"email": ["This field is required."]},
    }


def test_non_field_validation_error_keeps_its_code():
    response = handle(exceptions.ValidationError("Room is locked.", code="room_locked"))

    assert response.data == {"detail": "Room is locked.", "code": "room_locked"}


def test_unhandled_exception_is_json_500():
    response = handle(RuntimeError("boom"))

    assert response.status_code == 500
    assert response.data == {"detail": "Internal server error.", "code": "server_error"}
