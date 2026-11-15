"""Free time detection and the suggestion scoring.

Kept out of views.py so it can be tested on its own, and so it's easy to swap
the scoring for something smarter later without touching the pages.
"""

import json
import logging
import math
from bisect import insort
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Avg, Count, F, Q
from django.utils import timezone

from .models import MIN_USABLE_MINUTES, BacklogItem, MediaType, Review, SentReminder, Suggestion

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

    # What other students thought. Multiplied by (average rating - 3) / 2, so
    # a title classmates rated 5 gets the whole bonus and one rated 1 loses
    # it. Half a step: it breaks ties between equals and nudges neighbours,
    # but the user's own priority always wins over the crowd.
    community_bonus: float = 10

    # What this user has accepted and turned down at this time of day for
    # this kind of thing: films after dinner, chapters before class.
    # Multiplied by (accepts - rejects) / (accepts + rejects) for the media
    # type in the gap's part of the day, once there are at least two answers
    # to go on. Half a step, like the community bonus, for the same reason.
    time_of_day_bonus: float = 10
    time_of_day_memory_days: int = 60
    time_of_day_min_answers: int = 2

    def __post_init__(self):
        for name, value in vars(self).items():
            if value < 0:
                raise ValueError(f"{name} can't be negative.")
        if self.rejection_memory_days < 1:
            raise ValueError("rejection_memory_days must be at least 1.")
        if self.time_of_day_memory_days < 1 or self.time_of_day_min_answers < 1:
            raise ValueError("time_of_day_memory_days and time_of_day_min_answers must be at least 1.")


DEFAULT_WEIGHTS = ScoringWeights()


@dataclass(frozen=True)
class Interval:
    start: time
    end: time
    # Free minutes past midnight that a sitting started in this gap may run
    # into, on an overnight schedule. Not part of `minutes`, which is what the
    # pages add up per date; only the planner spends it.
    extension: int = 0

    @property
    def minutes(self):
        # A gap that runs to midnight is stored ending 23:59, the last minute
        # there is, so count that minute too.
        end = END_OF_DAY if self.end == time(23, 59) else _to_minutes(self.end)
        return end - _to_minutes(self.start)

    @property
    def usable(self):
        """What a sitting started here can be: the gap itself plus any run past midnight."""
        return self.minutes + self.extension

    @property
    def label(self):
        return f"{_fmt(self.start)} - {_fmt(self.end)}"


@dataclass(frozen=True)
class BusyBlock:
    start: time
    end: time
    label: str
    source: str  # recurring | commitment | planned
    # The RecurringBlock id for recurring blocks, so a page can offer to skip
    # it on this date. None for everything else.
    block_id: int | None = None


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


# One minute past the last minute of the day. A window or a gap that runs to
# midnight ends here, and _from_minutes turns it into 23:59 for storage.
END_OF_DAY = 24 * 60


def waking_windows(user):
    """The stretches of one calendar day the user is awake, in minutes from midnight.

    A normal day is one stretch, 08:00 to 23:00. A bedtime past midnight, say
    up by 10:00 and in bed by 02:00, means two stretches on every calendar
    day: the small hours, 00:00 to 02:00, left over from the evening before,
    and the main part, 10:00 to midnight. Splitting at midnight keeps every
    gap, suggestion and reminder on one date, which is what the calendar,
    the day panel and the reminder poll all assume.
    """
    start, end = waking_window(user)
    start_min, end_min = _to_minutes(start), _to_minutes(end)
    if end_min > start_min:
        return [(start_min, end_min)]
    return [(0, end_min), (start_min, END_OF_DAY)]


def _end_time_for(start_minutes, minutes):
    """The end time of a sitting, wrapping past midnight.

    A sitting that ends exactly at midnight is stored ending 23:59, the last
    minute of its date (as gaps are). One that runs past midnight gets the
    small-hours time, which is earlier on the clock than its start; that is
    how Suggestion.crosses_midnight knows.
    """
    total = start_minutes + minutes
    if total > END_OF_DAY:
        return _from_minutes(total - END_OF_DAY)
    return _from_minutes(total)


