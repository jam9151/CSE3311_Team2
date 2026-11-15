import math
from datetime import time

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils.text import slugify


class MediaType(models.TextChoices):
    MOVIE = "movie", "Movie"
    TV = "tv", "TV series"
    BOOK = "book", "Book"
    GAME = "game", "Game"


# Per-type rules for how we carve an item into sittings.
#
# chunk_minutes is the natural unit (a chapter, an episode). max_minutes caps
# one sitting. We need that cap because a free Saturday doesn't mean anyone
# reads for twelve hours straight -- without it the first item picked just
# swallowed the entire day. Movies are the odd one out: they only work if the
# whole runtime fits, so they get no chunking at all.
SESSION_RULES = {
    MediaType.MOVIE: {"chunkable": False, "chunk_minutes": 0, "max_minutes": 0, "unit": "the whole film"},
    MediaType.TV: {"chunkable": True, "chunk_minutes": 45, "max_minutes": 135, "unit": "an episode"},
    MediaType.BOOK: {"chunkable": True, "chunk_minutes": 25, "max_minutes": 90, "unit": "a chapter"},
    MediaType.GAME: {"chunkable": True, "chunk_minutes": 60, "max_minutes": 120, "unit": "a session"},
}

# Anything shorter than this isn't worth suggesting against.
MIN_USABLE_MINUTES = 20

# The panels on Today, in page order. A user can hide any of them; the keys
# are what Profile.hidden_panels stores. The heading and the suggest button
# are not panels and always show.
DASHBOARD_PANELS = [
    ("numbers", "The numbers", "Free time, longest gap, backlog size and what is planned."),
    ("suggestions", "Suggestions for today", "What fits the gaps in your day, waiting for a yes or no."),
    ("planned", "Planned today", "The sittings already on today's calendar."),
    ("followup", "Did these happen?", "Planned sittings whose time has passed and were never marked done."),
    ("invites", "Watch together", "Invitations from classmates, and the ones you sent that are still waiting."),
    ("day", "Your day", "Busy blocks and free gaps, hour by hour."),
    ("backlog", "Backlog", "The first few things on your list."),
    ("upcoming", "Coming up", "Sittings planned for the next few days."),
]
DASHBOARD_PANEL_KEYS = [key for key, _, _ in DASHBOARD_PANELS]


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    # Real time objects, not "08:00" strings: a profile made by get_or_create
    # is used straight away without a reload, and the free-time maths needs
    # .hour and .minute on it.
    day_start = models.TimeField(default=time(8, 0), help_text="When your day usually starts")
    day_end = models.TimeField(default=time(23, 0), help_text="When you usually go to bed")
    email_verified = models.BooleanField(default=False)
    verification_sent_at = models.DateTimeField(null=True, blank=True)
    notify_enabled = models.BooleanField(default=False, help_text="Remind me before planned sessions and commitments")
    notify_lead_minutes = models.PositiveSmallIntegerField(
        default=15,
        validators=[MinValueValidator(1), MaxValueValidator(120)],
        help_text="How many minutes ahead to remind me",
    )
    # The getting-started checklist on Today. It also goes away by itself once
    # every step is done, so this only records an explicit "hide this".
    walkthrough_dismissed = models.BooleanField(default=False)
    # Panels taken off Today, as a comma-separated list of DASHBOARD_PANELS
    # keys. Empty means the full page.
    hidden_panels = models.CharField(max_length=200, blank=True)
    # What other students see on a shared review instead of the username.
    display_name = models.CharField(
        max_length=40, blank=True, help_text="Shown on reviews you share. Leave blank to use your username."
    )
    # A target for the week, Monday to Sunday. Zero means no goal.
    weekly_goal_minutes = models.PositiveIntegerField(
        default=0, help_text="Minutes you want to log each week. Leave at 0 for no goal."
    )

    @property
    def hidden_panel_keys(self):
        return {key for key in self.hidden_panels.split(",") if key in DASHBOARD_PANEL_KEYS}

    def set_hidden_panels(self, keys):
        # Stored in page order so the field reads the same whatever order the
        # boxes were ticked in.
        wanted = set(keys)
        self.hidden_panels = ",".join(key for key in DASHBOARD_PANEL_KEYS if key in wanted)

    @property
    def public_name(self):
        return self.display_name.strip() or self.user.username

    @property
    def is_overnight(self):
        """True when bedtime is past midnight, e.g. up by 10:00, in bed by 02:00."""
        return self.day_end <= self.day_start

    def __str__(self):
        return f"Profile for {self.user.username}"


