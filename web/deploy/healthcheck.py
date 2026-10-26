"""Container healthcheck for `web`: GET /healthz the way nginx would send it.

Django only answers for the configured host and redirects plain-HTTP requests to
HTTPS unless X-Forwarded-Proto says the original request was HTTPS, so both
headers are set here. /healthz returns 503 if the database or the NAS volume is
unavailable, which marks the container unhealthy.
"""

import os
import sys
import urllib.error
import urllib.request

host = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")[0].strip()
request = urllib.request.Request(
    "http://127.0.0.1:8000/healthz", headers={"Host": host, "X-Forwarded-Proto": "https"})
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        sys.exit(0 if response.status == 200 else 1)
except (urllib.error.URLError, OSError) as exc:
    print(f"unhealthy: {exc}", file=sys.stderr)
    sys.exit(1)
