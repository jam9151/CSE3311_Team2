"""Free-time detection and the suggestion engine.

Two steps, kept deliberately separate:

1. ``free_intervals`` turns a user's commitments into the gaps between them.
2. ``suggest_for_day`` scores backlog items against each gap and proposes the
   best fit, taking past rejections into account.
"""

from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.utils import timezone

from .models import MIN_USABLE_MINUTES, BacklogItem, Suggestion

# How far back a rejection keeps counting against an item.
REJECTION_MEMORY_DAYS = 14


@dataclass(frozen=True)
class Interval:
    """A span of time on a single day."""

    start: time
    end: time

    @property
    def minutes(self):
        return int((_to_minutes(self.end) - _to_minutes(self.start)))

    @property
    def label(self):
        return f"{_fmt(self.start)} - {_fmt(self.end)}"


@dataclass(frozen=True)
class BusyBlock:
    """Something already occupying part of the day."""

    start: time
    end: time
    label: str
    source: str  # "recurring", "commitment", or "planned"


def _to_minutes(value):
    return value.hour * 60 + value.minute


def _from_minutes(total):
    total = max(0, min(24 * 60 - 1, int(total)))
    return time(hour=total // 60, minute=total % 60)


def _fmt(value):
    """12-hour time without a leading zero, portable across platforms."""
    return value.strftime("%I:%M %p").lstrip("0")


def humanize_minutes(minutes):
    """Render a duration the way a person would say it: 3h 20m, 45 min, 2h."""
    minutes = int(minutes)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins} min"


def waking_window(user):
    """The user's waking hours, falling back to a sensible default."""
    profile = getattr(user, "profile", None)
    if profile:
        return profile.day_start, profile.day_end
    return time(8, 0), time(23, 0)


def busy_blocks(user, day, include_planned=True):
    """Everything that occupies time on ``day``, sorted and normalised to times."""
    blocks = []

    for block in user.recurring_blocks.filter(weekday=day.weekday()):
        blocks.append(BusyBlock(block.start_time, block.end_time, block.label, "recurring"))

    day_start = timezone.make_aware(datetime.combine(day, time.min))
    day_end = day_start + timedelta(days=1)
    for commitment in user.commitments.filter(starts_at__lt=day_end, ends_at__gt=day_start):
        local_start = timezone.localtime(commitment.starts_at)
        local_end = timezone.localtime(commitment.ends_at)
        # Clip a multi-day commitment to this day.
        start = local_start.time() if local_start.date() == day else time.min
        end = local_end.time() if local_end.date() == day else time.max
        blocks.append(BusyBlock(start, end, commitment.title, "commitment"))

    if include_planned:
        planned = user.suggestions.filter(date=day, status=Suggestion.Status.ACCEPTED)
        for suggestion in planned:
            blocks.append(
                BusyBlock(suggestion.start_time, suggestion.end_time, suggestion.backlog_item.title, "planned")
            )

    return sorted(blocks, key=lambda b: (_to_minutes(b.start), _to_minutes(b.end)))


def free_intervals(user, day, include_planned=True):
    """The usable gaps in a user's day.

    Walks the waking window left to right, skipping past anything busy. Only
    gaps of at least ``MIN_USABLE_MINUTES`` are returned, since a ten-minute
    sliver is not worth a recommendation.
    """
    start_of_day, end_of_day = waking_window(user)
    cursor = _to_minutes(start_of_day)
    limit = _to_minutes(end_of_day)

    # Today's gaps that have already passed are not offerable.
    if day == timezone.localdate():
        now = _to_minutes(timezone.localtime().time())
        cursor = max(cursor, now)

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

    if cursor < limit and limit - cursor >= MIN_USABLE_MINUTES:
        gaps.append(Interval(_from_minutes(cursor), _from_minutes(limit)))

    return gaps


def total_free_minutes(user, day, include_planned=True):
    return sum(gap.minutes for gap in free_intervals(user, day, include_planned=include_planned))


def _rejection_penalty(item, rejected_counts):
    """Recent rejections push an item down, but never remove it permanently."""
    return 35 * rejected_counts.get(item.pk, 0)


def _score(item, gap, rejected_counts):
    """Rank one backlog item against one gap. Higher is better."""
    minutes = item.session_minutes_for(gap.minutes)
    if minutes is None:
        return None

    score = item.priority * 20

    # Finishing something already started beats starting something new.
    if item.status == BacklogItem.Status.IN_PROGRESS:
        score += 25

    # Reward filling the gap well. A 2h film in a 2h10m gap is a great fit; the
    # same film in a 5h gap leaves time on the table.
    usage = minutes / gap.minutes
    score += usage * 30

    # A film that only just fits is the most satisfying use of a long evening.
    if not item.rules["chunkable"]:
        score += 15

    # An item we can actually finish in this sitting is worth extra.
    if minutes >= item.remaining_minutes:
        score += 20

    score -= _rejection_penalty(item, rejected_counts)
    return score, minutes


def _reason_for(item, gap, minutes):
    """The one-line explanation shown with the suggestion."""
    gap_text = humanize_minutes(gap.minutes)
    if not item.rules["chunkable"]:
        return f"You have {gap_text} free and its {humanize_minutes(item.remaining_minutes)} runtime fits in one sitting."
    if minutes >= item.remaining_minutes:
        return f"{gap_text} free is just enough to finish it."
    unit = item.rules["unit"]
    return f"{gap_text} free, so {unit} of this is a comfortable fit."


def recent_rejection_counts(user, on_date=None):
    """How many times each backlog item has been turned down lately."""
    on_date = on_date or timezone.localdate()
    cutoff = on_date - timedelta(days=REJECTION_MEMORY_DAYS)
    rows = user.suggestions.filter(status=Suggestion.Status.REJECTED, date__gte=cutoff).values_list(
        "backlog_item_id", flat=True
    )
    counts = {}
    for item_id in rows:
        counts[item_id] = counts.get(item_id, 0) + 1
    return counts


def suggest_for_day(user, day=None, limit=3, persist=True):
    """Propose what to do with today's free time.

    Returns the ``Suggestion`` objects created (or built, when ``persist`` is
    False). One suggestion per gap, best gaps first, no item repeated.
    """
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
    already_suggested = set(
        user.suggestions.filter(date=day)
        .exclude(status=Suggestion.Status.REJECTED)
        .values_list("backlog_item_id", flat=True)
    )

    suggestions = []
    used_items = set(already_suggested)

    # Longest gaps first: they have the most options, so fill them deliberately.
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
        end_minutes = _to_minutes(gap.start) + minutes
        suggestion = Suggestion(
            user=user,
            backlog_item=item,
            date=day,
            start_time=gap.start,
            end_time=_from_minutes(end_minutes),
            minutes=minutes,
            reason=_reason_for(item, gap, minutes),
        )
        if persist:
            suggestion.save()
        suggestions.append(suggestion)
        used_items.add(item.pk)

    return suggestions


def day_plan(user, day):
    """Everything needed to render one day: busy blocks, gaps, and the plan."""
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
    """A six-week calendar grid for the month, each cell annotated with load.

    Weeks run Sunday to Saturday to match how the class sketched the screen.
    """
    first = date_cls(year, month, 1)
    # Sunday-first offset: Python's weekday() is Monday=0, so Sunday is 6.
    lead = (first.weekday() + 1) % 7
    grid_start = first - timedelta(days=lead)

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

    # Drop a trailing week that belongs entirely to the next month.
    if all(not cell["in_month"] for cell in weeks[-1]):
        weeks.pop()
    return weeks
