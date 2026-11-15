"""Bringing in what people already keep elsewhere.

Two readers, no dependencies:

- `read_ics` turns a calendar export (.ics from a UTA schedule, Google
  Calendar, Outlook) into recurring blocks and one-off commitments.
- `read_media_csv` turns a Letterboxd or Goodreads export into backlog rows.

Each returns a plan: what would be created, and what was skipped and why. The
`apply_*` functions write the plan for one user without adding duplicates.
Kept out of views.py so the parsing can be tested on text alone.
"""

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from .models import BacklogItem, CatalogItem, Commitment, MediaType, RecurringBlock, Review

# ---------------------------------------------------------------- calendars

# iCalendar weekday codes, Monday first, matching RecurringBlock.Weekday.
ICS_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
# How far ahead one-off events are taken. Anything later is noise for a planner.
COMMITMENT_HORIZON_DAYS = 365
# Course codes look like CSE 3311 or MATH-2425.
COURSE_CODE = re.compile(r"\b[A-Z]{2,4}[ -]?\d{4}\b")


@dataclass
class SchedulePlan:
    blocks: list = field(default_factory=list)       # (label, kind, weekday, start_time, end_time)
    commitments: list = field(default_factory=list)  # (title, starts_at, ends_at), aware datetimes
    skipped: Counter = field(default_factory=Counter)

    @property
    def empty(self):
        return not self.blocks and not self.commitments


