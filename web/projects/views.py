"""
views.py — the server-rendered project pages: the project list (File > Open
Project…), the workspace shell that boots the TypeScript review screen, and
project settings (rename, share, delete).
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Count, OuterRef, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import User
from processing import queue
from processing.models import JobKind
from projects.access import get_project_for, visible_projects
from projects.models import Image, ImageStatus, Membership, Project, Role, Sample


def _with_stats(projects):
    # Each figure is its own subquery: annotating several aggregates across the
    # samples and images joins at once multiplies sums by the join's fan-out.
    images = Image.objects.filter(project=OuterRef("pk")).order_by().values("project")
    return projects.select_related("owner").annotate(
        n_samples=Coalesce(Subquery(Sample.objects.filter(project=OuterRef("pk")).order_by().values("project")
                                    .annotate(n=Count("pk")).values("n")), 0),
        n_images=Coalesce(Subquery(images.annotate(n=Count("pk")).values("n")), 0),
        n_done=Coalesce(Subquery(images.filter(status=ImageStatus.DONE).annotate(n=Count("pk")).values("n")), 0),
        size_bytes=Coalesce(Subquery(images.annotate(total=Sum("size_bytes")).values("total")), 0),
    ).order_by("-updated_at")


@login_required
def project_list(request):
    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        if not name:
            messages.error(request, "Give the project a name.")
        elif len(name) > 120:
            messages.error(request, "Project names can be up to 120 characters.")
        else:
            try:
                with transaction.atomic():
                    project = Project.objects.create(owner=request.user, name=name)
            except IntegrityError:
                messages.error(request, f"You already have a project called “{name}”.")
            else:
                return redirect("projects:workspace", project_id=project.id)

    mine = _with_stats(Project.objects.filter(owner=request.user))
    shared = _with_stats(visible_projects(request.user).exclude(owner=request.user))
    roles = dict(Membership.objects.filter(user=request.user).values_list("project_id", "role"))
    for p in shared:
        p.my_role = roles.get(p.id)
    return render(request, "projects/list.html", {"mine": mine, "shared": shared})


@login_required
def workspace(request, project_id):
    project, role = get_project_for(request.user, project_id)
    return render(request, "projects/workspace.html", {"project": project, "role": role})


@login_required
def project_settings(request, project_id):
    project, role = get_project_for(request.user, project_id)
    members = project.memberships.select_related("user").order_by("user__email")
    candidates = (User.objects.filter(is_active=True, approved_at__isnull=False)
                  .exclude(pk=project.owner_id).exclude(memberships__project=project).order_by("email"))
    return render(request, "projects/settings.html", {
        "project": project,
        "role": role,
        "is_owner": role == Role.OWNER,
        "members": members,
        "candidates": candidates,
    })


def _owned(request, project_id) -> Project:
    project, role = get_project_for(request.user, project_id)
    if role != Role.OWNER:
        raise Http404
    return project


@login_required
@require_POST
def rename(request, project_id):
    project = _owned(request, project_id)
    name = request.POST.get("name", "").strip()
    if not name or len(name) > 120:
        messages.error(request, "Project names must be 1–120 characters.")
    else:
        try:
            with transaction.atomic():
                project.name = name
                project.save(update_fields=["name", "updated_at"])
            messages.success(request, "Project renamed.")
        except IntegrityError:
            messages.error(request, f"You already have a project called “{name}”.")
    return redirect("projects:settings", project_id=project.id)


@login_required
@require_POST
def share(request, project_id):
    project = _owned(request, project_id)
    role = request.POST.get("role")
    if role not in (Role.EDITOR, Role.VIEWER):
        messages.error(request, "Pick Editor or Viewer.")
        return redirect("projects:settings", project_id=project.id)
    user = User.objects.filter(pk=request.POST.get("user_id"), is_active=True,
                               approved_at__isnull=False).exclude(pk=project.owner_id).first()
    if user is None:
        messages.error(request, "Pick someone to share with.")
    else:
        Membership.objects.update_or_create(project=project, user=user,
                                            defaults={"role": role, "added_by": request.user})
        messages.success(request, f"Shared with {user.label} as {Role(role).label.lower()}.")
    return redirect("projects:settings", project_id=project.id)


@login_required
@require_POST
def unshare(request, project_id, membership_id):
    project, role = get_project_for(request.user, project_id)
    membership = get_object_or_404(Membership, pk=membership_id, project=project)
    # The owner can remove anyone; a collaborator can only remove themselves.
    if role != Role.OWNER and membership.user_id != request.user.id:
        raise Http404
    leaving = membership.user_id == request.user.id
    membership.delete()
    if leaving:
        messages.success(request, f"You've left “{project.name}”.")
        return redirect("projects:list")
    messages.success(request, f"{membership.user.label} no longer has access.")
    return redirect("projects:settings", project_id=project.id)


@login_required
@require_POST
def delete(request, project_id):
    project = _owned(request, project_id)
    if request.POST.get("confirm_name", "").strip() != project.name:
        messages.error(request, "Type the project's name exactly to delete it.")
        return redirect("projects:settings", project_id=project.id)
    with transaction.atomic():
        image_ids = [str(i) for i in project.images.values_list("id", flat=True)]
        name, pid = project.name, str(project.id)
        project.delete()
        # Files are removed by the CPU worker; NFS deletes of thousands of tiles
        # can outlast a web request.
        queue.enqueue(JobKind.PURGE, request.user, params={"project_id": pid, "image_ids": image_ids})
    messages.success(request, f"Deleted “{name}”.")
    return redirect("projects:list")
