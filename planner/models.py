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


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    day_start = models.TimeField(default="08:00", help_text="When your day usually starts")
    day_end = models.TimeField(default="23:00", help_text="When you usually go to bed")

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

    class Meta:
        ordering = ["title"]

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

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="backlog_items")
    # Null for anything the user typed in by hand instead of adding from search.
    catalog_item = models.ForeignKey(
        CatalogItem, on_delete=models.SET_NULL, null=True, blank=True, related_name="backlog_entries"
    )
    title = models.CharField(max_length=200)
    media_type = models.CharField(max_length=20, choices=MediaType.choices)
    priority = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    estimated_minutes = models.PositiveIntegerField(help_text="Estimated total time to finish")
    minutes_completed = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.BACKLOG)
    notes = models.TextField(blank=True)
    added_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

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
    def rules(self):
        return SESSION_RULES.get(self.media_type, SESSION_RULES[MediaType.BOOK])

    def session_minutes_for(self, available_minutes):
        """Length of sitting to propose in a gap this size, or None if it won't fit."""
        rules = self.rules
        remaining = self.remaining_minutes
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

    def __str__(self):
        return f"{self.backlog_item.title} on {self.date}"


class Review(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews")
    backlog_item = models.OneToOneField(BacklogItem, on_delete=models.CASCADE, related_name="review")
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    body = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def star_range(self):
        # Templates can't do range(), so hand them something to loop over.
        return range(self.rating)

    def __str__(self):
        return f"{self.backlog_item.title} - {self.rating}/5"
