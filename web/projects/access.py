"""
access.py — one place that answers "may this user do this to this project?".

Every view and API endpoint that touches a project, sample or image goes through
one of the `get_*_for` helpers, which 404 (never 403) when the user has no role
on the project, so a guessed UUID doesn't even confirm that the project exists.
"""

from __future__ import annotations

from django.db.models import Q
from django.http import Http404

from core.api import ApiError
from projects.models import ROLE_RANK, Image, Project, Role, Sample


def visible_projects(user):
    return Project.objects.filter(Q(owner=user) | Q(memberships__user=user)).distinct()


def require_role(project: Project, user, minimum: str) -> str:
    role = project.role_for(user)
    if role is None:
        raise Http404
    if ROLE_RANK[role] < ROLE_RANK[minimum]:
        raise ApiError(403, "forbidden", f"This needs {minimum} access to the project.")
    return role


def get_project_for(user, project_id, minimum: str = Role.VIEWER) -> tuple[Project, str]:
    try:
        project = Project.objects.select_related("owner").get(pk=project_id)
    except (Project.DoesNotExist, ValueError) as exc:
        raise Http404 from exc
    return project, require_role(project, user, minimum)


def get_image_for(user, image_id, minimum: str = Role.VIEWER) -> tuple[Image, str]:
    try:
        image = Image.objects.select_related("project", "sample").get(pk=image_id)
    except (Image.DoesNotExist, ValueError) as exc:
        raise Http404 from exc
    return image, require_role(image.project, user, minimum)


def get_sample_for(user, sample_id, minimum: str = Role.VIEWER) -> tuple[Sample, str]:
    try:
        sample = Sample.objects.select_related("project").get(pk=sample_id)
    except (Sample.DoesNotExist, ValueError) as exc:
        raise Http404 from exc
    return sample, require_role(sample.project, user, minimum)