def busy_blocks(user, day, include_planned=True, exclude_suggestion=None):
    """Everything taking up time on `day`, sorted by start.

    `exclude_suggestion` leaves one accepted sitting out, for checking where
    it could move to.
    """
    blocks = []

    skipped = set(user.block_exceptions.filter(date=day).values_list("block_id", flat=True))
    for block in user.recurring_blocks.filter(weekday=day.weekday()):
        if block.pk in skipped:
            continue
        blocks.append(BusyBlock(block.start_time, block.end_time, block.label, "recurring", block.pk))

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
        planned = user.suggestions.filter(date=day, status=Suggestion.Status.ACCEPTED).select_related("backlog_item")
        if exclude_suggestion is not None:
            planned = planned.exclude(pk=exclude_suggestion)
        for suggestion in planned:
            # One that runs past midnight is busy to the end of this date.
            end = time(23, 59) if suggestion.crosses_midnight else suggestion.end_time
            blocks.append(BusyBlock(suggestion.start_time, end, suggestion.backlog_item.title, "planned"))
        # And the small hours of this date may be taken by last night's sitting.
        overnight = user.suggestions.filter(
            date=day - timedelta(days=1), status=Suggestion.Status.ACCEPTED, end_time__lt=F("start_time")
        ).select_related("backlog_item")
        if exclude_suggestion is not None:
            overnight = overnight.exclude(pk=exclude_suggestion)
        for suggestion in overnight:
            blocks.append(BusyBlock(time.min, suggestion.end_time, suggestion.backlog_item.title, "planned"))

    return sorted(blocks, key=lambda b: (_to_minutes(b.start), _to_minutes(b.end)))


def free_intervals(user, day, include_planned=True):
    """The gaps left in a day once everything busy is taken out.

    Sweeps left to right keeping a cursor at "earliest time still free".
    Overlapping blocks fall out of this for free, since the cursor only ever
    moves forward.
    """
    # No point offering a gap that already went by.
    now = _to_minutes(timezone.localtime().time()) if day == timezone.localdate() else 0

    blocks = busy_blocks(user, day, include_planned=include_planned)
    gaps = []
    for window_start, window_end in waking_windows(user):
        gaps.extend(_gaps_in_window(blocks, max(window_start, now), window_end))

    # On an overnight schedule the evening gap may run on into the small
    # hours of the next date, as far as the first busy thing there or
    # bedtime. The next date still owns those hours (they stay in its own
    # gaps and totals); this only lets a sitting started tonight use them.
    profile = getattr(user, "profile", None)
    if gaps and profile and profile.is_overnight and gaps[-1].end == time(23, 59):
        bedtime = _to_minutes(profile.day_end)
        next_blocks = busy_blocks(user, day + timedelta(days=1), include_planned=include_planned)
        first_busy = min((_to_minutes(b.start) for b in next_blocks if _to_minutes(b.end) > 0), default=bedtime)
        extension = min(first_busy, bedtime)
        if extension > 0:
            gaps[-1] = replace(gaps[-1], extension=extension)
    return gaps


def _gaps_in_window(blocks, cursor, limit):
    """The free stretches between `cursor` and `limit`, both in minutes."""
    gaps = []
    for block in blocks:
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


# The four parts of a day the time-of-day preference is learned over.
PARTS_OF_DAY = ["morning", "afternoon", "evening", "night"]


def part_of_day(value):
    """'morning' before noon, 'afternoon' to 17:00, 'evening' to 21:00, 'night' after."""
    hour = value.hour
    if hour < 12:
        return "morning"
    if hour < 17:
        return "afternoon"
    if hour < 21:
        return "evening"
    return "night"


def time_of_day_preferences(user, on_date=None, memory_days=None, min_answers=None):
    """{(media_type, part_of_day): score in -1..1} from what the user accepted and turned down.

    Accepted and done count for, rejected against; "Later" is neither and is
    not here. Pairs with fewer than `min_answers` answers are left out.
    """
    on_date = on_date or timezone.localdate()
    memory_days = DEFAULT_WEIGHTS.time_of_day_memory_days if memory_days is None else memory_days
    min_answers = DEFAULT_WEIGHTS.time_of_day_min_answers if min_answers is None else min_answers
    cutoff = on_date - timedelta(days=memory_days)
    tally = {}
    rows = user.suggestions.filter(
        date__gte=cutoff,
        status__in=[Suggestion.Status.ACCEPTED, Suggestion.Status.DONE, Suggestion.Status.REJECTED],
    ).values_list("backlog_item__media_type", "start_time", "status")
    for media_type, start, status in rows:
        key = (media_type, part_of_day(start))
        yes, no = tally.get(key, (0, 0))
        if status == Suggestion.Status.REJECTED:
            tally[key] = (yes, no + 1)
        else:
            tally[key] = (yes + 1, no)
    return {
        key: (yes - no) / (yes + no)
        for key, (yes, no) in tally.items()
        if yes + no >= min_answers
    }


