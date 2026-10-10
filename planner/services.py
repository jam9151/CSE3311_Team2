"""Free time detection and the suggestion scoring.

Kept out of views.py so it can be tested on its own, and so it's easy to swap
the scoring for something smarter later without touching the pages.
"""

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.db.models import Avg, Count, F
from django.utils import timezone

from .models import MIN_USABLE_MINUTES, BacklogItem, MediaType, Suggestion

@dataclass(frozen=True)
class ScoringWeights:
    """Every number the suggestion scoring uses, and why it is what it is.

    An item's score in a gap is the sum of the bonuses below minus the
    penalties. The biggest score in a gap wins. The numbers only mean anything
    next to each other, so each comment says where its number sits in steps of
    one priority level (per_priority_step). ScoringTradeoffTests pins those
    relationships, so changing a value here fails a test that names the
    trade-off you just changed.

    Pass your own ScoringWeights to suggest_for_day or suggest_for_week to try
    other values without touching the defaults.
    """

    # The user's own say-so, 1 to 5, so 20 to 100. The biggest single term, and
    # one level (20) is the unit the other numbers are measured in.
    per_priority_step: float = 20

    # Finishing what you started beats starting something new. A bit more than
    # one step: a started priority 3 beats a fresh 4, but a fresh 5 still beats
    # a started 3.
    in_progress_bonus: float = 25

    # Multiplied by the share of the gap the sitting uses, so 0 to 30. It fills
    # a gap well rather than leaving most of it empty. Between one step and two:
    # a perfect fit can reorder neighbouring priorities but can't lift an item
    # two levels.
    gap_fill_bonus: float = 30

    # A film that fits a gap is the best use of a long evening, and the case
    # the app exists for. A film also always earns finishes_item_bonus, since it
    # is watched in one go, so a film that fits leads by 35 before gap fill:
    # one step and a bit more. A priority 2 film beats a priority 3 chapter,
    # and can edge a priority 4 chapter when it fills the gap better. Lower this
    # if films win more often than you want.
    whole_film_bonus: float = 15

    # Being able to finish the item in this sitting. Equal to one step: a done
    # item is worth about as much as a more wanted one.
    finishes_item_bonus: float = 20

    # Taken off per time the user said "Not today" inside the memory window.
    # More than one step and less than two: one no drops a 4 below a fresh 3
    # but not below a fresh 2, and it is not a hard filter, so the item comes
    # back once the window passes.
    rejection_penalty: float = 35
    rejection_memory_days: int = 14

    # Taken off per sitting of the same item already planned earlier in the
    # week, so a week mixes titles. Less than one step: a priority buys at
    # least one repeat before the next item gets a turn, then the next item
    # starts to catch up.
    variety_penalty: float = 15

    def __post_init__(self):
        for name, value in vars(self).items():
            if value < 0:
                raise ValueError(f"{name} can't be negative.")
        if self.rejection_memory_days < 1:
            raise ValueError("rejection_memory_days must be at least 1.")


DEFAULT_WEIGHTS = ScoringWeights()


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


def recent_rejection_counts(user, on_date=None, memory_days=None):
    """{backlog_item_id: times turned down recently}."""
    on_date = on_date or timezone.localdate()
    memory_days = DEFAULT_WEIGHTS.rejection_memory_days if memory_days is None else memory_days
    cutoff = on_date - timedelta(days=memory_days)
    counts = {}
    rows = user.suggestions.filter(status=Suggestion.Status.REJECTED, date__gte=cutoff).values_list(
        "backlog_item_id", flat=True
    )
    for item_id in rows:
        counts[item_id] = counts.get(item_id, 0) + 1
    return counts


def _score(item, gap, rejected_counts, earlier=(), weights=DEFAULT_WEIGHTS):
    """Rank one item against one gap. Higher wins. None means it doesn't fit.

    `earlier` is the minutes of each sitting of this item already planned on
    other days of the week being planned. See ScoringWeights for the numbers.
    """
    remaining = max(0, item.remaining_minutes - sum(earlier))
    minutes = item.session_minutes_for(gap.minutes, remaining=remaining)
    if minutes is None:
        return None

    score = item.priority * weights.per_priority_step

    if item.status == BacklogItem.Status.IN_PROGRESS:
        score += weights.in_progress_bonus

    score += (minutes / gap.minutes) * weights.gap_fill_bonus

    if not item.rules["chunkable"]:
        score += weights.whole_film_bonus

    if minutes >= remaining:
        score += weights.finishes_item_bonus

    score -= weights.variety_penalty * len(earlier)
    score -= weights.rejection_penalty * rejected_counts.get(item.pk, 0)
    return score, minutes, remaining


def _reason_for(item, gap, minutes, remaining):
    gap_text = humanize_minutes(gap.minutes)
    if not item.rules["chunkable"]:
        return f"You have {gap_text} free and its {humanize_minutes(remaining)} runtime fits in one sitting."
    if minutes >= remaining:
        return f"{gap_text} free is just enough to finish it."
    return f"{gap_text} free, so {item.rules['unit']} of this is a comfortable fit."


