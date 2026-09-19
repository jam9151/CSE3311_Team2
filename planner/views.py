from datetime import date as date_cls
from datetime import datetime, timedelta

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    BacklogItemForm,
    CatalogSearchForm,
    CommitmentForm,
    ProfileForm,
    RecurringBlockForm,
    RegisterForm,
    ReviewForm,
)
from .models import BacklogItem, CatalogItem, MediaType, Profile, RecurringBlock, Review, Suggestion
from .services import day_plan, free_intervals, humanize_minutes, month_grid, suggest_for_day, total_free_minutes


def _parse_date(raw, fallback=None):
    """?date=YYYY-MM-DD, falling back if it's missing or someone edited the URL."""
    fallback = fallback or timezone.localdate()
    if not raw:
        return fallback
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def home(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "planner/home.html")


def register(request):
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        # Straight to the schedule, not the dashboard -- with no commitments
        # entered there's no free time yet and the dashboard looks broken.
        messages.success(request, "Welcome to MediaFlow. Start by telling us when you are busy.")
        return redirect("weekly_schedule")
    return render(request, "registration/register.html", {"form": form})


@login_required
def dashboard(request):
    today = timezone.localdate()
    plan = day_plan(request.user, today)
    free_minutes = sum(gap.minutes for gap in plan["free"])

    upcoming = (
        request.user.suggestions.filter(date__gt=today, status=Suggestion.Status.ACCEPTED)
        .select_related("backlog_item")[:5]
    )
    backlog = request.user.backlog_items.exclude(status=BacklogItem.Status.COMPLETED)

    context = {
        "plan": plan,
        "free_minutes": free_minutes,
        "free_label": humanize_minutes(free_minutes),
        "longest_gap": max(plan["free"], key=lambda g: g.minutes, default=None),
        "upcoming": upcoming,
        "backlog": backlog[:6],
        "backlog_count": backlog.count(),
        "in_progress": backlog.filter(status=BacklogItem.Status.IN_PROGRESS)[:4],
        "recent_reviews": request.user.reviews.select_related("backlog_item")[:3],
        "needs_schedule": not request.user.recurring_blocks.exists(),
    }
    return render(request, "planner/dashboard.html", context)


@login_required
@require_POST
def refresh_suggestions(request):
    day = _parse_date(request.POST.get("date"))
    # Clear the untouched ones first so the user doesn't end up with a pile of
    # stale suggestions every time they press the button.
    request.user.suggestions.filter(date=day, status=Suggestion.Status.PENDING).delete()
    created = suggest_for_day(request.user, day)

    # Empty results have three different causes and the fix differs each time,
    # so work out which one it was instead of saying "nothing found".
    if created:
        messages.success(request, f"Found {len(created)} thing(s) that fit your free time.")
    elif not request.user.backlog_items.exclude(status=BacklogItem.Status.COMPLETED).exists():
        messages.info(request, "Add something to your backlog first, then we can plan around it.")
    elif not free_intervals(request.user, day):
        messages.info(request, "No usable free time left that day. Try another one.")
    else:
        messages.info(request, "Nothing in your backlog fits the gaps you have left that day.")

    return redirect(request.POST.get("next") or "dashboard")


@login_required
@require_POST
def respond_to_suggestion(request, pk, action):
    suggestion = get_object_or_404(Suggestion, pk=pk, user=request.user)

    if action == "accept":
        suggestion.status = Suggestion.Status.ACCEPTED
        if suggestion.backlog_item.status == BacklogItem.Status.BACKLOG:
            suggestion.backlog_item.status = BacklogItem.Status.IN_PROGRESS
            suggestion.backlog_item.save(update_fields=["status"])
        messages.success(request, f"Added {suggestion.backlog_item.title} to your calendar.")

    elif action == "reject":
        suggestion.status = Suggestion.Status.REJECTED
        messages.info(request, "Noted. We will ease off on that one for a while.")

    elif action == "done":
        suggestion.status = Suggestion.Status.DONE
        item = suggestion.backlog_item
        # Trust the planned length rather than asking how long it actually took.
        item.minutes_completed = min(item.estimated_minutes, item.minutes_completed + suggestion.minutes)
        if item.remaining_minutes == 0:
            item.status = BacklogItem.Status.COMPLETED
            item.completed_at = timezone.now()
            messages.success(request, f"Finished {item.title}. Want to review it?")
        else:
            item.status = BacklogItem.Status.IN_PROGRESS
            messages.success(request, f"Logged {humanize_minutes(suggestion.minutes)} on {item.title}.")
        item.save()

    else:
        raise Http404

    suggestion.responded_at = timezone.now()
    suggestion.save(update_fields=["status", "responded_at"])
    return redirect(request.POST.get("next") or "dashboard")