def community_ratings(user):
    """{catalog_item_id: average rating out of 5} from other students' shared reviews."""
    rows = (
        Review.objects.filter(shared=True, backlog_item__catalog_item__isnull=False)
        .exclude(user=user)
        .values("backlog_item__catalog_item_id")
        .annotate(average=Avg("rating"))
    )
    return {row["backlog_item__catalog_item_id"]: float(row["average"]) for row in rows}


def _score(item, gap, rejected_counts, earlier=(), weights=DEFAULT_WEIGHTS, community=None, preferences=None):
    """Rank one item against one gap. Higher wins. None means it doesn't fit.

    `earlier` is the minutes of each sitting of this item already planned on
    other days of the week being planned. `community` is community_ratings()
    and `preferences` time_of_day_preferences() for the user. See
    ScoringWeights for the numbers.
    """
    remaining = max(0, item.remaining_minutes - sum(earlier))
    minutes = item.session_minutes_for(gap.usable, remaining=remaining)
    if minutes is None:
        return None

    score = item.priority * weights.per_priority_step

    if item.status == BacklogItem.Status.IN_PROGRESS:
        score += weights.in_progress_bonus

    score += (minutes / gap.usable) * weights.gap_fill_bonus

    if community and item.catalog_item_id in community:
        score += weights.community_bonus * (community[item.catalog_item_id] - 3) / 2

    if preferences:
        score += weights.time_of_day_bonus * preferences.get((item.media_type, part_of_day(gap.start)), 0)

    if not item.rules["chunkable"]:
        score += weights.whole_film_bonus

    if minutes >= remaining:
        score += weights.finishes_item_bonus

    score -= weights.variety_penalty * len(earlier)
    score -= weights.rejection_penalty * rejected_counts.get(item.pk, 0)
    return score, minutes, remaining


def _reason_for(item, gap, minutes, remaining):
    gap_text = humanize_minutes(gap.usable)
    if not item.rules["chunkable"]:
        return f"You have {gap_text} free and its {humanize_minutes(remaining)} runtime fits in one sitting."
    if minutes >= remaining:
        return f"{gap_text} free is just enough to finish it."
    units = item.units_label(minutes)
    if units:
        return f"{gap_text} free, so {units} fit comfortably."
    return f"{gap_text} free, so {item.rules['unit']} of this is a comfortable fit."


# How a long gap is shared out. Once the best item has taken its sitting at
# the start of a gap, whatever is left past a short break goes back into the
# pool as a gap of its own, so a free Saturday gets more than one proposal
# instead of one chapter and eleven empty hours. The remainder has to be worth
# sitting down for again, which is more than the bare MIN_USABLE_MINUTES a
# gap needs to exist at all.
SESSION_BREAK_MINUTES = 15
SPLIT_MIN_REMAINDER_MINUTES = 45


def _remainder_gap(gap, minutes):
    """What is left of `gap` after a sitting of `minutes` at its start and a break, or None.

    A sitting that ran past midnight leaves nothing: the rest of the night
    belongs to the next date.
    """
    start = _to_minutes(gap.start) + minutes + SESSION_BREAK_MINUTES
    end = END_OF_DAY if gap.end == time(23, 59) else _to_minutes(gap.end)
    if end - start < SPLIT_MIN_REMAINDER_MINUTES:
        return None
    return Interval(_from_minutes(start), _from_minutes(end), extension=gap.extension)


def _pool_key(gap):
    # Biggest last, so pop() takes it, counting any run past midnight, since
    # that is what a sitting started there can use. On a tie the earlier gap
    # comes out first.
    return (gap.usable, -_to_minutes(gap.start))


