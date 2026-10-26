from django.db import connection
from django.http import JsonResponse

from core.storage import volume_available


def healthz(request):
    """Liveness for Docker/nginx. Unauthenticated, so it reports nothing beyond
    up/down for the database and the data volume."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception:  # noqa: BLE001 — any failure just means "not healthy"
        db_ok = False
    storage_ok = volume_available()
    status = 200 if db_ok and storage_ok else 503
    return JsonResponse({"database": db_ok, "storage": storage_ok}, status=status)