def suggest_for_day(user, day=None, limit=3, persist=True, planned=None, weights=None):
    """Pick something for each free gap. One item per gap, no repeats.

    `planned` is {backlog_item_id: [minutes, ...]} for sittings already planned
    on other days. Only suggest_for_week passes it. `weights` is a
    ScoringWeights, the defaults if left out.
    """
    day = day or timezone.localdate()
    planned = planned or {}
    weights = weights or DEFAULT_WEIGHTS
    gaps = free_intervals(user, day)
    if not gaps:
        return []

    candidates = list(
        user.backlog_items.exclude(status__in=[BacklogItem.Status.COMPLETED, BacklogItem.Status.PAUSED])
    )
    if not candidates:
        return []

    rejected_counts = recent_rejection_counts(user, day, weights.rejection_memory_days)

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
            scored = _score(item, gap, rejected_counts, planned.get(item.pk, ()), weights)
            if scored is None:
                continue
            score, minutes, remaining = scored
            if best is None or score > best[0]:
                best = (score, minutes, remaining, item)

        if best is None:
            continue

        _, minutes, remaining, item = best
        suggestion = Suggestion(
            user=user,
            backlog_item=item,
            date=day,
            start_time=gap.start,
            end_time=_from_minutes(_to_minutes(gap.start) + minutes),
            minutes=minutes,
            reason=_reason_for(item, gap, minutes, remaining),
        )
        if persist:
            suggestion.save()
        suggestions.append(suggestion)
        used_items.add(item.pk)

    return suggestions


def suggest_for_week(user, start=None, days=7, per_day=3, persist=True, weights=None):
    """Run suggest_for_day over a stretch of days. Returns {date: [suggestions]}.

    Days are planned in order and each one is told what the earlier days took.
    That stops a film being planned twice, stops a book being planned past its
    last page, and nudges later days toward something different.

    Callers clear stale pending suggestions in the window first; anything still
    pending or accepted there is counted as already planned.
    """
    start = start or timezone.localdate()
    window = [start + timedelta(days=offset) for offset in range(days)]

    planned = {}
    existing = user.suggestions.filter(
        date__range=(window[0], window[-1]),
        status__in=[Suggestion.Status.ACCEPTED, Suggestion.Status.PENDING],
    )
    for suggestion in existing:
        planned.setdefault(suggestion.backlog_item_id, []).append(suggestion.minutes)

    plan = {}
    for day in window:
        made = suggest_for_day(user, day, limit=per_day, persist=persist, planned=planned, weights=weights)
        for suggestion in made:
            planned.setdefault(suggestion.backlog_item_id, []).append(suggestion.minutes)
        plan[day] = made
    return plan


DEFAULT_REMINDER_LEAD = 15


def reminder_lead_minutes(user):
    profile = getattr(user, "profile", None)
    return profile.notify_lead_minutes if profile else DEFAULT_REMINDER_LEAD


def upcoming_reminders(user, now=None, lead=None):
    """Accepted sessions and commitments starting within the lead window.

    Returns dicts rather than model rows so the same data feeds the dashboard
    banner and the JSON endpoint the browser polls. `key` is stable per event
    and start time, which is what lets the browser avoid notifying twice.
    """
    now = timezone.localtime(now or timezone.now())
    lead = reminder_lead_minutes(user) if lead is None else lead
    horizon = now + timedelta(minutes=lead)
    reminders = []

    sessions = user.suggestions.filter(
        status=Suggestion.Status.ACCEPTED, date__in={now.date(), horizon.date()}
    ).select_related("backlog_item")
    for suggestion in sessions:
        starts = timezone.make_aware(datetime.combine(suggestion.date, suggestion.start_time))
        if now <= starts <= horizon:
            reminders.append(
                {
                    "key": f"session-{suggestion.pk}-{starts.isoformat()}",
                    "kind": "session",
                    "title": suggestion.backlog_item.title,
                    "starts_at": starts,
                    "minutes_until": max(0, round((starts - now).total_seconds() / 60)),
                    "detail": f"{humanize_minutes(suggestion.minutes)} planned",
                }
            )

    for commitment in user.commitments.filter(starts_at__gte=now, starts_at__lte=horizon):
        reminders.append(
            {
                "key": f"commitment-{commitment.pk}-{commitment.starts_at.isoformat()}",
                "kind": "commitment",
                "title": commitment.title,
                "starts_at": commitment.starts_at,
                "minutes_until": max(0, round((commitment.starts_at - now).total_seconds() / 60)),
                "detail": "Commitment",
            }
        )

    return sorted(reminders, key=lambda r: r["starts_at"])


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


# ------------------------------------------------------------ progress

# Fixed, so a type keeps its place and its colour on the progress page whatever
# the numbers are.
MEDIA_ORDER = [MediaType.MOVIE, MediaType.TV, MediaType.BOOK, MediaType.GAME]