def _unfold(text):
    """Join continuation lines (a line starting with a space or tab belongs to the one before)."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _events(text):
    """Each VEVENT as {NAME: [(params, value), ...]}. Names are upper-cased, params too."""
    events, current = [], None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, _, value = line.partition(":")
        name, *raw_params = head.split(";")
        params = {}
        for param in raw_params:
            key, _, val = param.partition("=")
            params[key.upper()] = val
        current.setdefault(name.upper(), []).append((params, value))
    return events


def _first(event, name):
    values = event.get(name)
    return values[0] if values else ({}, None)


def _unescape(value):
    return (value or "").replace("\\,", ",").replace("\\;", ";").replace("\\n", "\n").replace("\\\\", "\\").strip()


def _parse_when(value, params, default_tz):
    """An aware datetime, the date for an all-day value, or None if unreadable."""
    value = (value or "").strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            return None
    utc = value.endswith("Z")
    core = value[:-1] if utc else value
    try:
        naive = datetime.strptime(core, "%Y%m%dT%H%M%S")
    except ValueError:
        try:
            naive = datetime.strptime(core, "%Y%m%dT%H%M")
        except ValueError:
            return None
    if utc:
        tz = ZoneInfo("UTC")
    elif params.get("TZID"):
        try:
            tz = ZoneInfo(params["TZID"].strip('"'))
        except (ZoneInfoNotFoundError, ValueError):
            tz = default_tz
    else:
        tz = default_tz
    return timezone.localtime(naive.replace(tzinfo=tz))


def _rrule(value):
    parts = {}
    for piece in (value or "").split(";"):
        key, _, val = piece.partition("=")
        if key:
            parts[key.upper()] = val
    return parts


def guess_kind(summary):
    """Class for anything that looks like a course code, work for shifts, study for labs and study groups."""
    text = summary or ""
    lowered = text.lower()
    if COURSE_CODE.search(text) or any(word in lowered for word in ("lecture", "class", "seminar", "recitation")):
        return RecurringBlock.Kind.CLASS
    if any(word in lowered for word in ("shift", "work", "job")):
        return RecurringBlock.Kind.WORK
    if any(word in lowered for word in ("study", "lab", "tutoring", "office hours")):
        return RecurringBlock.Kind.STUDY
    return RecurringBlock.Kind.OTHER


def read_ics(text, now=None):
    """What a calendar file would add. Weekly repeats become recurring blocks, the rest commitments."""
    now = timezone.localtime(now or timezone.now())
    default_tz = timezone.get_current_timezone()
    horizon = now + timedelta(days=COMMITMENT_HORIZON_DAYS)
    plan = SchedulePlan()

    for event in _events(text):
        summary = _unescape(_first(event, "SUMMARY")[1]) or "Busy"
        start_params, start_raw = _first(event, "DTSTART")
        end_params, end_raw = _first(event, "DTEND")
        start = _parse_when(start_raw, start_params, default_tz)
        if start is None:
            plan.skipped["unreadable start time"] += 1
            continue
        if isinstance(start, date) and not isinstance(start, datetime):
            plan.skipped["all-day"] += 1
            continue
        end = _parse_when(end_raw, end_params, default_tz) if end_raw else None
        if end is None or isinstance(end, date) and not isinstance(end, datetime):
            _, duration = _first(event, "DURATION")
            end = start + (_parse_duration(duration) or timedelta(hours=1))
        if end <= start:
            plan.skipped["ends before it starts"] += 1
            continue

        rule = _rrule(_first(event, "RRULE")[1]) if "RRULE" in event else None
        if rule:
            freq = rule.get("FREQ", "").upper()
            if freq not in ("WEEKLY", "DAILY"):
                plan.skipped[f"repeats {freq.lower() or 'oddly'}"] += 1
                continue
            if rule.get("INTERVAL", "1") not in ("", "1"):
                plan.skipped["repeats every few weeks"] += 1
                continue
            until = rule.get("UNTIL")
            if until:
                until_when = _parse_when(until, {}, default_tz)
                until_dt = until_when if isinstance(until_when, datetime) else (
                    datetime.combine(until_when, time.max, tzinfo=default_tz) if until_when else None
                )
                if until_dt and until_dt < now:
                    plan.skipped["already ended"] += 1
                    continue
            if start.date() != end.date():
                plan.skipped["repeats and runs past midnight"] += 1
                continue
            if freq == "DAILY":
                weekdays = list(range(7))
            else:
                codes = [code.strip()[-2:] for code in rule.get("BYDAY", "").split(",") if code.strip()]
                weekdays = sorted({ICS_DAYS[code] for code in codes if code in ICS_DAYS}) or [start.weekday()]
            kind = guess_kind(summary)
            for weekday in weekdays:
                plan.blocks.append((summary[:120], kind, weekday, start.time().replace(second=0, microsecond=0), end.time().replace(second=0, microsecond=0)))
            continue

        if end < now:
            plan.skipped["in the past"] += 1
            continue
        if start > horizon:
            plan.skipped["more than a year away"] += 1
            continue
        plan.commitments.append((summary[:200], start, end))

    return plan


def _parse_duration(value):
    """PT1H30M and the like. Days and weeks too; months and years are not in iCalendar durations."""
    match = re.fullmatch(r"(-)?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", (value or "").strip())
    if not match or not value:
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    total = timedelta(
        weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0), minutes=int(minutes or 0), seconds=int(seconds or 0)
    )
    return -total if sign else total


def apply_schedule(user, plan):
    """Create what the plan says, skipping anything the user already has. Returns a Counter."""
    counts = Counter()
    for label, kind, weekday, start, end in plan.blocks:
        _, created = RecurringBlock.objects.get_or_create(
            user=user, label=label, weekday=weekday, start_time=start, end_time=end, defaults={"kind": kind}
        )
        counts["blocks" if created else "blocks already there"] += 1
    for title, starts_at, ends_at in plan.commitments:
        _, created = Commitment.objects.get_or_create(user=user, title=title, starts_at=starts_at, ends_at=ends_at)
        counts["commitments" if created else "commitments already there"] += 1
    return counts


# ---------------------------------------------------------------- backlogs

# Default lengths when the file says nothing and the catalog has no match.
DEFAULT_FILM_MINUTES = 120
DEFAULT_BOOK_MINUTES = 480
MINUTES_PER_PAGE = 1.3

GOODREADS_SHELVES = {
    "to-read": BacklogItem.Status.BACKLOG,
    "currently-reading": BacklogItem.Status.IN_PROGRESS,
    "read": BacklogItem.Status.COMPLETED,
}


@dataclass
class MediaRow:
    title: str
    media_type: str
    year: int | None = None
    creator: str = ""
    status: str = BacklogItem.Status.BACKLOG
    minutes: int | None = None
    rating: int | None = None  # out of 5, Goodreads only


@dataclass
class MediaPlan:
    source: str  # "letterboxd" | "goodreads"
    rows: list = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def read_media_csv(text, default_status=BacklogItem.Status.BACKLOG):
    """Rows from a Letterboxd or Goodreads export, or None if the file is neither."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = {name.strip() for name in reader.fieldnames or []}

    if "Letterboxd URI" in headers and "Name" in headers:
        plan = MediaPlan("letterboxd")
        for row in reader:
            title = (row.get("Name") or "").strip()
            if not title:
                plan.skipped["no title"] += 1
                continue
            status = default_status
            rating = None
            if row.get("Rating"):
                # ratings.csv and diary.csv: a rating means it was watched.
                status = BacklogItem.Status.COMPLETED
                try:
                    rating = max(1, min(5, round(float(row["Rating"]))))
                except ValueError:
                    rating = None
            plan.rows.append(MediaRow(title[:200], MediaType.MOVIE, _int(row.get("Year")), status=status, rating=rating))
        return plan

    if "Title" in headers and "Author" in headers and ("Exclusive Shelf" in headers or "Bookshelves" in headers):
        plan = MediaPlan("goodreads")
        for row in reader:
            title = (row.get("Title") or "").strip()
            if not title:
                plan.skipped["no title"] += 1
                continue
            shelf = (row.get("Exclusive Shelf") or "").strip().lower()
            status = GOODREADS_SHELVES.get(shelf, default_status)
            pages = _int(row.get("Number of Pages"))
            minutes = round(pages * MINUTES_PER_PAGE) if pages and pages > 0 else None
            rating = _int(row.get("My Rating"))
            plan.rows.append(
                MediaRow(
                    title[:200], MediaType.BOOK, _int(row.get("Original Publication Year") or row.get("Year Published")),
                    creator=(row.get("Author") or "").strip()[:160], status=status, minutes=minutes,
                    rating=rating if rating and 1 <= rating <= 5 else None,
                )
            )
        return plan

    return None


