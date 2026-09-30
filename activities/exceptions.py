from rest_framework import status
from rest_framework.exceptions import APIException


class RoomLocked(APIException):
    status_code = status.HTTP_423_LOCKED
    default_detail = "Room is locked."
    default_code = "room_locked"


class NoLiveActivity(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "No activity running yet. Waiting for your teacher…"
    default_code = "no_live_activity"


class ActivityEnded(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This activity has ended."
    default_code = "activity_ended"


class ActivityLive(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This activity is still running. End it first."
    default_code = "activity_live"


class NotCurrentQuestion(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This question is not open for answers right now."
    default_code = "not_current_question"


class AlreadyFinished(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "You have already finished this activity."
    default_code = "already_finished"


class ResponseLocked(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "This answer is locked and can no longer be changed."
    default_code = "response_locked"


class NotTeacherPaced(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Only teacher-paced activities can be navigated."
    default_code = "not_teacher_paced"


class NotStudentPaced(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Only student-paced activities can be finished."
    default_code = "not_student_paced"


class NotShortAnswer(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "Only short answer quick questions can be turned into a vote."
    default_code = "not_short_answer"


class NotEnoughAnswers(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "A vote needs at least 2 different answers."
    default_code = "not_enough_answers"


# Not AuthenticationFailed: student views have no authentication classes, and DRF turns
# AuthenticationFailed into a 403 there. We want 401.
class InvalidParticipantToken(APIException):
    status_code = status.HTTP_401_UNAUTHORIZED
    default_detail = "Missing or invalid participant token. Join the room again."
    default_code = "invalid_participant_token"

