"""Grow the catalog with real titles from Open Library, TMDB and RAWG.

    python manage.py import_catalog                     everything you have keys for
    python manage.py import_catalog --only books        just books, no key needed
    python manage.py import_catalog --limit 20          a quick try, 20 per type
    python manage.py import_catalog --dry-run           show what it would add

Safe to run again: titles are matched by where they came from, so a second run
refreshes them instead of adding copies. Keys go in .env, see .env.example.
"""

import os
from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planner.catalog_import import SOURCES, HttpFetcher, ImportFailure, save_record


class Command(BaseCommand):
    help = "Import books, movies, TV and games into the catalog from Open Library, TMDB and RAWG."

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            nargs="+",
            choices=list(SOURCES),
            help="Which types to import. Default is all of them.",
        )
        parser.add_argument("--limit", type=int, default=150, help="Titles to import per type. Default 150.")
        parser.add_argument("--dry-run", action="store_true", help="List what would be imported and save nothing.")

    def handle(self, *args, **options):
        if options["limit"] < 1:
            raise CommandError("--limit must be at least 1.")

        # Open Library gives clients that say who they are a faster rate limit.
        contact = os.environ.get("CATALOG_CONTACT_EMAIL", "").strip() or settings.EMAIL_HOST_USER
        user_agent = f"MediaFlow-CSE3311 ({contact})" if contact else "MediaFlow-CSE3311"

        failures = []
        for name in options["only"] or list(SOURCES):
            source_class = SOURCES[name]
            key = os.environ.get(source_class.key_env, "").strip() if source_class.key_env else ""
            if source_class.key_env and not key:
                self.stdout.write(
                    self.style.WARNING(f"Skipping {name}: {source_class.key_env} isn't set. See .env.example.")
                )
                continue

            source = source_class(HttpFetcher(user_agent, pause=source_class.pause(bool(contact))), key)
            counts = Counter()
            self.stdout.write(f"Importing {name}, up to {options['limit']}...")
            try:
                for record in source.records(options["limit"]):
                    outcome = "would add" if options["dry_run"] else save_record(record)
                    counts[outcome] += 1
                    if options["dry_run"]:
                        self.stdout.write(f"  {record.title} ({record.typical_minutes} min)")
                    elif sum(counts.values()) % 25 == 0:
                        self.stdout.write(f"  {sum(counts.values())} so far")
            except ImportFailure as error:
                failures.append(name)
                self.stderr.write(self.style.ERROR(f"  {name} stopped early: {error}"))

            self.stdout.write(self.style.SUCCESS(f"  {name}: {self._summary(counts, source.skipped)}"))

        if failures:
            raise CommandError(f"Did not finish: {', '.join(failures)}. What was fetched before the problem is saved.")

    @staticmethod
    def _summary(counts, skipped):
        words = {
            "created": "added",
            "updated": "refreshed",
            "duplicate": "already in the catalog",
            "would add": "would add",
        }
        parts = [f"{counts[outcome]} {word}" for outcome, word in words.items() if counts[outcome]]
        if skipped:
            reasons = ", ".join(f"{reason} {number}" for reason, number in skipped.most_common())
            parts.append(f"left out {sum(skipped.values())} ({reasons})")
        return ", ".join(parts) or "nothing to import"