@login_required
def discover(request):
    genres = CatalogItem.objects.exclude(genre="").order_by("genre").values_list("genre", flat=True).distinct()
    form = CatalogSearchForm(request.GET or None, media_types=MediaType.choices, genres=list(genres))
    results = CatalogItem.objects.all()

    if form.is_valid():
        query = form.cleaned_data.get("q")
        if query:
            results = results.filter(
                Q(title__icontains=query) | Q(creator__icontains=query) | Q(description__icontains=query)
            )
        if form.cleaned_data.get("media_type"):
            results = results.filter(media_type=form.cleaned_data["media_type"])
        if form.cleaned_data.get("genre"):
            results = results.filter(genre=form.cleaned_data["genre"])
        if form.cleaned_data.get("max_minutes"):
            results = results.filter(typical_minutes__lte=form.cleaned_data["max_minutes"])
        results = results.order_by(form.cleaned_data.get("sort") or "title")

    # So the cards can show "already on your backlog" instead of an Add button.
    owned = set(
        request.user.backlog_items.filter(catalog_item__isnull=False).values_list("catalog_item_id", flat=True)
    )
    context = {
        "form": form,
        "results": results[:60],
        "result_count": results.count(),
        "owned_ids": owned,
        "free_today": humanize_minutes(total_free_minutes(request.user, timezone.localdate())),
    }
    return render(request, "planner/discover.html", context)


def _fits_today(user, catalog_item):
    """True/False, or None when there's no free time to compare against."""
    gaps = free_intervals(user, timezone.localdate())
    if not gaps:
        return None
    return max(gap.minutes for gap in gaps) >= catalog_item.typical_minutes


@login_required
def catalog_detail(request, slug):
    item = get_object_or_404(CatalogItem, slug=slug)
    return render(
        request,
        "planner/catalog_detail.html",
        {
            "item": item,
            "entry": request.user.backlog_items.filter(catalog_item=item).first(),
            "fits_today": _fits_today(request.user, item),
        },
    )


@login_required
@require_POST
def add_from_catalog(request, pk):
    item = get_object_or_404(CatalogItem, pk=pk)
    _, created = BacklogItem.objects.get_or_create(
        user=request.user,
        catalog_item=item,
        defaults={
            "title": item.title,
            "media_type": item.media_type,
            "estimated_minutes": item.typical_minutes,
        },
    )
    if created:
        messages.success(request, f"Added {item.title} to your backlog.")
    else:
        messages.info(request, f"{item.title} is already on your backlog.")
    return redirect(request.POST.get("next") or "discover")


@login_required
def backlog(request):
    items = request.user.backlog_items.select_related("catalog_item")
    status = request.GET.get("status")
    if status in BacklogItem.Status.values:
        items = items.filter(status=status)
    return render(
        request,
        "planner/backlog.html",
        {"items": items, "active_status": status or "", "statuses": BacklogItem.Status.choices},
    )


@login_required
def backlog_edit(request, pk=None):
    # Same view handles add and edit; pk is None on the add route.
    instance = get_object_or_404(BacklogItem, pk=pk, user=request.user) if pk else None
    form = BacklogItemForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)
        item.user = request.user
        if item.status == BacklogItem.Status.COMPLETED and not item.completed_at:
            item.completed_at = timezone.now()
            item.minutes_completed = item.estimated_minutes
        item.save()
        messages.success(request, "Backlog updated.")
        return redirect("backlog")
    return render(
        request,
        "planner/form.html",
        {"form": form, "heading": "Edit item" if instance else "Add to backlog", "cancel_url": "backlog"},
    )


@login_required
@require_POST
def backlog_priority(request, pk):
    """Inline priority dropdown on the backlog page."""
    item = get_object_or_404(BacklogItem, pk=pk, user=request.user)
    try:
        priority = int(request.POST.get("priority", item.priority))
    except (TypeError, ValueError):
        priority = item.priority
    item.priority = max(1, min(5, priority))
    item.save(update_fields=["priority"])
    return redirect(request.POST.get("next") or "backlog")