def suggest_for_day(user, day=None, limit=3, persist=True, planned=None, weights=None):
    """Pick something for each free gap, no repeats.

    A gap gets the best-scoring item at its start. If enough of the gap is
    left after that sitting and a break, the rest is treated as a new gap and
    can get a suggestion of its own, up to `limit` for the day.

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
        .filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=day))
    )
    if not candidates:
        return []

    rejected_counts = recent_rejection_counts(user, day, weights.rejection_memory_days)
    community = community_ratings(user) if weights.community_bonus else {}
    preferences = (
        time_of_day_preferences(user, day, weights.time_of_day_memory_days, weights.time_of_day_min_answers)
        if weights.time_of_day_bonus
        else {}
    )

    # Don't offer something that's already sitting on the same day. Rejected
    # ones are excluded so a refresh can legitimately bring them back.
    used_items = set(
        user.suggestions.filter(date=day)
        .exclude(status=Suggestion.Status.REJECTED)
        .values_list("backlog_item_id", flat=True)
    )

    suggestions = []

    # Biggest gaps first. They have the most options, so spend them deliberately
    # instead of letting a short gap take the item a long one needed. What is
    # left of a gap after a sitting goes back in, so the pool is kept sorted.
    pool = sorted(gaps, key=_pool_key)
    while pool and len(suggestions) < limit:
        gap = pool.pop()

        best = None
        for item in candidates:
            if item.pk in used_items:
                continue
            scored = _score(item, gap, rejected_counts, planned.get(item.pk, ()), weights, community, preferences)
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
            end_time=_end_time_for(_to_minutes(gap.start), minutes),
            minutes=minutes,
            reason=_reason_for(item, gap, minutes, remaining),
        )
        if persist:
            suggestion.save()
        suggestions.append(suggestion)
        used_items.add(item.pk)

        leftover = _remainder_gap(gap, minutes)
        if leftover is not None:
            insort(pool, leftover, key=_pool_key)

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


def stale_sittings(user, now=None, limit=10):
    """Accepted sittings whose time has passed and that were never marked done.

    Yesterday's and older always count. Today's count once their end time is
    behind us; one that runs past midnight is not stale until tomorrow.
    """
    now = timezone.localtime(now or timezone.now())
    today = now.date()
    rows = user.suggestions.filter(status=Suggestion.Status.ACCEPTED).filter(
        Q(date__lt=today) | Q(date=today, end_time__lte=now.time(), end_time__gte=F("start_time"))
    )
    return list(rows.select_related("backlog_item").order_by("-date", "-start_time")[:limit])


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


def _commitments_by_day(user, grid_start, days):
    """{date: [(commitment, minutes on that date), ...]} over a run of days.

    One query for the whole run. A commitment that straddles midnight is
    listed on each date it touches, with only the minutes that fall on that
    date, the same clipping busy_blocks does for one day.
    """
    grid_end = grid_start + timedelta(days=days)
    range_start = timezone.make_aware(datetime.combine(grid_start, time.min))
    range_end = timezone.make_aware(datetime.combine(grid_end, time.min))
    by_day = {}
    for commitment in user.commitments.filter(starts_at__lt=range_end, ends_at__gt=range_start):
        local_start = timezone.localtime(commitment.starts_at)
        local_end = timezone.localtime(commitment.ends_at)
        day = max(local_start.date(), grid_start)
        while day < grid_end and timezone.make_aware(datetime.combine(day, time.min)) < local_end:
            start = _to_minutes(local_start.time()) if local_start.date() == day else 0
            end = _to_minutes(local_end.time()) if local_end.date() == day else END_OF_DAY
            by_day.setdefault(day, []).append((commitment, max(0, end - start)))
            day += timedelta(days=1)
    return by_day


def month_grid(user, year, month):
    """Six weeks of cells for the calendar page, Sunday first.

    Each cell carries the day's planned sittings and its commitments as
    `entries`, commitments first, and a `busy_label` that sums recurring
    blocks and commitments, so the grid reads as how busy each day is and
    not only as what was planned from the backlog.
    """
    first = date_cls(year, month, 1)
    # weekday() is Monday=0, and we want Sunday in column 0.
    lead = (first.weekday() + 1) % 7
    grid_start = first - timedelta(days=lead)

    # One query each for the whole grid rather than one per cell.
    planned_by_day = {}
    for suggestion in user.suggestions.filter(
        date__gte=grid_start,
        date__lt=grid_start + timedelta(days=42),
        status__in=[Suggestion.Status.ACCEPTED, Suggestion.Status.DONE],
    ).select_related("backlog_item"):
        planned_by_day.setdefault(suggestion.date, []).append(suggestion)
    commitments_by_day = _commitments_by_day(user, grid_start, 42)
    recurring_minutes = Counter()
    recurring_count = Counter()
    for block in user.recurring_blocks.all():
        recurring_minutes[block.weekday] += _to_minutes(block.end_time) - _to_minutes(block.start_time)
        recurring_count[block.weekday] += 1

    today = timezone.localdate()
    weeks = []
    for week_index in range(6):
        week = []
        for day_index in range(7):
            day = grid_start + timedelta(days=week_index * 7 + day_index)
            planned = planned_by_day.get(day, [])
            commitments = commitments_by_day.get(day, [])
            busy = recurring_minutes[day.weekday()] + sum(minutes for _, minutes in commitments)
            entries = [{"kind": "commitment", "title": c.title} for c, _ in commitments]
            entries += [{"kind": "planned", "title": s.backlog_item.title} for s in planned]
            week.append(
                {
                    "date": day,
                    "in_month": day.month == month,
                    "is_today": day == today,
                    "planned": planned,
                    "commitments": [c for c, _ in commitments],
                    "entries": entries,
                    "busy_minutes": busy,
                    "busy_label": humanize_minutes(busy) if busy else "",
                    "recurring_count": recurring_count[day.weekday()],
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
    """Numbers for the progress page, from sittings marked done and time logged by hand.

    Time comes from Suggestion rows with status done, on the day they were
    marked done, and from TimeLog rows on their own date. Minutes typed
    straight into a backlog item's "already done" counter have no date, so
    they still can't be placed on a day and are not counted here.
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
    entries = [
        (timezone.localtime(responded_at).date() if responded_at else planned_date, minutes, media_type)
        for responded_at, planned_date, minutes, media_type in done
    ]
    entries += list(user.time_logs.values_list("date", "minutes", "backlog_item__media_type"))
    for day, minutes, media_type in entries:
        if day > today:
            continue
        per_day[day] += minutes
        total_minutes += minutes
        if day >= month_start:
            month_minutes_by_type[media_type] += minutes
            month_sessions += 1

    current_streak, best_streak = _streaks({day for day, minutes in per_day.items() if minutes > 0}, today)

    # This week, Monday to today, against the goal if there is one.
    week_start = today - timedelta(days=today.weekday())
    week_minutes = sum(per_day[week_start + timedelta(days=offset)] for offset in range((today - week_start).days + 1))
    profile = getattr(user, "profile", None)
    weekly_goal = profile.weekly_goal_minutes if profile else 0

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
        "week_minutes": week_minutes,
        "week_time": humanize_minutes(week_minutes),
        "weekly_goal": weekly_goal,
        "weekly_goal_time": humanize_minutes(weekly_goal) if weekly_goal else "",
        "goal_percent": min(100, round(week_minutes / weekly_goal * 100)) if weekly_goal else 0,
        "goal_met": bool(weekly_goal) and week_minutes >= weekly_goal,
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


