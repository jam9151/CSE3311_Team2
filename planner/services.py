"""Free time detection and the suggestion scoring.

Kept out of views.py so it can be tested on its own, and so it's easy to swap
the scoring for something smarter later without touching the pages.
"""

from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import MIN_USABLE_MINUTES, BacklogItem, Suggestion

# How long a rejection keeps counting against an item.
REJECTION_MEMORY_DAYS = 14


@dataclass(frozen=True)
class Interval:
    start: time
    end: time

    @property
    def minutes(self):
        return _to_minutes(self.end) - _to_minutes(self.start)

    @property
    def label(self):
        return f"{_fmt(self.start)} - {_fmt(self.end)}"


@dataclass(frozen=True)
class BusyBlock:
    start: time
    end: time
    label: str
    source: str  # recurring | commitment | planned


def _to_minutes(value):
    return value.hour * 60 + value.minute


def _from_minutes(total):
    total = max(0, min(24 * 60 - 1, int(total)))
    return time(hour=total // 60, minute=total % 60)


def _fmt(value):
    # %-I isn't portable (breaks on Windows), so strip the zero ourselves.
    return value.strftime("%I:%M %p").lstrip("0")


def humanize_minutes(minutes):
    """180 -> '3h', 200 -> '3h 20m', 45 -> '45 min'."""
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins} min"


def waking_window(user):
    profile = getattr(user, "profile", None)
    if profile:
        return profile.day_start, profile.day_end
    return time(8, 0), time(23, 0)


def busy_blocks(user, day, include_planned=True):
    """Everything taking up time on `day`, sorted by start."""
    blocks = []

    for block in user.recurring_blocks.filter(weekday=day.weekday()):
        blocks.append(BusyBlock(block.start_time, block.end_time, block.label, "recurring"))

    day_start = timezone.make_aware(datetime.combine(day, time.min))
    day_end = day_start + timedelta(days=1)
    for commitment in user.commitments.filter(starts_at__lt=day_end, ends_at__gt=day_start):
        local_start = timezone.localtime(commitment.starts_at)
        local_end = timezone.localtime(commitment.ends_at)
        # A commitment can straddle midnight, so clip it to the day we're on.
        start = local_start.time() if local_start.date() == day else time.min
        end = local_end.time() if local_end.date() == day else time.max
        blocks.append(BusyBlock(start, end, commitment.title, "commitment"))

    if include_planned:
        # Anything already accepted is booked time as far as we're concerned.
        planned = user.suggestions.filter(date=day, status=Suggestion.Status.ACCEPTED)
        for suggestion in planned:
            blocks.append(
                BusyBlock(suggestion.start_time, suggestion.end_time, suggestion.backlog_item.title, "planned")
            )

    return sorted(blocks, key=lambda b: (_to_minutes(b.start), _to_minutes(b.end)))


def free_intervals(user, day, include_planned=True):
    """The gaps left in a day once everything busy is taken out.

    Sweeps left to right keeping a cursor at "earliest time still free".
    Overlapping blocks fall out of this for free, since the cursor only ever
    moves forward.
    """
    start_of_day, end_of_day = waking_window(user)
    cursor = _to_minutes(start_of_day)
    limit = _to_minutes(end_of_day)

    # No point offering a gap that already went by.
    if day == timezone.localdate():
        cursor = max(cursor, _to_minutes(timezone.localtime().time()))

    gaps = []
    for block in busy_blocks(user, day, include_planned=include_planned):
        block_start = _to_minutes(block.start)
        block_end = _to_minutes(block.end)
        if block_end <= cursor:
            continue
        if block_start > cursor:
            gap_end = min(block_start, limit)
            if gap_end - cursor >= MIN_USABLE_MINUTES:
                gaps.append(Interval(_from_minutes(cursor), _from_minutes(gap_end)))
        cursor = max(cursor, block_end)
        if cursor >= limit:
            break

    if limit - cursor >= MIN_USABLE_MINUTES:
        gaps.append(Interval(_from_minutes(cursor), _from_minutes(limit)))

    return gaps


def total_free_minutes(user, day, include_planned=True):
    return sum(gap.minutes for gap in free_intervals(user, day, include_planned=include_planned))


def recent_rejection_counts(user, on_date=None):
    """{backlog_item_id: times turned down recently}."""
    on_date = on_date or timezone.localdate()
    cutoff = on_date - timedelta(days=REJECTION_MEMORY_DAYS)
    counts = {}
    rows = user.suggestions.filter(status=Suggestion.Status.REJECTED, date__gte=cutoff).values_list(
        "backlog_item_id", flat=True
    )
    for item_id in rows:
        counts[item_id] = counts.get(item_id, 0) + 1
    return counts


