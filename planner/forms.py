from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm

from .models import BacklogItem, Commitment, Profile, RecurringBlock, Review

# The team scoped this to UTA students, enforced at signup.
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
    class Meta:
        model = Profile
        fields = ["day_start", "day_end"]
        labels = {"day_start": "I am usually up by", "day_end": "I am usually in bed by"}
        widgets = {
            "day_start": forms.TimeInput(attrs={"type": "time"}),
            "day_end": forms.TimeInput(attrs={"type": "time"}),
        }

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("day_start"), cleaned.get("day_end")
        if start and end and start >= end:
            raise forms.ValidationError("Your bedtime needs to be after your wake-up time.")
        return cleaned


class BacklogItemForm(forms.ModelForm):
    class Meta:
        model = BacklogItem
        fields = ["title", "media_type", "priority", "estimated_minutes", "minutes_completed", "status", "notes"]
        labels = {
            "estimated_minutes": "Estimated total time (minutes)",
            "minutes_completed": "Minutes already done",
        }
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def clean(self):
        cleaned = super().clean()
        total = cleaned.get("estimated_minutes")
        done = cleaned.get("minutes_completed")
        if total is not None and done is not None and done > total:
            raise forms.ValidationError("Minutes already done cannot exceed the total estimate.")
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
        for field in ("starts_at", "ends_at"):
            self.fields[field].input_formats = ["%Y-%m-%dT%H:%M"]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("starts_at"), cleaned.get("ends_at")
        if start and end and start >= end:
            raise forms.ValidationError("End time must be after start time.")
        return cleaned


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ["rating", "body"]
        labels = {"rating": "Rating out of 5", "body": "What did you think?"}
        widgets = {
            "rating": forms.NumberInput(attrs={"min": 1, "max": 5}),
            "body": forms.Textarea(attrs={"rows": 5, "placeholder": "Worth the time?"}),
        }


class CatalogSearchForm(forms.Form):
    """Filters for the discover page. Every field is optional."""

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
        super().__init__(*args, **kwargs)
        self.fields["media_type"].choices = [("", "Any type")] + list(media_types)
        self.fields["genre"].choices = [("", "Any genre")] + [(g, g) for g in genres]
