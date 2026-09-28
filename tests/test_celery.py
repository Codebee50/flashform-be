from flashform.celery import ping


def test_ping_task_runs_inline():
    assert ping.delay().get() == "pong"