def _score(item, gap, rejected_counts):
    """Rank one item against one gap. Higher wins. None means it doesn't fit."""
    minutes = item.session_minutes_for(gap.minutes)
    if minutes is None:
        return None

    score = item.priority * 20

    # Finishing something you started beats starting something new.
    if item.status == BacklogItem.Status.IN_PROGRESS:
        score += 25

    # Prefer filling the gap rather than leaving most of it empty.
    score += (minutes / gap.minutes) * 30

    # A film that only just fits is the best use of a long evening, and it's
    # the case the whole app exists for, so give it a nudge.
    if not item.rules["chunkable"]:
        score += 15

    if minutes >= item.remaining_minutes:
        score += 20

    # Not a hard filter -- a rejected item should come back eventually.
    score -= 35 * rejected_counts.get(item.pk, 0)
    return score, minutes


def _reason_for(item, gap, minutes):
    gap_text = humanize_minutes(gap.minutes)
    if not item.rules["chunkable"]:
        return f"You have {gap_text} free and its {humanize_minutes(item.remaining_minutes)} runtime fits in one sitting."
    if minutes >= item.remaining_minutes:
        return f"{gap_text} free is just enough to finish it."
    return f"{gap_text} free, so {item.rules['unit']} of this is a comfortable fit."


def suggest_for_day(user, day=None, limit=3, persist=True):
    """Pick something for each free gap. One item per gap, no repeats."""
    day = day or timezone.localdate()
    gaps = free_intervals(user, day)
    if not gaps:
        return []

    candidates = list(
        user.backlog_items.exclude(status__in=[BacklogItem.Status.COMPLETED, BacklogItem.Status.PAUSED])
    )
    if not candidates:
        return []

    rejected_counts = recent_rejection_counts(user, day)

    # Don't offer something that's already sitting on the same day. Rejected
    # ones are excluded so a refresh can legitimately bring them back.
    used_items = set(
        user.suggestions.filter(date=day)
        .exclude(status=Suggestion.Status.REJECTED)
        .values_list("backlog_item_id", flat=True)
    )

    suggestions = []

    # Biggest gaps first. They have the most options, so spend them deliberately
    # instead of letting a short gap take the item a long one needed.
    for gap in sorted(gaps, key=lambda g: g.minutes, reverse=True):
        if len(suggestions) >= limit:
            break

        best = None
        for item in candidates:
            if item.pk in used_items:
                continue
            scored = _score(item, gap, rejected_counts)
            if scored is None:
                continue
            score, minutes = scored
            if best is None or score > best[0]:
                best = (score, minutes, item)

        if best is None:
            continue

        _, minutes, item = best
        suggestion = Suggestion(
            user=user,
            backlog_item=item,
            date=day,
            start_time=gap.start,
            end_time=_from_minutes(_to_minutes(gap.start) + minutes),
            minutes=minutes,
            reason=_reason_for(item, gap, minutes),
        )
        if persist:
            suggestion.save()
        suggestions.append(suggestion)
        used_items.add(item.pk)

    return suggestions


def day_plan(user, day):
    """Everything the dashboard and the calendar day panel need."""
    return {
        "date": day,
        "busy": busy_blocks(user, day, include_planned=False),
        "free": free_intervals(user, day),
        "planned": list(
            user.suggestions.filter(
                date=day, status__in=[Suggestion.Status.ACCEPTED, Suggestion.Status.DONE]
            ).select_related("backlog_item")
        ),
        "pending": list(
            user.suggestions.filter(date=day, status=Suggestion.Status.PENDING).select_related("backlog_item")
        ),
    }


def month_grid(user, year, month):
    """Six weeks of cells for the calendar page, Sunday first."""
    first = date_cls(year, month, 1)
    # weekday() is Monday=0, and we want Sunday in column 0.
    lead = (first.weekday() + 1) % 7
    grid_start = first - timedelta(days=lead)

    # One query for the whole grid rather than one per cell.
    planned_by_day = {}
    for suggestion in user.suggestions.filter(
        date__gte=grid_start,
        date__lt=grid_start + timedelta(days=42),
        status__in=[Suggestion.Status.ACCEPTED, Suggestion.Status.DONE],
    ).select_related("backlog_item"):
        planned_by_day.setdefault(suggestion.date, []).append(suggestion)

    today = timezone.localdate()
    weeks = []
    for week_index in range(6):
        week = []
        for day_index in range(7):
            day = grid_start + timedelta(days=week_index * 7 + day_index)
            week.append(
                {
                    "date": day,
                    "in_month": day.month == month,
                    "is_today": day == today,
                    "planned": planned_by_day.get(day, []),
                }
            )
        weeks.append(week)

    # Most months don't need all six rows; drop the last one if it's all spill.
    if all(not cell["in_month"] for cell in weeks[-1]):
        weeks.pop()
    return weeks
