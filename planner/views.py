from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from .forms import AvailabilityWindowForm, CommitmentForm, MediaItemForm, RegisterForm
from .services import generate_schedule


def home(request):
    return redirect("dashboard") if request.user.is_authenticated else render(request, "planner/home.html")


def register(request):
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("dashboard")
    return render(request, "registration/register.html", {"form": form})


@login_required
def dashboard(request):
    today = timezone.localdate()
    context = {
        "items": request.user.media_items.all(),
        "availability": request.user.availability_windows.all(),
        "commitments": request.user.commitments.filter(ends_at__gte=timezone.now())[:10],
        "sessions": request.user.scheduled_sessions.filter(starts_at__date__gte=today, starts_at__date__lt=today + timedelta(days=7)),
    }
    return render(request, "planner/dashboard.html", context)


def _create(request, form_class):
    form = form_class(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.user = request.user
        obj.save()
        messages.success(request, "Saved successfully.")
        return redirect("dashboard")
    return render(request, "planner/form.html", {"form": form})


@login_required
def add_media(request):
    return _create(request, MediaItemForm)


@login_required
def add_availability(request):
    return _create(request, AvailabilityWindowForm)


@login_required
def add_commitment(request):
    return _create(request, CommitmentForm)


@login_required
def build_schedule(request):
    if request.method == "POST":
        today = timezone.localdate()
        request.user.scheduled_sessions.filter(starts_at__date__gte=today, completed=False).delete()
        count = len(generate_schedule(request.user, today))
        messages.success(request, f"Created {count} scheduled session(s).")
    return redirect("dashboard")