def _match_catalog(row):
    """The catalog row for an imported title, preferring the same year."""
    candidates = list(CatalogItem.objects.filter(media_type=row.media_type, title__iexact=row.title))
    if not candidates:
        return None
    if row.year:
        for item in candidates:
            if item.release_date and item.release_date.year == row.year:
                return item
    return candidates[0]


def apply_media(user, plan):
    """Put the rows on the user's backlog. Returns a Counter of what happened."""
    counts = Counter()
    existing_titles = {
        (title.lower(), media_type)
        for title, media_type in user.backlog_items.values_list("title", "media_type")
    }
    for row in plan.rows:
        catalog = _match_catalog(row)
        if catalog is not None:
            item, created = BacklogItem.objects.get_or_create(
                user=user,
                catalog_item=catalog,
                defaults={
                    "title": catalog.title,
                    "media_type": catalog.media_type,
                    "estimated_minutes": catalog.typical_minutes,
                    "status": row.status,
                },
            )
            counts["matched" if created else "already on your backlog"] += 1
        else:
            if (row.title.lower(), row.media_type) in existing_titles:
                counts["already on your backlog"] += 1
                continue
            default = DEFAULT_FILM_MINUTES if row.media_type == MediaType.MOVIE else DEFAULT_BOOK_MINUTES
            item = BacklogItem.objects.create(
                user=user,
                title=row.title,
                media_type=row.media_type,
                estimated_minutes=row.minutes or default,
                status=row.status,
                notes=f"Imported from {plan.source.capitalize()}" + (f". By {row.creator}" if row.creator else ""),
            )
            existing_titles.add((row.title.lower(), row.media_type))
            counts["added by title"] += 1
            created = True
        if created and row.status == BacklogItem.Status.COMPLETED:
            item.minutes_completed = item.estimated_minutes
            item.completed_at = timezone.now()
            item.save(update_fields=["minutes_completed", "completed_at"])
            if row.rating and not hasattr(item, "review"):
                # Private by default: nobody asked to share a rating made elsewhere.
                Review.objects.create(user=user, backlog_item=item, rating=row.rating, shared=False)
                counts["ratings"] += 1
    return counts
