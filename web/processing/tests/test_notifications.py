"""Opt-in processing emails: nothing goes out unless the preference is on."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from django.core import mail
from django.utils import timezone

from accounts.models import NotificationPreferences
from processing import notifications, queue
from processing.models import Job, JobKind, JobStatus
from processing.tests.test_scheduler import queue_segments

pytestmark = pytest.mark.django_db


def finish_all(user, status=JobStatus.DONE):
    for job in Job.objects.filter(user=user, status=JobStatus.QUEUED):
        job.status = status
        job.finished_at = timezone.now()
        job.save()


def test_nothing_is_sent_by_default(user):
    queue_segments(user, 2)
    finish_all(user, JobStatus.ERROR)
    notifications.send_pending()
    notifications.send_daily_summaries(now=timezone.now().replace(hour=12))
    assert mail.outbox == []
    # …and old jobs are marked covered, so turning a preference on later
    # doesn't email a backlog.
    NotificationPreferences.objects.filter(user=user).update(job_failures=True, batch_finished=True)
    notifications.send_pending()
    assert mail.outbox == []


def test_failure_email_groups_by_project(user):
    prefs = NotificationPreferences.for_user(user)
    prefs.job_failures = True
    prefs.save()
    jobs = queue_segments(user, 3)
    for job in jobs[:2]:
        job.image.error = "The GPU ran out of memory on this image."
        job.image.save()
        queue.fail(job, "boom")
    notifications.send_pending()
    assert len(mail.outbox) == 1
    message = mail.outbox[0]
    assert "2 images failed" in message.subject
    assert "A1_SNAP.tif: The GPU ran out of memory" in message.body
    notifications.send_pending()
    assert len(mail.outbox) == 1


def test_batch_email_waits_for_the_last_job(user):
    prefs = NotificationPreferences.for_user(user)
    prefs.batch_finished = True
    prefs.save()
    jobs = queue_segments(user, 2)
    queue.finish(jobs[0])
    notifications.send_pending()
    assert mail.outbox == []  # one still queued
    queue.finish(jobs[1])
    notifications.send_pending()
    assert len(mail.outbox) == 1 and "Segmented: 2 images" in mail.outbox[0].body


def test_daily_summary_once_a_day_after_eight(user):
    prefs = NotificationPreferences.for_user(user)
    prefs.daily_summary = True
    prefs.save()
    tz = timezone.get_current_timezone()
    morning = timezone.make_aware(datetime.combine(timezone.localdate(), datetime.min.time()), tz)
    queue_segments(user, 1)
    finish_all(user)
    Job.objects.update(finished_at=morning + timedelta(hours=6))
    assert notifications.send_daily_summaries(now=morning + timedelta(hours=7)) == 0
    assert notifications.send_daily_summaries(now=morning + timedelta(hours=9)) == 1
    assert notifications.send_daily_summaries(now=morning + timedelta(hours=10)) == 0
    assert "Segmented: 1" in mail.outbox[0].body


def test_rescans_and_exports_never_email(user):
    prefs = NotificationPreferences.for_user(user)
    prefs.job_failures = prefs.batch_finished = True
    prefs.save()
    image = queue_segments(user, 1)[0].image
    Job.objects.all().delete()
    rescan = queue.enqueue(JobKind.RESCAN, user, image=image)
    queue.fail(rescan, "boom")
    notifications.send_pending()
    assert mail.outbox == []