# ------------------------------------------------------------ first use

# The getting-started checklist on Today, in the order a new account should do
# them. Each step is done when the data says so, not when a box is ticked, so
# someone who skips ahead is credited for it.
WALKTHROUGH_STEPS = [
    {
        "key": "schedule",
        "title": "Tell us when you're busy",
        "detail": "Add a class, a shift or a standing plan on My week. Everything left over is free time.",
        "url_name": "weekly_schedule",
        "link": "Open My week",
    },
    {
        "key": "backlog",
        "title": "Put something on your backlog",
        "detail": "Pick a film, show, book or game from Discover, or add one by hand.",
        "url_name": "discover",
        "link": "Browse Discover",
    },
    {
        "key": "suggest",
        "title": "Ask for a suggestion",
        "detail": "Press Suggest something on Today, or Plan my week on My week.",
        "url_name": "dashboard",
        "link": "Go to Today",
    },
    {
        "key": "accept",
        "title": "Add one to your calendar",
        "detail": "Add to calendar puts the sitting on your day. Not today eases off that title for a while.",
        "url_name": "dashboard",
        "link": "See today's suggestions",
    },
    {
        "key": "done",
        "title": "Mark it done",
        "detail": "On the day, Today lists what you planned. Mark done logs the time on Progress.",
        "url_name": "dashboard",
        "link": "Go to Today",
    },
]