class CatalogItem(models.Model):
    """Shared catalog everyone searches. Users copy from it into their backlog."""

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    media_type = models.CharField(max_length=20, choices=MediaType.choices)
    genre = models.CharField(max_length=60, blank=True)
    creator = models.CharField(max_length=160, blank=True, help_text="Author, director, or studio")
    description = models.TextField(blank=True)
    release_date = models.DateField(null=True, blank=True)
    rating = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(10)],
        help_text="Aggregate score out of 10",
    )
    typical_minutes = models.PositiveIntegerField(help_text="Typical time to finish, in minutes")
    # Where an imported row came from, so running the import again updates it
    # instead of adding a copy. Blank for anything entered by hand.
    source = models.CharField(max_length=20, blank=True)
    source_id = models.CharField(max_length=60, blank=True)
    # Set when someone edits an imported row in the admin, so the next import
    # refreshes everything else and leaves this one as they left it.
    locked = models.BooleanField(
        default=False, verbose_name="Keep my edits", help_text="Imports will not overwrite this row."
    )
    # The series or collection this belongs to ("Dune", "The Broken Earth"),
    # filled from TMDB collections for films and by hand for anything else.
    # Titles in the same series are offered as "next in the series" once one
    # is finished, in series_order, or by release date when that is blank.
    series = models.CharField(max_length=160, blank=True)
    series_order = models.PositiveSmallIntegerField(null=True, blank=True, help_text="1 for the first, and so on")

    class Meta:
        ordering = ["title"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "source_id"],
                condition=~models.Q(source=""),
                name="one_catalog_row_per_source_id",
            )
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            # Two different media can share a title (Dune the film, Dune the
            # book), so the type goes in the slug and we still count up on ties.
            base = slugify(f"{self.title}-{self.media_type}")[:210]
            slug, counter = base, 2
            while CatalogItem.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("catalog_detail", args=[self.slug])

    @property
    def release_year(self):
        return self.release_date.year if self.release_date else None

    def __str__(self):
        return self.title


