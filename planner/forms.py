from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils import timezone

from .models import BacklogItem, Commitment, Profile, RecurringBlock, Review, TimeLog

# Scoped to UTA students, per the inception deck. Checked at signup only --
# we don't verify the address actually exists, that's an iteration 2 problem.
UTA_EMAIL_DOMAIN = "mavs.uta.edu"


class RegisterForm(UserCreationForm):
    email = forms.EmailField(
        required=True,
        label="UTA email",
        help_text=f"Must be your @{UTA_EMAIL_DOMAIN} address.",
        widget=forms.EmailInput(attrs={"placeholder": f"you@{UTA_EMAIL_DOMAIN}"}),
    )

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ["username", "email"]

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if not email.endswith(f"@{UTA_EMAIL_DOMAIN}"):
            raise forms.ValidationError(
                f"MediaFlow is open to UTA students. Please sign up with your @{UTA_EMAIL_DOMAIN} address."
            )
        if get_user_model().objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account already uses that email.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if commit:
            user.save()
            Profile.objects.get_or_create(user=user)
        return user


class ProfileForm(forms.ModelForm):
    """Waking hours. These bound the window free time is searched in.

    A bedtime earlier in the clock than the wake-up time is read as past
    midnight: up by 10:00 and in bed by 02:00 is a sixteen hour day that ends
    in the small hours of the next date. Only the same time twice is refused,
    since that is either no day at all or all of it.
    """

    class Meta:
        model = Profile
        fields = ["day_start", "day_end"]
        labels = {"day_start": "I am usually up by", "day_end": "I am usually in bed by"}
        help_texts = {"day_end": "A bedtime earlier than your wake-up time, like 2:00 AM, counts as after midnight."}
        widgets = {
            "day_start": forms.TimeInput(attrs={"type": "time"}),
            "day_end": forms.TimeInput(attrs={"type": "time"}),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("day_start"), cleaned.get("day_end")
        if start and end and start == end:
            raise forms.ValidationError("Your bedtime and wake-up time can't be the same time.")
        return cleaned


class ScheduleImportForm(forms.Form):
    file = forms.FileField(label="Calendar file (.ics)", help_text="Up to 5 MB.")


class BacklogImportForm(forms.Form):
    file = forms.FileField(label="CSV export", help_text="watchlist.csv from Letterboxd, or the Goodreads library export. Up to 5 MB.")
    status = forms.ChoiceField(
        label="These are things I",
        choices=[
            (BacklogItem.Status.BACKLOG, "want to get to"),
            (BacklogItem.Status.COMPLETED, "have already finished"),
        ],
        initial=BacklogItem.Status.BACKLOG,
        help_text="Goodreads shelves and Letterboxd ratings override this per row.",
    )


class WatchInviteForm(forms.Form):
    """Step one of Watch together: who, and a note."""

    guest = forms.CharField(label="Classmate's username", max_length=150, widget=forms.TextInput(attrs={"placeholder": "their MediaFlow username", "autocomplete": "off"}))
    message = forms.CharField(label="A note (optional)", max_length=200, required=False, widget=forms.TextInput(attrs={"placeholder": "Popcorn is on me"}))

    def __init__(self, *args, host, **kwargs):
        super().__init__(*args, **kwargs)
        self.host = host

    def clean_guest(self):
        username = self.cleaned_data["guest"].strip().lstrip("@")
        user = get_user_model().objects.filter(username__iexact=username).first()
        if user is None:
            raise forms.ValidationError("No MediaFlow account with that username.")
        if user == self.host:
            raise forms.ValidationError("That is you. Pick a classmate.")
        profile = getattr(user, "profile", None)
        if not (user.is_staff or (profile and profile.email_verified)):
            raise forms.ValidationError("That account has not confirmed its email yet.")
        return user


class ApiTokenForm(forms.Form):
    name = forms.CharField(label="What is this token for?", max_length=60, widget=forms.TextInput(attrs={"placeholder": "Phone widget"}))


class DeleteAccountForm(forms.Form):
    """Type the password again before the account goes."""

    password = forms.CharField(label="Your password", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))
    confirm = forms.BooleanField(label="I understand this cannot be undone")

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean_password(self):
        password = self.cleaned_data["password"]
        if not self.user.check_password(password):
            raise forms.ValidationError("That is not your password.")
        return password


class DisplayNameForm(forms.ModelForm):
    """The name other students see on a shared review. Blank falls back to the username."""

    class Meta:
        model = Profile
        fields = ["display_name"]
        labels = {"display_name": "Sign shared reviews as"}
        widgets = {"display_name": forms.TextInput(attrs={"placeholder": "Your username", "maxlength": 40})}

    def clean_display_name(self):
        return self.cleaned_data["display_name"].strip()


class WeeklyGoalForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ["weekly_goal_minutes"]
        labels = {"weekly_goal_minutes": "Minutes a week"}
        help_texts = {"weekly_goal_minutes": "Monday to Sunday. 0 turns the goal off. 300 is five hours."}
        widgets = {"weekly_goal_minutes": forms.NumberInput(attrs={"min": 0, "max": 10080, "step": 15})}

    def clean_weekly_goal_minutes(self):
        value = self.cleaned_data["weekly_goal_minutes"]
        if value > 7 * 24 * 60:
            raise forms.ValidationError("There are only 10,080 minutes in a week.")
        return value


class NotificationSettingsForm(forms.ModelForm):
    class Meta:
        model = Profile
        fields = ["notify_enabled", "notify_lead_minutes"]
        labels = {
            "notify_enabled": "Remind me before things start",
            "notify_lead_minutes": "Minutes of warning",
        }
        help_texts = {"notify_enabled": "", "notify_lead_minutes": "Between 1 and 120."}
        widgets = {"notify_lead_minutes": forms.NumberInput(attrs={"min": 1, "max": 120})}


class BacklogItemForm(forms.ModelForm):
    # Where you are, in episodes or chapters, as an alternative to minutes.
    # Converted to minutes_completed with the item's unit length.
    units_done = forms.IntegerField(
        required=False,
        min_value=0,
        label="Episodes or chapters already finished",
        help_text="Optional. Overrides the minutes above using the length of one episode or chapter.",
    )

    class Meta:
        model = BacklogItem
        fields = [
            "title", "media_type", "priority", "estimated_minutes", "minutes_completed", "unit_minutes", "status", "notes",
        ]
        labels = {
            "estimated_minutes": "Estimated total time (minutes)",
            "minutes_completed": "Minutes already done",
            "unit_minutes": "Minutes per episode, chapter or session",
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "unit_minutes": forms.NumberInput(attrs={"placeholder": "Default: 45 for TV, 25 for books, 60 for games"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Keep the order sensible: the unit fields next to the minutes they explain.
        order = ["title", "media_type", "priority", "estimated_minutes", "unit_minutes", "minutes_completed", "units_done", "status", "notes"]
        self.order_fields(order)
        if self.instance.pk and self.instance.units_total is not None:
            self.initial.setdefault("units_done", self.instance.units_done)

    def clean(self):
        cleaned = super().clean()
        total = cleaned.get("estimated_minutes")
        done = cleaned.get("minutes_completed")
        units = cleaned.get("units_done")
        media_type = cleaned.get("media_type")
        if units is not None and media_type and total is not None:
            rules = BacklogItem(media_type=media_type, unit_minutes=cleaned.get("unit_minutes")).rules
            if rules["chunkable"]:
                done = min(total, units * rules["chunk_minutes"])
                cleaned["minutes_completed"] = done
        if total is not None and done is not None and done > total:
            raise forms.ValidationError("Minutes already done cannot exceed the total estimate.")
        return cleaned

    def save(self, commit=True):
        item = super().save(commit=False)
        if "minutes_completed" in self.cleaned_data:
            item.minutes_completed = self.cleaned_data["minutes_completed"]
        if commit:
            item.save()
        return item


class LogTimeForm(forms.ModelForm):
    """Minutes spent on a backlog item on a date, outside a planned sitting."""

    class Meta:
        model = TimeLog
        fields = ["date", "minutes", "note"]
        labels = {"minutes": "Minutes spent", "note": "Note (optional)"}
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "minutes": forms.NumberInput(attrs={"min": 1, "max": 24 * 60, "placeholder": "40"}),
            "note": forms.TextInput(attrs={"placeholder": "On the bus"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        if not self.initial.get("date") and not self.instance.pk:
            self.initial["date"] = timezone.localdate()

    def clean_date(self):
        day = self.cleaned_data["date"]
        if day > timezone.localdate():
            raise forms.ValidationError("You can't log time you haven't spent yet.")
        return day


class PlanSessionForm(forms.Form):
    """Put a backlog item on a day yourself, from the calendar, rather than waiting to be asked.

    The result is an accepted Suggestion, so it shows up and is marked done
    exactly like one the planner proposed. With `moving` set to an accepted
    sitting, the same form moves it instead, and that sitting's own time does
    not count as busy.
    """

    backlog_item = forms.ModelChoiceField(queryset=BacklogItem.objects.none(), label="What", empty_label="Pick something")
    date = forms.DateField(
        label="On", required=False, widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
    )
    start_time = forms.TimeField(label="Starting at", widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    minutes = forms.IntegerField(
        label="For how long (minutes)", min_value=10, max_value=12 * 60, widget=forms.NumberInput(attrs={"placeholder": "45"})
    )

    def __init__(self, *args, user, day, moving=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.day = day
        self.moving = moving
        self.fields["date"].input_formats = ["%Y-%m-%d"]
        self.initial.setdefault("date", day)
        self.fields["backlog_item"].queryset = user.backlog_items.exclude(
            status=BacklogItem.Status.COMPLETED
        ).order_by("title")

    def clean(self):
        cleaned = super().clean()
        start, minutes = cleaned.get("start_time"), cleaned.get("minutes")
        day = cleaned.get("date") or self.day
        cleaned["date"] = day
        if not start or not minutes:
            return cleaned
        start_minutes = start.hour * 60 + start.minute
        end_minutes = start_minutes + minutes
        if end_minutes > 24 * 60:
            raise forms.ValidationError("That runs past midnight. Start earlier or make it shorter.")
        cleaned["end_minutes"] = end_minutes

        # Local import: services imports models, forms import services only here.
        from .services import busy_blocks

        exclude = self.moving.pk if self.moving is not None else None
        for block in busy_blocks(self.user, day, exclude_suggestion=exclude):
            block_start = block.start.hour * 60 + block.start.minute
            block_end = block.end.hour * 60 + block.end.minute
            if block_start < end_minutes and start_minutes < block_end:
                raise forms.ValidationError(
                    f"That overlaps {block.label} ({block.start.strftime('%I:%M %p').lstrip('0')} to "
                    f"{block.end.strftime('%I:%M %p').lstrip('0')}). Pick a free stretch."
                )
        return cleaned


class RecurringBlockForm(forms.ModelForm):
    class Meta:
        model = RecurringBlock
        fields = ["label", "kind", "weekday", "start_time", "end_time"]
        labels = {"label": "What is it?"}
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}),
            "end_time": forms.TimeInput(attrs={"type": "time"}),
            "label": forms.TextInput(attrs={"placeholder": "CSE 3311 lecture"}),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if start and end and start >= end:
            raise forms.ValidationError("End time must be after start time.")
        return cleaned


class CommitmentForm(forms.ModelForm):
    class Meta:
        model = Commitment
        fields = ["title", "starts_at", "ends_at"]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "title": forms.TextInput(attrs={"placeholder": "Group project meeting"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # datetime-local posts back as 2026-09-19T14:30, which Django won't
        # parse unless we say so.
        for field in ("starts_at", "ends_at"):
            self.fields[field].input_formats = ["%Y-%m-%dT%H:%M"]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("starts_at"), cleaned.get("ends_at")
        if start and end and start >= end:
            raise forms.ValidationError("End time must be after start time.")
        return cleaned


class ReviewForm(forms.ModelForm):
    # Declared here so the picker is exactly five stars, with no blank option.
    rating = forms.TypedChoiceField(
        label="Rating out of 5", choices=Review.RATING_CHOICES, coerce=int, widget=forms.RadioSelect
    )
    # The app's own question, asked straight. Stored as True/False on the model.
    recommend = forms.TypedChoiceField(
        label="Was it worth the time?",
        choices=[("yes", "Yes, worth it"), ("no", "Not really")],
        coerce=lambda value: value == "yes",
        widget=forms.RadioSelect,
    )

    class Meta:
        model = Review
        fields = ["rating", "recommend", "headline", "body", "contains_spoilers", "shared", "anonymous"]
        labels = {
            "rating": "Rating out of 5",
            "headline": "In one line",
            "body": "What did you think?",
            "contains_spoilers": "This review has spoilers",
            "shared": "Share with other students",
            "anonymous": "Don't show my name",
        }
        help_texts = {
            "headline": "",
            "shared": "Shown on the catalog page for this title, signed with your display name.",
            "anonymous": f"Shared as \"{Review.ANONYMOUS_NAME}\" instead.",
        }
        widgets = {
            "rating": forms.RadioSelect,
            "headline": forms.TextInput(attrs={"placeholder": "Slow start, worth sticking with"}),
            "body": forms.Textarea(attrs={"rows": 5, "placeholder": "What worked, what didn't, who it's for."}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A saved yes/no comes back as a bool; the radio needs its string.
        if self.instance.pk and self.instance.recommend is not None:
            self.initial["recommend"] = "yes" if self.instance.recommend else "no"


class CatalogSearchForm(forms.Form):
    """Discover page filters. Not a ModelForm -- nothing here gets saved."""

    SORT_CHOICES = [
        ("title", "Title A-Z"),
        ("-rating", "Highest rated"),
        ("-release_date", "Newest first"),
        ("typical_minutes", "Shortest first"),
    ]

    q = forms.CharField(
        required=False, label="Search", widget=forms.TextInput(attrs={"placeholder": "Title, creator, or keyword"})
    )
    media_type = forms.ChoiceField(required=False, label="Type")
    genre = forms.ChoiceField(required=False, label="Genre")
    max_minutes = forms.IntegerField(
        required=False, min_value=1, label="Fits in (minutes)", widget=forms.NumberInput(attrs={"placeholder": "180"})
    )
    sort = forms.ChoiceField(required=False, choices=SORT_CHOICES, label="Sort by")

    def __init__(self, *args, media_types=(), genres=(), **kwargs):
        # Genres come from whatever is actually in the catalog, so the choices
        # are built here instead of being hardcoded on the field.
        super().__init__(*args, **kwargs)
        self.fields["media_type"].choices = [("", "Any type")] + list(media_types)
        self.fields["genre"].choices = [("", "Any genre")] + [(g, g) for g in genres]