@login_required
@require_POST
def backlog_delete(request, pk):
    item = get_object_or_404(BacklogItem, pk=pk, user=request.user)
    title = item.title
    item.delete()
    messages.success(request, f"Removed {title} from your backlog.")
    return redirect("backlog")


@login_required
def weekly_schedule(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)

    # Two forms on the page, told apart by which submit button was used.
    saving_profile = "save_profile" in request.POST
    adding_block = "add_block" in request.POST
    profile_form = ProfileForm(request.POST if saving_profile else None, instance=profile)
    block_form = RecurringBlockForm(request.POST if adding_block else None)

    if saving_profile and profile_form.is_valid():
        profile_form.save()
        messages.success(request, "Waking hours updated.")
        return redirect("weekly_schedule")
    if adding_block and block_form.is_valid():
        block = block_form.save(commit=False)
        block.user = request.user
        block.save()
        messages.success(request, f"Added {block.label} to your week.")
        return redirect("weekly_schedule")

    # Real dates rather than a generic Mon-Sun, so the free time shown accounts
    # for one-off commitments too.
    today = timezone.localdate()
    week = []
    for offset in range(7):
        day = today + timedelta(days=offset)
        gaps = free_intervals(request.user, day, include_planned=False)
        week.append(
            {
                "date": day,
                "blocks": request.user.recurring_blocks.filter(weekday=day.weekday()),
                "free": gaps,
                "free_label": humanize_minutes(sum(gap.minutes for gap in gaps)),
            }
        )

    return render(
        request,
        "planner/weekly_schedule.html",
        {
            "profile_form": profile_form,
            "block_form": block_form,
            "week": week,
            "commitments": request.user.commitments.filter(ends_at__gte=timezone.now())[:10],
        },
    )


@login_required
@require_POST
def delete_block(request, pk):
    get_object_or_404(RecurringBlock, pk=pk, user=request.user).delete()
    messages.success(request, "Removed from your week.")
    return redirect("weekly_schedule")


@login_required
def add_commitment(request):
    form = CommitmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        commitment = form.save(commit=False)
        commitment.user = request.user
        commitment.save()
        messages.success(request, "Commitment saved.")
        return redirect("weekly_schedule")
    return render(
        request,
        "planner/form.html",
        {"form": form, "heading": "Add a one-off commitment", "cancel_url": "weekly_schedule"},
    )


@login_required
def calendar(request):
    today = timezone.localdate()
    selected = _parse_date(request.GET.get("date"), today)

    # The month shown and the day selected are separate: paging to next month
    # shouldn't throw away which day the side panel is on.
    try:
        anchor = date_cls(int(request.GET.get("year", selected.year)), int(request.GET.get("month", selected.month)), 1)
    except ValueError:
        anchor = selected.replace(day=1)

    previous_month = (anchor - timedelta(days=1)).replace(day=1)
    next_month = (anchor + timedelta(days=32)).replace(day=1)

    return render(
        request,
        "planner/calendar.html",
        {
            "weeks": month_grid(request.user, anchor.year, anchor.month),
            "anchor": anchor,
            "previous_month": previous_month,
            "next_month": next_month,
            "selected": selected,
            "plan": day_plan(request.user, selected),
            "free_label": humanize_minutes(total_free_minutes(request.user, selected)),
            "weekday_names": ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
        },
    )


@login_required
def reviews(request):
    return render(
        request,
        "planner/reviews.html",
        {
            "reviews": request.user.reviews.select_related("backlog_item"),
            "awaiting": request.user.backlog_items.filter(
                status=BacklogItem.Status.COMPLETED, review__isnull=True
            ),
        },
    )


@login_required
def write_review(request, pk):
    item = get_object_or_404(BacklogItem, pk=pk, user=request.user)
    instance = Review.objects.filter(backlog_item=item).first()
    form = ReviewForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        review = form.save(commit=False)
        review.user = request.user
        review.backlog_item = item
        review.save()
        # Reviewing something implies you finished it, even if the backlog
        # status was never updated.
        if item.status != BacklogItem.Status.COMPLETED:
            item.status = BacklogItem.Status.COMPLETED
            item.completed_at = timezone.now()
            item.minutes_completed = item.estimated_minutes
            item.save()
        messages.success(request, "Review saved.")
        return redirect("reviews")
    return render(request, "planner/review_form.html", {"form": form, "item": item})
