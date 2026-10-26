"""
api.py — the chunked upload endpoints (DESIGN.md, "Uploads"). The protocol
logic lives in uploads/services.py; these views do access checks and request
plumbing only.

Chunk bodies are read straight off `request` in blocks and never through
`request.body`, so a 32 MB chunk never sits in memory whole and Django's
DATA_UPLOAD_MAX_MEMORY_SIZE (sized for ordinary forms) doesn't apply.
"""

from __future__ import annotations

from django.http import Http404, HttpResponse

from core.api import ApiError, api_view, parse_json
from projects.access import get_image_for, get_project_for, require_role
from projects.api import image_json
from projects.models import Role
from uploads import services
from uploads.models import UploadSession


def _content_length(request) -> int | None:
    try:
        return int(request.META.get("CONTENT_LENGTH") or "")
    except ValueError:
        return None


def _own_session(request, upload_id) -> UploadSession:
    """An open upload session belonging to the caller, who must still have edit
    access to its project (access can be revoked mid-upload)."""
    session = (UploadSession.objects.select_related("project", "project__owner")
               .filter(pk=upload_id, user=request.user, completed_at__isnull=True).first())
    if session is None:
        raise Http404
    require_role(session.project, request.user, Role.EDITOR)
    return session


@api_view(["POST"])
def plan(request, project_id):
    project, _ = get_project_for(request.user, project_id, Role.EDITOR)
    return services.plan(project, parse_json(request).get("files"))


@api_view(["POST"])
def init(request, project_id):
    project, _ = get_project_for(request.user, project_id, Role.EDITOR)
    return services.init(project, request.user, parse_json(request))


@api_view(["PUT"])
def chunk(request, upload_id, index):
    session = _own_session(request, upload_id)
    received = services.write_chunk(session, index, request, _content_length(request))
    return {"received": received, "total_chunks": session.total_chunks}


@api_view(["POST"])
def complete(request, upload_id):
    session = _own_session(request, upload_id)
    # The body is optional: only a JSON body (usually gzipped) carries imported
    # desktop cells; anything else is ignored.
    import_data = None
    if request.content_type == "application/json":
        import_data = services.read_import_body(request, request.META.get("HTTP_CONTENT_ENCODING", ""))
    image, imported = services.complete(session, request.user, import_data)
    return {"image": image_json(image), "imported": imported}


@api_view(["DELETE"])
def abort(request, upload_id):
    services.abort(_own_session(request, upload_id))
    return HttpResponse(status=204)


@api_view(["POST"])
def import_cells(request, image_id):
    """Desktop review data for an image already in the project (see
    services.import_into_existing). Same body as `complete`, plus the file's
    sha256 as recorded in the desktop cellcounts.json."""
    image, _ = get_image_for(request.user, image_id, Role.EDITOR)
    if request.content_type != "application/json":
        raise ApiError(400, "invalid_import", "Send the desktop review data as JSON.")
    data = services.read_import_body(request, request.META.get("HTTP_CONTENT_ENCODING", ""))
    if data is None:
        raise ApiError(400, "invalid_import", "No desktop review data was sent.")
    return services.import_into_existing(image, request.user, data)

