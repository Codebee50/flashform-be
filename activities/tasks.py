from celery import shared_task

from . import broadcast


@shared_task(name="activities.tasks.send_trailing_event", ignore_result=True)
def send_trailing_event(type: str, activity_id: int) -> None:
    """The trailing event of a throttled burst (see `broadcast.send_throttled`).

    Idempotent: it tells the teacher to refetch, with the activity's current version, so
    running it twice only causes one extra refetch.
    """
    if type not in broadcast.THROTTLED_EVENTS:
        raise ValueError(f"Not a throttled event: {type}")
    broadcast.send_trailing(type, activity_id)
