from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import AvailabilityWindow, Commitment, MediaItem


class RegisterForm(UserCreationForm):
    pass


class MediaItemForm(forms.ModelForm):
    class Meta:
        model = MediaItem
        fields = ["title", "media_type", "priority", "estimated_minutes", "minutes_completed", "status", "notes"]


class AvailabilityWindowForm(forms.ModelForm):
    class Meta:
        model = AvailabilityWindow
        fields = ["weekday", "start_time", "end_time"]
        widgets = {"start_time": forms.TimeInput(attrs={"type": "time"}), "end_time": forms.TimeInput(attrs={"type": "time"})}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("start_time") and cleaned.get("end_time") and cleaned["start_time"] >= cleaned["end_time"]:
            raise forms.ValidationError("End time must be after start time.")
        return cleaned


class CommitmentForm(forms.ModelForm):
    class Meta:
        model = Commitment
        fields = ["title", "starts_at", "ends_at"]
        widgets = {"starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}), "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("starts_at") and cleaned.get("ends_at") and cleaned["starts_at"] >= cleaned["ends_at"]:
            raise forms.ValidationError("End time must be after start time.")
        return cleaned
