"""gunicorn settings for the `web` container (see docker-compose.yml).

Threads matter more than processes here: most request time is I/O (streaming
32 MB upload chunks to the NAS, Postgres), and a chunk upload holds its thread
for as long as the browser takes to send it.
"""

import os

bind = "0.0.0.0:8000"
workers = int(os.environ.get("GUNICORN_WORKERS", "4"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))
worker_class = "gthread"
timeout = 120          # Cloudflare gives up at ~100 s anyway
graceful_timeout = 30
keepalive = 5
max_requests = 2000    # recycle workers now and then; bounds any slow leak
max_requests_jitter = 200
worker_tmp_dir = "/dev/shm"  # the root filesystem is read-only

# Only nginx can reach this port (private Docker network), so its
# X-Forwarded-* headers are trusted.
forwarded_allow_ips = "*"

accesslog = "-"
errorlog = "-"
# %({cf-connecting-ip}i)s is the real client (nginx passes Cloudflare's header on).
access_log_format = '%({cf-connecting-ip}i)s "%(r)s" %(s)s %(b)s %(D)sus "%(a)s"'
