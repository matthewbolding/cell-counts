"""GPU scheduling policy (DESIGN.md, "GPU scheduling") and the queue API."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from processing import queue
from processing.models import Job, JobKind, JobStatus, QueueState
from projects.models import Image, ImageStatus, Project

pytestmark = pytest.mark.django_db


@pytest.fixture
def alice(user):
    return user


@pytest.fixture
def bob(make_user):
    return make_user("bob@example.com")


def make_images(owner, n: int, name="P"):
    project = Project.objects.create(owner=owner, name=f"{name}-{uuid.uuid4().hex[:8]}")
    images = []
    for i in range(n):
        sample = project.samples.create(prefix=f"A{i + 1}")
        images.append(Image.objects.create(
            project=project, sample=sample, channel="SNAP", filename=f"A{i + 1}_SNAP.tif", sha256="a" * 64,
            size_bytes=1, width=1000, height=1000, status=ImageStatus.QUEUED))
    return images


def queue_segments(owner, n: int) -> list[Job]:
    return [queue.enqueue(JobKind.SEGMENT, owner, image=img) for img in make_images(owner, n)]


def claim_order(n: int) -> list[Job]:
    out = []
    for _ in range(n):
        job = queue.claim_next("gpu")
        if job is None:
            break
        queue.finish(job)
        out.append(job)
    return out


def test_users_take_turns(alice, bob):
    a = queue_segments(alice, 3)
    b = queue_segments(bob, 2)
    order = claim_order(10)
    assert [j.user_id for j in order] == [alice.id, bob.id, alice.id, bob.id, alice.id]
    assert [j.id for j in order if j.user_id == alice.id] == [j.id for j in a]
    assert [j.id for j in order if j.user_id == bob.id] == [j.id for j in b]


def test_rescans_jump_the_line(alice, bob):
    queue_segments(alice, 2)
    image = make_images(bob, 1, "R")[0]
    rescan = queue.enqueue(JobKind.RESCAN, bob, image=image, params={"diameter": 0, "cellprob_threshold": 0})
    assert claim_order(1)[0].id == rescan.id


def test_paused_user_is_skipped_and_global_pause_stops_everything(alice, bob):
    queue_segments(alice, 2)
    b = queue_segments(bob, 1)
    queue.set_user_paused(alice, True)
    assert [j.id for j in claim_order(5)] == [b[0].id]
    queue.set_user_paused(alice, False)
    queue.set_gpu_paused(True, by=bob)
    assert queue.claim_next("gpu") is None
    queue.set_gpu_paused(False, by=bob)
    assert queue.claim_next("gpu") is not None


def test_reorder_only_moves_own_queued_jobs(alice, bob):
    a = queue_segments(alice, 3)
    b = queue_segments(bob, 1)
    moved = queue.reorder(alice, [str(a[2].id), str(a[0].id), str(b[0].id)])
    assert moved == 2
    alice_order = [j.id for j in claim_order(10) if j.user_id == alice.id]
    assert alice_order == [a[2].id, a[1].id, a[0].id]


def test_preview_matches_claim_order(alice, bob):
    queue_segments(alice, 3)
    queue_segments(bob, 2)
    running, slots = queue.gpu_order()
    assert running is None
    previewed = [s.job.id for s in sorted(slots, key=lambda s: s.ahead)]
    assert previewed == [j.id for j in claim_order(10)]


def test_eta_uses_measured_speed(alice):
    done = queue_segments(alice, 1)[0]
    now = timezone.now()
    Job.objects.filter(pk=done.pk).update(status=JobStatus.DONE, started_at=now - timedelta(seconds=10),
                                          finished_at=now)  # 10 s for 1 MP
    queued = queue_segments(alice, 2)
    _, slots = queue.gpu_order()
    eta = {s.job.id: s.eta_seconds for s in slots}
    assert eta[queued[0].id] == 0 and eta[queued[1].id] == pytest.approx(10)


def test_interrupted_jobs_retry_once_then_fail(alice):
    job = queue_segments(alice, 1)[0]
    claimed = queue.claim_next("gpu")
    assert claimed.id == job.id and claimed.attempts == 1
    assert queue.recover_interrupted("gpu") == (1, 0)
    claimed = queue.claim_next("gpu")
    assert claimed.attempts == 2
    assert queue.recover_interrupted("gpu") == (0, 1)
    job.refresh_from_db()
    assert job.status == JobStatus.ERROR


# --------------------------------------------------------------------------- #
# Queue API
# --------------------------------------------------------------------------- #
def post(client, name, body):
    return client.post(reverse(name), json.dumps(body), content_type="application/json")


def test_overview_hides_other_peoples_jobs(login, alice, bob):
    queue_segments(alice, 2)
    queue_segments(bob, 1)
    overview = login(bob).get(reverse("processing:api_queue")).json()
    assert overview["total_queued"] == 3
    assert len(overview["jobs"]) == 1 and overview["jobs"][0]["mine"]
    people = {p["user"]: p for p in overview["users"]}
    assert people["alice"]["queued"] == 2 and people["bob"]["mine"]


def test_cancel_own_jobs_only_unless_staff(login, alice, bob, admin_user):
    a = queue_segments(alice, 1)
    b = queue_segments(bob, 1)
    assert post(login(bob), "processing:api_queue_cancel", {"job_ids": [str(a[0].id)]}).json() == {"cancelled": 0}
    assert post(login(bob), "processing:api_queue_cancel", {"job_ids": [str(b[0].id)]}).json() == {"cancelled": 1}
    assert Image.objects.get(pk=b[0].image_id).error == "Cancelled"
    assert post(login(admin_user), "processing:api_queue_cancel",
                {"job_ids": [str(a[0].id)]}).json() == {"cancelled": 1}


def test_pause_endpoints(login, alice, admin_user):
    response = post(login(alice), "processing:api_queue_pause", {"paused": True})
    assert response.json() == {"segmenting_paused": True}
    assert post(login(alice), "processing:api_admin_queue_pause", {"paused": True}).status_code == 403
    assert post(login(admin_user), "processing:api_admin_queue_pause", {"paused": True}).status_code == 200
    assert QueueState.get().gpu_paused


def test_queue_page_renders(login, alice):
    queue_segments(alice, 1)
    response = login(alice).get(reverse("processing:queue_page"))
    assert response.status_code == 200
    assert b'id="queue-initial"' in response.content
