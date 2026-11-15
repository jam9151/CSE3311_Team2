"""Browser-level tests: a real Chromium driving the real pages.

They need Playwright, which is not part of the app's requirements:

    pip install -r requirements-dev.txt
    python -m playwright install chromium

Without it every test here is skipped, so `manage.py test` still passes on a
plain checkout. CI installs it and runs this module on its own. The unit tests
in tests.py check the same pages through Django's test client; these check
that the buttons, forms and redirects hang together in a browser, which the
test client cannot see.
"""

import os
import re
from datetime import datetime, time, timedelta
from importlib.util import find_spec
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core import mail
from django.utils import timezone

from .models import BacklogItem, MediaType, Profile, RecurringBlock, Suggestion

HAS_PLAYWRIGHT = find_spec("playwright") is not None
PASSWORD = "correct-horse-battery-9"


def make_user(username="sam"):
    user = get_user_model().objects.create_user(
        username=username, password=PASSWORD, email=f"{username}@mavs.uta.edu"
    )
    Profile.objects.create(user=user, day_start=time(8), day_end=time(23), email_verified=True)
    return user


@skipUnless(HAS_PLAYWRIGHT, "Playwright is not installed; see requirements-dev.txt")
class BrowserFlowTests(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        # Playwright's sync API runs an event loop of its own, which Django
        # otherwise refuses to share a thread with.
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        super().setUpClass()
        from playwright.sync_api import sync_playwright

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        super().tearDownClass()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.set_default_timeout(10_000)

    def tearDown(self):
        self.page.close()

    def url(self, path):
        return f"{self.live_server_url}{path}"

    def sign_in(self, user):
        self.page.goto(self.url("/accounts/login/"))
        self.page.fill("input[name=username]", user.username)
        self.page.fill("input[name=password]", PASSWORD)
        self.page.click("button:has-text('Sign in')")
        self.page.wait_for_url("**/dashboard/")

    def tomorrow(self):
        return timezone.localdate() + timedelta(days=1)

    # ---- flows

    def test_sign_up_confirm_by_email_and_land_on_my_week(self):
        self.page.goto(self.url("/register/"))
        self.page.fill("input[name=username]", "newbie")
        self.page.fill("input[name=email]", "newbie@mavs.uta.edu")
        self.page.fill("input[name=password1]", PASSWORD)
        self.page.fill("input[name=password2]", PASSWORD)
        self.page.click("button:has-text('Get started')")
        self.page.wait_for_url("**/verify/")
        self.assertIn("newbie@mavs.uta.edu", self.page.content())

        # The app is closed until the link in the email is opened.
        self.page.goto(self.url("/dashboard/"))
        self.page.wait_for_url("**/verify/")

        link = re.search(r"https?://\S+/verify/\S+", mail.outbox[-1].body).group(0)
        self.page.goto(link)
        self.page.wait_for_url("**/schedule/")
        self.assertIn("Tell us when you", self.page.text_content("h1"))

    def test_suggest_accept_from_the_calendar_and_mark_done_on_today(self):
        user = make_user()
        BacklogItem.objects.create(user=user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        day = self.tomorrow()
        RecurringBlock.objects.create(
            user=user, label="Work", weekday=day.weekday(), start_time=time(8), end_time=time(19)
        )
        self.sign_in(user)

        # Tomorrow has one evening gap. Ask for a suggestion there and accept it.
        self.page.goto(self.url(f"/calendar/?date={day:%Y-%m-%d}"))
        self.page.click("button:has-text('Suggest something for this day')")
        self.page.wait_for_selector("text=Waiting on you")
        self.page.click("button:has-text('Add to calendar')")
        self.page.wait_for_selector("text=Added A book to your calendar")
        self.assertEqual(Suggestion.objects.filter(status=Suggestion.Status.ACCEPTED).count(), 1)

        # Something already planned for today can be marked done from Today.
        today = timezone.localdate()
        Suggestion.objects.create(
            user=user, backlog_item=user.backlog_items.get(), date=today, start_time=time(7), end_time=time(7, 45),
            minutes=45, status=Suggestion.Status.ACCEPTED,
        )
        self.page.goto(self.url("/dashboard/"))
        self.page.click("button:has-text('Mark done')")
        self.page.wait_for_selector("text=Logged 45 min on A book")
        self.assertEqual(user.backlog_items.get().minutes_completed, 45)

    def test_plan_something_from_a_calendar_day(self):
        user = make_user()
        BacklogItem.objects.create(user=user, title="Dune", media_type=MediaType.MOVIE, estimated_minutes=155)
        day = self.tomorrow()
        self.sign_in(user)

        self.page.goto(self.url(f"/calendar/?date={day:%Y-%m-%d}"))
        self.page.click("a:has-text('Plan something')")
        self.page.wait_for_url("**/calendar/plan/**")
        self.page.select_option("select[name=backlog_item]", label="Dune")
        self.page.fill("input[name=start_time]", "20:00")
        self.page.fill("input[name=minutes]", "155")
        self.page.click("button:has-text('Put it on the calendar')")
        self.page.wait_for_url(f"**/calendar/?date={day:%Y-%m-%d}")
        self.assertIn("Planned 2h 35m of Dune", self.page.content())
        # The grid cell for that day now carries it.
        self.assertIn('data-entry="planned">Dune', self.page.content())

    def test_hide_a_today_panel_and_bring_it_back(self):
        user = make_user()
        self.sign_in(user)

        self.page.click("[data-panel=planned] button:has-text('Hide')")
        self.page.wait_for_selector("text=1 panel hidden")
        self.assertEqual(self.page.locator("[data-panel=planned]").count(), 0)

        self.page.check("input[name=panels][value=planned]")
        self.page.click("#customize button:has-text('Save')")
        self.page.wait_for_selector("text=Today updated")
        self.assertEqual(self.page.locator("[data-panel=planned]").count(), 1)
        user.profile.refresh_from_db()
        self.assertEqual(user.profile.hidden_panels, "")

    def test_log_time_shows_up_on_progress(self):
        user = make_user()
        item = BacklogItem.objects.create(user=user, title="A book", media_type=MediaType.BOOK, estimated_minutes=600)
        self.sign_in(user)

        self.page.goto(self.url("/backlog/"))
        self.page.click("a:has-text('Log time')")
        self.page.wait_for_url(f"**/backlog/{item.pk}/log/**")
        self.page.fill("input[name=minutes]", "40")
        self.page.click("button:has-text('Log it')")
        self.page.wait_for_selector("text=Logged 40 min on A book")

        self.page.goto(self.url("/progress/"))
        self.assertIn("40 min", self.page.text_content("main"))