def walkthrough(user):
    """The checklist for Today, or None once it is finished or hidden.

    Returns {"steps": [...], "done": n, "total": n, "next": step} where each
    step is a WALKTHROUGH_STEPS entry plus "done" and "current".
    """
    profile = getattr(user, "profile", None)
    if profile and profile.walkthrough_dismissed:
        return None

    statuses = set(user.suggestions.values_list("status", flat=True).distinct())
    done_by_key = {
        "schedule": user.recurring_blocks.exists() or user.commitments.exists(),
        "backlog": user.backlog_items.exists(),
        "suggest": bool(statuses),
        "accept": bool(statuses & {Suggestion.Status.ACCEPTED, Suggestion.Status.DONE}),
        "done": Suggestion.Status.DONE in statuses,
    }
    steps = [{**step, "done": done_by_key[step["key"]], "current": False} for step in WALKTHROUGH_STEPS]
    remaining = [step for step in steps if not step["done"]]
    if not remaining:
        return None
    remaining[0]["current"] = True
    return {"steps": steps, "done": len(steps) - len(remaining), "total": len(steps), "next": remaining[0]}


# ------------------------------------------------------------ reviews

def shared_reviews(catalog_item, exclude_user=None, limit=5):
    """Other students' shared reviews of one catalog title, newest first, with a summary.

    Returns {"reviews": [...], "count": n, "average": 4.2 or None,
    "recommend_percent": 80 or None}. The summary counts every shared review;
    the list is capped at `limit`.
    """
    rows = Review.objects.filter(backlog_item__catalog_item=catalog_item, shared=True).select_related(
        "user", "user__profile", "backlog_item"
    )
    if exclude_user is not None:
        rows = rows.exclude(user=exclude_user)
    totals = rows.aggregate(count=Count("id"), average=Avg("rating"))
    verdicts = [recommend for recommend in rows.values_list("recommend", flat=True) if recommend is not None]
    return {
        "reviews": list(rows[:limit]),
        "count": totals["count"],
        "average": round(totals["average"], 1) if totals["average"] is not None else None,
        "recommend_percent": round(sum(verdicts) / len(verdicts) * 100) if verdicts else None,
    }


# ------------------------------------------------------------ push reminders

# The same reminders the open-tab poller shows, sent through the browser's
# push service so they arrive with the tab closed. Off unless the VAPID keys
# are configured (settings.PUSH_ENABLED). `manage.py send_push_reminders`
# calls push_due_reminders once a minute.

push_logger = logging.getLogger("planner.push")


def push_payload(reminder):
    """What the service worker shows: title, body, a tag so repeats collapse, and where a click goes."""
    if reminder["minutes_until"]:
        body = f"{reminder['detail']} · starts in {reminder['minutes_until']} min"
    else:
        body = f"{reminder['detail']} · starting now"
    return {"title": reminder["title"], "body": body, "tag": reminder["key"], "url": "/dashboard/"}


def _webpush_send(subscription, payload):
    """Send one payload to one subscription. Returns "sent", "gone" or "error"."""
    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription.subscription_info(),
            data=payload,
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": f"mailto:{settings.VAPID_CLAIMS_EMAIL}"},
            ttl=15 * 60,
        )
        return "sent"
    except WebPushException as error:
        status = getattr(getattr(error, "response", None), "status_code", None)
        if status in (404, 410):
            # The browser unsubscribed or the subscription expired.
            return "gone"
        push_logger.warning("Push to %s failed: %s", subscription, error)
        return "error"
    except Exception as error:  # noqa: BLE001 - one bad subscription must not stop the rest
        push_logger.warning("Push to %s failed: %s", subscription, error)
        return "error"


