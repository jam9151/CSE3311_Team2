"""Deletes user accounts so sign-up and the confirmation email can be retested.

Deleting a user also deletes everything they own: profile, backlog, schedule,
suggestions and reviews. The shared catalog is untouched.

    python manage.py reset_accounts                 asks first, keeps staff
    python manage.py reset_accounts --yes           no prompt
    python manage.py reset_accounts --keep-demo     leave the demo account alone
    python manage.py reset_accounts --include-staff delete admins too

Only runs with DEBUG on, so it can't be pointed at a real deployment by accident.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Delete user accounts and their data so sign-up can be tested again. Development only."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Don't ask for confirmation.")
        parser.add_argument("--keep-demo", action="store_true", help="Keep the demo account.")
        parser.add_argument(
            "--include-staff", action="store_true", help="Also delete staff and superuser accounts."
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Refusing to run with DEBUG off. This deletes real accounts.")

        users = get_user_model().objects.all()
        if not options["include_staff"]:
            users = users.filter(is_staff=False, is_superuser=False)
        if options["keep_demo"]:
            users = users.exclude(username="demo")

        accounts = list(users.order_by("username").values_list("username", "email"))
        if not accounts:
            self.stdout.write("No accounts to delete.")
            return

        self.stdout.write(f"This will delete {len(accounts)} account(s) and all of their data:")
        for username, email in accounts:
            self.stdout.write(f"  {username}  {email or '(no email)'}")

        if not options["yes"]:
            answer = input("Type 'yes' to continue: ")
            if answer.strip().lower() != "yes":
                self.stdout.write("Nothing deleted.")
                return

        users.delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {len(accounts)} account(s)."))
        if not options["keep_demo"]:
            self.stdout.write("Run `python manage.py seed_catalog --demo` to bring the demo account back.")
