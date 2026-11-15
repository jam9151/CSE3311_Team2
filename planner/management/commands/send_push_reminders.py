"""Push the reminders that are due. Once, or every minute with --loop.

In Docker this runs as the `push` service (COMPOSE_PROFILES=push). Anywhere
else, a cron line every minute or `--loop` under a process manager does it.
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planner.services import push_due_reminders


class Command(BaseCommand):
    help = "Send background push reminders that are now inside their lead window."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, checking every --interval seconds.")
        parser.add_argument("--interval", type=int, default=60, help="Seconds between checks with --loop.")

    def handle(self, *args, **options):
        if not settings.PUSH_ENABLED:
            raise CommandError(
                "Push reminders are off: VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY are not both set. "
                "Run `manage.py vapid_keys` and put them in .env."
            )
        if options["interval"] < 1:
            raise CommandError("--interval must be at least 1 second.")

        while True:
            counts = push_due_reminders()
            if counts:
                self.stdout.write(
                    f"{counts['users']} user(s) due: {counts['sent']} sent, {counts['gone']} unsubscribed, "
                    f"{counts['error']} failed"
                )
            if not options["loop"]:
                return
            try:
                time.sleep(options["interval"])
            except KeyboardInterrupt:
                return
