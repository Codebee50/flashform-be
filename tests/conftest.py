import re
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient


@pytest.fixture(autouse=True)
def _isolate_tests(settings):
    # Throttle counters live in the cache; don't let them leak between tests.
    cache.clear()
    # Real password hashing is deliberately slow; tests don't need that.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    yield
    cache.clear()


@pytest.fixture
def api_client():
    return APIClient()


TEACHER_PASSWORD = "correct-horse-battery"


@pytest.fixture
def unverified_teacher(db, django_user_model):
    return django_user_model.objects.create_user(
        username="ada@example.com",
        email="ada@example.com",
        password=TEACHER_PASSWORD,
        first_name="Ada Lovelace",
    )


@pytest.fixture
def teacher(unverified_teacher):
    """A teacher who has verified their email, so they can log in."""
    from accounts.services import mark_email_verified

    mark_email_verified(unverified_teacher)
    return unverified_teacher


@pytest.fixture
def teacher_client(teacher):
    """An APIClient authenticated as `teacher` with a real JWT access token."""
    from rest_framework_simplejwt.tokens import AccessToken

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(teacher)}")
    return client


# --- Email ------------------------------------------------------------------


class SentEmails:
    """Emails handed to a mocked Brevo API (requests.post)."""

    def __init__(self, post: mock.Mock):
        self.post = post

    @property
    def payloads(self) -> list[dict]:
        return [call.kwargs["json"] for call in self.post.call_args_list]

    def __len__(self) -> int:
        return len(self.payloads)

    def last_link(self) -> tuple[str, dict[str, str]]:
        """(path, query params) of the frontend link in the most recent email."""
        match = re.search(r"http://frontend\.test/\S+", self.payloads[-1]["textContent"])
        assert match, "no frontend link in the email"
        url = urlsplit(match.group())
        return url.path, {key: values[0] for key, values in parse_qs(url.query).items()}


@pytest.fixture
def brevo(settings):
    """Route email through a mocked Brevo API that accepts everything. Never hits the network."""
    settings.BREVO_API_KEY = "test-brevo-key"
    with mock.patch(
        "accounts.email_service.requests.post",
        return_value=mock.Mock(status_code=201, text='{"messageId": "<test@brevo>"}'),
    ) as post:
        yield SentEmails(post)


@pytest.fixture
def post_committed(api_client, django_capture_on_commit_callbacks):
    """POST JSON and run on_commit callbacks (i.e. queue emails) as a real commit would.

    Tests run inside a transaction that never commits, so without this nothing is queued.
    """

    def post(url, data, client=None):
        with django_capture_on_commit_callbacks(execute=True):
            return (client or api_client).post(url, data, format="json")

    return post


def is_verified(user) -> bool:
    """Fresh from the DB: `user.profile` may be a stale cached instance."""
    from accounts.models import TeacherProfile

    return TeacherProfile.objects.get(user=user).email_verified