class BacklogItem(models.Model):
    class Status(models.TextChoices):
        BACKLOG = "backlog", "Backlog"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        PAUSED = "paused", "Paused"

    PRIORITY_LABELS = {5: "Must play next", 4: "High", 3: "Normal", 2: "Low", 1: "Someday"}
    # The one way a priority is written anywhere in the app: "P4 · High".
    PRIORITY_CHOICES = [(value, f"P{value} · {label}") for value, label in PRIORITY_LABELS.items()]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="backlog_items")
    # Null for anything the user typed in by hand instead of adding from search.
    catalog_item = models.ForeignKey(
        CatalogItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="backlog_entries"
    )
    title = models.CharField(max_length=200)
    media_type = models.CharField(max_length=20, choices=MediaType.choices)
    priority = models.PositiveSmallIntegerField(
        default=3, choices=PRIORITY_CHOICES, validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    estimated_minutes = models.PositiveIntegerField(help_text="Estimated total time to finish")
    minutes_completed = models.PositiveIntegerField(default=0)
    # Length of one episode, chapter or session for this title when it differs
    # from the type's default (a 60-minute drama, a 15-minute chapter book).
    # Drives both the sittings proposed and the "episode 5 of 24" position.
    unit_minutes = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(5), MaxValueValidator(600)],
        help_text="Minutes per episode, chapter or session, if not the default",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.BACKLOG)
    notes = models.TextField(blank=True)
    added_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    # "Later": not offered again before this date, with nothing held against
    # it. The softer cousin of "Not today", which penalises for two weeks.
    snoozed_until = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["-priority", "added_at"]
        constraints = [
            # Stops the same catalog entry being added twice. Hand-typed items
            # have catalog_item = NULL, and the condition keeps those exempt.
            models.UniqueConstraint(
                fields=["user", "catalog_item"],
                condition=models.Q(catalog_item__isnull=False),
                name="one_backlog_entry_per_catalog_item",
            )
        ]

    @property
    def remaining_minutes(self):
        return max(0, self.estimated_minutes - self.minutes_completed)

    @property
    def percent_complete(self):
        if not self.estimated_minutes:
            return 0
        return min(100, round(self.minutes_completed / self.estimated_minutes * 100))

    @property
    def priority_label(self):
        return self.PRIORITY_LABELS.get(self.priority, "Normal")

    @property
    def priority_display(self):
        return f"P{self.priority} · {self.priority_label}"

    UNIT_NAMES = {MediaType.TV: "episode", MediaType.BOOK: "chapter", MediaType.GAME: "session"}

    @property
    def rules(self):
        base = SESSION_RULES.get(self.media_type, SESSION_RULES[MediaType.BOOK])
        if self.unit_minutes and base["chunkable"]:
            return {**base, "chunk_minutes": self.unit_minutes, "unit": f"{self.unit_name} ({self.unit_minutes} min)"}
        return base

    @property
    def unit_name(self):
        """'episode', 'chapter' or 'session'; empty for a film, which is one sitting."""
        return self.UNIT_NAMES.get(self.media_type, "")

    @property
    def units_total(self):
        """How many episodes, chapters or sessions the whole thing is, or None for a film."""
        chunk = self.rules["chunk_minutes"]
        if not self.rules["chunkable"] or not chunk:
            return None
        return max(1, math.ceil(self.estimated_minutes / chunk))

    @property
    def units_done(self):
        chunk = self.rules["chunk_minutes"]
        if self.units_total is None or not chunk:
            return 0
        return min(self.units_total, self.minutes_completed // chunk)

    @property
    def next_unit(self):
        """The number of the next episode or chapter, or None when there is none left."""
        total = self.units_total
        if total is None or self.remaining_minutes == 0:
            return None
        return min(total, self.units_done + 1)

    @property
    def progress_label(self):
        """'Episode 5 of 24 next', 'Chapter 3 of 20 next', or '' for a film or a finished item."""
        if self.next_unit is None:
            return ""
        return f"{self.unit_name.capitalize()} {self.next_unit} of {self.units_total} next"

    def units_label(self, minutes):
        """What a sitting of `minutes` covers: 'episodes 5–7', 'chapter 3', or ''."""
        chunk = self.rules["chunk_minutes"]
        if self.units_total is None or not chunk:
            return ""
        first = self.units_done + 1
        count = max(1, minutes // chunk)
        last = min(self.units_total, first + count - 1)
        if last <= first:
            return f"{self.unit_name} {first}"
        return f"{self.unit_name}s {first}–{last}"

    def session_minutes_for(self, available_minutes, remaining=None):
        """Length of sitting to propose in a gap this size, or None if it won't fit.

        `remaining` lets week planning pass in what is left after the sittings
        already planned on other days. It defaults to the real remaining time.
        """
        rules = self.rules
        remaining = self.remaining_minutes if remaining is None else remaining
        if remaining <= 0 or available_minutes < MIN_USABLE_MINUTES:
            return None

        if not rules["chunkable"]:
            return remaining if remaining <= available_minutes else None

        usable = min(available_minutes, rules["max_minutes"])
        if remaining <= usable:
            # Close enough to the end that we may as well finish it.
            return remaining
        # Otherwise round down to whole chapters/episodes. Stopping mid-chapter
        # isn't a real suggestion.
        whole_chunks = usable // rules["chunk_minutes"]
        return whole_chunks * rules["chunk_minutes"] if whole_chunks else None

    def __str__(self):
        return self.title


class RecurringBlock(models.Model):
    """Something that happens every week: a class, a shift, practice."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    class Kind(models.TextChoices):
        CLASS = "class", "Class"
        WORK = "work", "Work"
        STUDY = "study", "Study"
        OTHER = "other", "Other"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recurring_blocks")
    label = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.CLASS)
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["weekday", "start_time"]

    def __str__(self):
        return f"{self.label} on {self.get_weekday_display()}"


class BlockException(models.Model):
    """A recurring block that does not happen on one date: no class this Thursday, a week off work."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="block_exceptions")
    block = models.ForeignKey(RecurringBlock, on_delete=models.CASCADE, related_name="exceptions")
    date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["block", "date"], name="one_exception_per_block_and_date")]

    def __str__(self):
        return f"{self.block.label} skipped on {self.date}"


class Commitment(models.Model):
    """One-off dated thing, as opposed to a RecurringBlock."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="commitments")
    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()

    class Meta:
        ordering = ["starts_at"]

    def __str__(self):
        return self.title


class TimeLog(models.Model):
    """Minutes logged by hand against a backlog item, on a date.

    Marking a planned sitting done logs time through the Suggestion row. This
    is the other route: "I read for forty minutes on the bus on Tuesday". It
    carries a date so the progress page can chart it, which the plain
    minutes_completed counter on BacklogItem never could.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="time_logs")
    backlog_item = models.ForeignKey(BacklogItem, on_delete=models.CASCADE, related_name="time_logs")
    date = models.DateField()
    minutes = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(24 * 60)])
    note = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.minutes} min on {self.backlog_item.title} ({self.date})"


class SuggestionQuerySet(models.QuerySet):
    def pending(self):
        return self.filter(status=Suggestion.Status.PENDING)

    def accepted(self):
        return self.filter(status=Suggestion.Status.ACCEPTED)


