"""
run_worker — run the CPU or GPU job worker (see processing/worker.py).

    python manage.py run_worker --resource cpu
    python manage.py run_worker --resource gpu
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from processing.models import Resource
from processing.worker import Worker


class Command(BaseCommand):
    help = "Process jobs from the queue until stopped (SIGTERM finishes the current job first)."

    def add_arguments(self, parser):
        parser.add_argument("--resource", choices=Resource.values, required=True)
        parser.add_argument("--once", action="store_true",
                            help="process whatever is claimable now, then exit (tests/maintenance)")
        parser.add_argument("--poll-seconds", type=float, default=1.0)

    def handle(self, *args, resource: str, once: bool, poll_seconds: float, **options):
        logging.getLogger("processing").setLevel(logging.INFO)
        worker = Worker(resource, poll_seconds=poll_seconds)
        if not once:
            worker.install_signal_handlers()
        processed = worker.run(once=once)
        self.stdout.write(f"{resource} worker stopped after {processed} job(s).")
