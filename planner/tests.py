from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.test import TestCase

from django.utils import timezone

from .models import AvailabilityWindow, Commitment, MediaItem, ScheduledSession
from .services import generate_schedule


class SchedulerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="sam", password="test-password")

    def test_prioritizes_and_splits_media_into_sessions(self):
        monday = date(2026, 9, 7)
        AvailabilityWindow.objects.create(user=self.user, weekday=0, start_time=time(18), end_time=time(20))
        first = MediaItem.objects.create(user=self.user, title="Important book", media_type="book", priority=5, estimated_minutes=90)
        second = MediaItem.objects.create(user=self.user, title="Later movie", media_type="movie", priority=2, estimated_minutes=120)

        sessions = generate_schedule(self.user, monday)

        self.assertEqual(len(sessions), 3)
        self.assertEqual([session.media_item for session in sessions], [first, first, second])
        self.assertEqual(ScheduledSession.objects.count(), 3)

    def test_schedules_before_and_after_a_commitment(self):
        monday = date(2026, 9, 7)
        AvailabilityWindow.objects.create(user=self.user, weekday=0, start_time=time(18), end_time=time(21))
        item = MediaItem.objects.create(user=self.user, title="A long game", media_type="game", priority=5, estimated_minutes=120)
        Commitment.objects.create(
            user=self.user,
            title="Dinner",
            starts_at=timezone.make_aware(datetime(2026, 9, 7, 19, 0)),
            ends_at=timezone.make_aware(datetime(2026, 9, 7, 20, 0)),
        )

        sessions = generate_schedule(self.user, monday)

        self.assertEqual(len(sessions), 2)
        self.assertEqual([timezone.localtime(session.starts_at).hour for session in sessions], [18, 20])
        self.assertTrue(all(session.media_item == item for session in sessions))


class PageTests(TestCase):
    def test_home_page_is_public(self):
        self.assertContains(self.client.get("/"), "Make time for the stories")

    def test_dashboard_requires_login(self):
        response = self.client.get("/dashboard/")
        self.assertRedirects(response, "/accounts/login/?next=/dashboard/")