class Suggestion(models.Model):
    """One proposal: this item, in this gap, on this day.

    Accepted ones become the plan and show up on the calendar. Rejected ones
    stay around on purpose -- the scoring reads them back so we stop pushing
    something the user already said no to.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REJECTED = "rejected", "Rejected"
        DONE = "done", "Completed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="suggestions")
    backlog_item = models.ForeignKey(BacklogItem, on_delete=models.CASCADE, related_name="suggestions")
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    minutes = models.PositiveIntegerField()
    reason = models.CharField(max_length=240, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    objects = SuggestionQuerySet.as_manager()

    class Meta:
        ordering = ["date", "start_time"]

    @property
    def is_open(self):
        return self.status == self.Status.PENDING

    @property
    def crosses_midnight(self):
        """True for a sitting that starts one evening and ends in the small hours of the next date."""
        return self.end_time < self.start_time

    def __str__(self):
        return f"{self.backlog_item.title} on {self.date}"


class WatchInvite(models.Model):
    """Two students, one title, one gap that is free for both.

    The host picks a classmate and a time the planner found free for both of
    them; the guest accepts or declines. Accepting puts an accepted sitting
    on both calendars (and the title on the guest's backlog if it is not
    there yet). The title is kept by name so the invite survives the
    catalog row or either backlog entry going away.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        CANCELLED = "cancelled", "Cancelled"

    host = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_invites")
    guest = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="received_invites")
    catalog_item = models.ForeignKey(CatalogItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="invites")
    title = models.CharField(max_length=200)
    media_type = models.CharField(max_length=20, choices=MediaType.choices)
    minutes = models.PositiveIntegerField()
    date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    message = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date", "start_time"]

    def __str__(self):
        return f"{self.title} with {self.guest.username} on {self.date}"


class ApiToken(models.Model):
    """A bearer token for the read-only JSON API. Only the hash is stored; the token is shown once."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=60, help_text="What this token is for: a phone widget, a script")
    prefix = models.CharField(max_length=8)
    key_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}…) for {self.user.username}"


class Review(models.Model):
    """What someone thought once they finished something.

    One per backlog item. A review can be shared, in which case other students
    see it (without the username's backlog) on the catalog page for the same
    title, so the question the app asks, "worth the time?", gets answered by
    people with the same amount of it.
    """

    RATING_CHOICES = [(1, "1"), (2, "2"), (3, "3"), (4, "4"), (5, "5")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews")
    backlog_item = models.OneToOneField(BacklogItem, on_delete=models.CASCADE, related_name="review")
    rating = models.PositiveSmallIntegerField(
        choices=RATING_CHOICES, validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    headline = models.CharField(max_length=120, blank=True, help_text="One line, like a title for the review")
    body = models.TextField(blank=True)
    # The app's own question. Null only for reviews written before it was asked.
    recommend = models.BooleanField(null=True, blank=True, help_text="Worth the time?")
    contains_spoilers = models.BooleanField(default=False)
    # Shown to other students on the catalog page for the same title.
    shared = models.BooleanField(default=True, help_text="Let other students see this review")
    # Shared, but signed "A student" rather than with a name.
    anonymous = models.BooleanField(default=False, help_text="Hide my name on the shared review")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def star_range(self):
        # Templates can't do range(), so hand them something to loop over.
        return range(self.rating)

    @property
    def empty_star_range(self):
        return range(5 - self.rating)

    @property
    def verdict(self):
        if self.recommend is None:
            return ""
        return "Worth the time" if self.recommend else "Not worth the time"

    ANONYMOUS_NAME = "A student"

    @property
    def reviewer_name(self):
        """What other students see as the author of a shared review."""
        if self.anonymous:
            return self.ANONYMOUS_NAME
        profile = getattr(self.user, "profile", None)
        return profile.public_name if profile else self.user.username

    def __str__(self):
        return f"{self.backlog_item.title} - {self.rating}/5"


class PushSubscription(models.Model):
    """One browser that asked for background reminders.

    What the browser's PushManager hands back: an endpoint at the push service
    (Google's, Mozilla's, Apple's) and two keys that encrypt the payload to
    that browser alone. One user can have several, one per browser.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    user_agent = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    failures = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def subscription_info(self):
        return {"endpoint": self.endpoint, "keys": {"p256dh": self.p256dh, "auth": self.auth}}

    def __str__(self):
        return f"{self.user.username} on {self.user_agent[:40] or 'a browser'}"


class SentReminder(models.Model):
    """One row per reminder pushed to a user, so a reminder is pushed once, not once a minute."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_reminders")
    key = models.CharField(max_length=200)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "key"], name="one_push_per_reminder")]

    def __str__(self):
        return self.key