def push_due_reminders(now=None, send=None):
    """Push every reminder now inside its lead window to every subscribed browser, once.

    Returns a Counter of outcomes: sent, gone, error, and users (how many
    users had something due). A reminder is recorded as sent for the user
    whatever the outcome, so a broken subscription does not retry every
    minute until the event starts; the open-tab poller still covers them.
    `send` can replace the real sender in tests.
    """
    counts = Counter()
    if not settings.PUSH_ENABLED:
        return counts
    send = send or _webpush_send
    now = timezone.localtime(now or timezone.now())

    from django.contrib.auth import get_user_model

    users = (
        get_user_model()
        .objects.filter(profile__notify_enabled=True, push_subscriptions__isnull=False)
        .distinct()
        .select_related("profile")
    )
    for user in users:
        reminders = upcoming_reminders(user, now=now)
        if not reminders:
            continue
        already = set(
            user.sent_reminders.filter(key__in=[r["key"] for r in reminders]).values_list("key", flat=True)
        )
        due = [r for r in reminders if r["key"] not in already]
        if not due:
            continue
        counts["users"] += 1
        subscriptions = list(user.push_subscriptions.all())
        for reminder in due:
            payload = json.dumps(push_payload(reminder))
            for subscription in list(subscriptions):
                outcome = send(subscription, payload)
                counts[outcome] += 1
                if outcome == "gone":
                    subscription.delete()
                    subscriptions.remove(subscription)
                elif outcome == "sent":
                    subscription.last_sent_at = now
                    subscription.failures = 0
                    subscription.save(update_fields=["last_sent_at", "failures"])
                else:
                    subscription.failures += 1
                    subscription.save(update_fields=["failures"])
            SentReminder.objects.get_or_create(user=user, key=reminder["key"])

    # Keys carry the start time, so a row is useless once the event is behind us.
    SentReminder.objects.filter(sent_at__lt=now - timedelta(days=2)).delete()
    return counts


# ------------------------------------------------------------ series

def next_in_series(catalog_item, user=None, limit=3):
    """Other titles in the same series, in order, not yet on `user`'s backlog.

    Order is series_order when set, else release date. Titles before this
    one are left out when the order is known, so finishing book two offers
    book three, not book one.
    """
    if not catalog_item or not catalog_item.series:
        return []
    from .models import CatalogItem

    rows = CatalogItem.objects.filter(series=catalog_item.series, media_type=catalog_item.media_type).exclude(pk=catalog_item.pk)
    if user is not None:
        owned = user.backlog_items.filter(catalog_item__isnull=False).values_list("catalog_item_id", flat=True)
        rows = rows.exclude(pk__in=owned)
    rows = list(rows)

    def position(item):
        if item.series_order is not None:
            return (0, item.series_order)
        return (1, item.release_date or date_cls.max)

    if catalog_item.series_order is not None:
        rows = [item for item in rows if item.series_order is None or item.series_order > catalog_item.series_order]
    elif catalog_item.release_date:
        rows = [item for item in rows if item.release_date is None or item.release_date > catalog_item.release_date]
    return sorted(rows, key=position)[:limit]


# ------------------------------------------------------------ semester view

def semester_overview(user, start=None, weeks=16):
    """Busy hours per week for the coming term, from the recurring schedule, exceptions and commitments.

    Returns {"weeks": [...], "busiest": week or None, "lightest": week or None,
    "average_minutes": n} where each week is {"start", "end", "minutes",
    "label", "days": [{date, minutes, label}], "commitments": [...],
    "is_current", "share"} and `share` is minutes over the busiest week's
    minutes as a percentage, for a bar. Planned sittings are not counted:
    this is about the fixed shape of the term, not what was planned into it.
    """
    today = timezone.localdate()
    start = start or today - timedelta(days=today.weekday())  # this Monday
    total_days = weeks * 7
    end = start + timedelta(days=total_days)

    recurring = {}
    for block in user.recurring_blocks.all():
        recurring.setdefault(block.weekday, []).append(block)
    skipped = set(user.block_exceptions.filter(date__gte=start, date__lt=end).values_list("block_id", "date"))
    by_day = _commitments_by_day(user, start, total_days)

    result = []
    for week_index in range(weeks):
        week_start = start + timedelta(days=week_index * 7)
        days = []
        commitments = []
        for offset in range(7):
            day = week_start + timedelta(days=offset)
            minutes = sum(
                _to_minutes(block.end_time) - _to_minutes(block.start_time)
                for block in recurring.get(day.weekday(), [])
                if (block.pk, day) not in skipped
            )
            for commitment, clipped in by_day.get(day, []):
                minutes += clipped
                if commitment not in commitments:
                    commitments.append(commitment)
            days.append({"date": day, "minutes": minutes, "label": humanize_minutes(minutes) if minutes else ""})
        total = sum(entry["minutes"] for entry in days)
        result.append(
            {
                "start": week_start,
                "end": week_start + timedelta(days=6),
                "minutes": total,
                "label": humanize_minutes(total) if total else "nothing",
                "days": days,
                "commitments": commitments,
                "is_current": week_start <= today <= week_start + timedelta(days=6),
            }
        )

    top = max((week["minutes"] for week in result), default=0)
    for week in result:
        week["share"] = round(week["minutes"] / top * 100) if top else 0
    with_time = [week for week in result if week["minutes"]]
    return {
        "weeks": result,
        "busiest": max(with_time, key=lambda w: w["minutes"]) if with_time else None,
        "lightest": min(with_time, key=lambda w: w["minutes"]) if with_time else None,
        "average_minutes": round(sum(w["minutes"] for w in result) / len(result)) if result else 0,
        "average_label": humanize_minutes(round(sum(w["minutes"] for w in result) / len(result))) if result else "0 min",
        "start": start,
        "end": end - timedelta(days=1),
    }


