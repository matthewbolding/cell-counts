"""The GPU Queue page (View > GPU Queue). Rendered once on the server with the
current overview; frontend/src/queue/main.ts keeps it live."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from processing import queue


@login_required
def queue_page(request):
    return render(request, "processing/queue.html", {"overview": queue.overview(request.user)})