# Axis steps in minutes, smallest first.
_AXIS_STEPS = [15, 30, 60, 120, 180, 240, 360, 480, 720, 960]


def _axis(top):
    """(step, maximum) for a chart axis: round numbers, at most four steps."""
    if top <= 0:
        return 15, 60
    for step in _AXIS_STEPS:
        if math.ceil(top / step) <= 4:
            return step, math.ceil(top / step) * step
    step = _AXIS_STEPS[-1]
    return step, math.ceil(top / step) * step


def _streaks(active_days, today):
    """(current, best) runs of consecutive days with something logged.

    The current run still counts if today is empty but yesterday wasn't, so it
    doesn't read zero first thing in the morning.
    """
    cursor = today if today in active_days else today - timedelta(days=1)
    current = 0
    while cursor in active_days:
        current += 1
        cursor -= timedelta(days=1)

    best = run = 0
    previous = None
    for day in sorted(active_days):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = day
    return current, best


def progress_stats(user, today=None, days=30):
    """Numbers for the progress page, all from sessions marked done.

    Time comes from Suggestion rows with status done, on the day they were
    marked done. Minutes typed into a backlog item by hand have no date, so
    they can't be placed on a day and are not counted here.
    """
    today = today or timezone.localdate()
    month_start = today.replace(day=1)

    per_day = Counter()
    month_minutes_by_type = Counter()
    month_sessions = 0
    total_minutes = 0
    done = user.suggestions.filter(status=Suggestion.Status.DONE).values_list(
        "responded_at", "date", "minutes", "backlog_item__media_type"
    )
    for responded_at, planned_date, minutes, media_type in done:
        day = timezone.localtime(responded_at).date() if responded_at else planned_date
        if day > today:
            continue
        per_day[day] += minutes
        total_minutes += minutes
        if day >= month_start:
            month_minutes_by_type[media_type] += minutes
            month_sessions += 1

    current_streak, best_streak = _streaks({day for day, minutes in per_day.items() if minutes > 0}, today)

    # The daily chart.
    window = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    top = max((per_day[day] for day in window), default=0)
    step, axis_max = _axis(top)
    best_day = max(window, key=lambda day: (per_day[day], -window.index(day))) if top else None
    daily = []
    for index, day in enumerate(window):
        minutes = per_day[day]
        daily.append(
            {
                "date": day,
                "minutes": minutes,
                "label": humanize_minutes(minutes) if minutes else "Nothing logged",
                "height": round(minutes / axis_max * 100, 1),
                "is_today": day == today,
                "is_best": day == best_day,
                # Keeps the best-day label inside the card at either end.
                "label_align": "left" if index < 3 else "right" if index >= days - 3 else "center",
            }
        )
    ticks = [
        {"minutes": value, "label": humanize_minutes(value) if value else "0", "bottom": round(value / axis_max * 100, 1)}
        for value in range(0, axis_max + 1, step)
    ]

    # Finished things.
    completed = user.backlog_items.filter(status=BacklogItem.Status.COMPLETED)
    finished_this_month = Counter()
    finished_total = 0
    for completed_at, media_type in completed.values_list("completed_at", "media_type"):
        finished_total += 1
        if completed_at and timezone.localtime(completed_at).date() >= month_start:
            finished_this_month[media_type] += 1

    month_minutes = sum(month_minutes_by_type.values())
    by_type = [
        {
            "key": media_type.value,
            "label": media_type.label,
            "minutes": month_minutes_by_type[media_type.value],
            "time": humanize_minutes(month_minutes_by_type[media_type.value]) if month_minutes_by_type[media_type.value] else "",
            "percent": round(month_minutes_by_type[media_type.value] / month_minutes * 100, 1) if month_minutes else 0,
            "finished": finished_this_month[media_type.value],
        }
        for media_type in MEDIA_ORDER
    ]

    reviews = user.reviews.aggregate(average=Avg("rating"), count=Count("id"))
    return {
        "today": today,
        "month_minutes": month_minutes,
        "month_time": humanize_minutes(month_minutes),
        "month_sessions": month_sessions,
        "total_minutes": total_minutes,
        "total_time": humanize_minutes(total_minutes),
        "finished_this_month": sum(finished_this_month.values()),
        "finished_total": finished_total,
        "current_streak": current_streak,
        "best_streak": best_streak,
        "average_rating": round(reviews["average"], 1) if reviews["average"] is not None else None,
        "review_count": reviews["count"],
        "daily": daily,
        "logged_days": [entry for entry in reversed(daily) if entry["minutes"]],
        "ticks": ticks,
        "has_chart_data": top > 0,
        "by_type": by_type,
        "recent_finished": list(completed.order_by(F("completed_at").desc(nulls_last=True))[:5]),
        "in_progress": list(user.backlog_items.filter(status=BacklogItem.Status.IN_PROGRESS)[:5]),
    }