# ------------------------------------------------------------ watch together

def shared_slots(host, guest, minutes, start=None, days=14, limit=6):
    """Stretches in the next `days` when both people are free for `minutes`.

    Intersects each date's free gaps of both users (planned sittings count
    as busy for each). Returns [(date, start_time, end_time)] earliest
    first, up to `limit`, each the start of the shared stretch.
    """
    start = start or timezone.localdate()
    found = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        mine = free_intervals(host, day)
        if not mine:
            continue
        theirs = free_intervals(guest, day)
        for gap in mine:
            for other in theirs:
                lo = max(_to_minutes(gap.start), _to_minutes(other.start))
                hi = min(
                    END_OF_DAY if gap.end == time(23, 59) else _to_minutes(gap.end),
                    END_OF_DAY if other.end == time(23, 59) else _to_minutes(other.end),
                )
                if hi - lo >= minutes:
                    found.append((day, _from_minutes(lo), _end_time_for(lo, minutes)))
                    if len(found) >= limit:
                        return found
    return found


def accept_invite(invite):
    """Put the sitting on both calendars. Returns the guest's backlog item."""
    from .models import BacklogItem, WatchInvite

    now = timezone.now()
    for person in (invite.host, invite.guest):
        item = None
        if invite.catalog_item_id:
            item, _ = BacklogItem.objects.get_or_create(
                user=person,
                catalog_item=invite.catalog_item,
                defaults={
                    "title": invite.title,
                    "media_type": invite.media_type,
                    "estimated_minutes": invite.catalog_item.typical_minutes,
                },
            )
        if item is None:
            item = person.backlog_items.filter(title__iexact=invite.title, media_type=invite.media_type).first()
        if item is None:
            item = BacklogItem.objects.create(
                user=person, title=invite.title, media_type=invite.media_type, estimated_minutes=invite.minutes
            )
        if item.status == BacklogItem.Status.BACKLOG:
            item.status = BacklogItem.Status.IN_PROGRESS
            item.save(update_fields=["status"])
        other = invite.guest if person == invite.host else invite.host
        Suggestion.objects.create(
            user=person,
            backlog_item=item,
            date=invite.date,
            start_time=invite.start_time,
            end_time=invite.end_time,
            minutes=invite.minutes,
            reason=f"Watching together with {other.username}.",
            status=Suggestion.Status.ACCEPTED,
            responded_at=now,
        )
    invite.status = WatchInvite.Status.ACCEPTED
    invite.responded_at = now
    invite.save(update_fields=["status", "responded_at"])


# ------------------------------------------------------------ api tokens

def make_api_token(user, name):
    """A fresh token for `user`. Returns (token row, the raw token to show once)."""
    import hashlib
    import secrets

    from .models import ApiToken

    raw = "mf_" + secrets.token_urlsafe(32)
    token = ApiToken.objects.create(
        user=user, name=name[:60], prefix=raw[:8], key_hash=hashlib.sha256(raw.encode()).hexdigest()
    )
    return token, raw


def user_for_api_token(raw):
    """The user a bearer token belongs to, or None. Touches last_used_at."""
    import hashlib

    from .models import ApiToken

    if not raw or not raw.startswith("mf_"):
        return None
    token = (
        ApiToken.objects.filter(key_hash=hashlib.sha256(raw.encode()).hexdigest())
        .select_related("user")
        .first()
    )
    if token is None or not token.user.is_active:
        return None
    ApiToken.objects.filter(pk=token.pk).update(last_used_at=timezone.now())
    return token.user
