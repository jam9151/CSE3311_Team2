from datetime import datetime, timedelta

from django.utils import timezone

from .models import ScheduledSession


def generate_schedule(user, start_date, days=7, session_minutes=60):
    """Fill weekly availability with prioritized backlog items around commitments."""
    items = list(user.media_items.exclude(status="completed"))
    remaining = {item.pk: item.remaining_minutes for item in items}
    end_date = start_date + timedelta(days=days)
    commitments = list(user.commitments.filter(ends_at__date__gte=start_date, starts_at__date__lt=end_date))
    created = []

    for offset in range(days):
        day = start_date + timedelta(days=offset)
        for window in user.availability_windows.filter(weekday=day.weekday()):
            cursor = timezone.make_aware(datetime.combine(day, window.start_time))
            window_end = timezone.make_aware(datetime.combine(day, window.end_time))
            while cursor < window_end:
                active_commitments = [c for c in commitments if c.starts_at <= cursor < c.ends_at]
                if active_commitments:
                    cursor = max(c.ends_at for c in active_commitments)
                    continue
                item = next((candidate for candidate in items if remaining[candidate.pk] > 0), None)
                if not item:
                    return created
                upcoming_starts = [c.starts_at for c in commitments if cursor < c.starts_at < window_end]
                open_slot_end = min(upcoming_starts, default=window_end)
                minutes = min(session_minutes, remaining[item.pk], int((open_slot_end - cursor).total_seconds() // 60))
                if minutes <= 0:
                    break
                end = cursor + timedelta(minutes=minutes)
                created.append(ScheduledSession.objects.create(user=user, media_item=item, starts_at=cursor, ends_at=end))
                remaining[item.pk] -= minutes
                cursor = end
    return created
