import json
import os
import smtplib
from datetime import date as date_cls
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import DatabaseError, connection
from django.db.models import Avg, Count, Q
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .catalog_import import HttpFetcher, lookup_title, source_keys
from functools import wraps

from .forms import (
    ApiTokenForm,
    BacklogImportForm,
    BacklogItemForm,
    CatalogSearchForm,
    CommitmentForm,
    DeleteAccountForm,
    DisplayNameForm,
    LogTimeForm,
    NotificationSettingsForm,
    PlanSessionForm,
    ProfileForm,
    RecurringBlockForm,
    RegisterForm,
    ReviewForm,
    ScheduleImportForm,
    WatchInviteForm,
    WeeklyGoalForm,
)
from .imports import apply_media, apply_schedule, read_ics, read_media_csv
from .models import (
    DASHBOARD_PANEL_KEYS,
    DASHBOARD_PANELS,
    ApiToken,
    BacklogItem,
    BlockException,
    CatalogItem,
    Commitment,
    MediaType,
    Profile,
    PushSubscription,
    RecurringBlock,
    Review,
    Suggestion,
    WatchInvite,
)
from .services import (
    _from_minutes,
    accept_invite,
    day_plan,
    free_intervals,
    humanize_minutes,
    make_api_token,
    month_grid,
    next_in_series,
    progress_stats,
    semester_overview,
    shared_slots,
    stale_sittings,
    suggest_for_day,
    shared_reviews,
    suggest_for_week,
    total_free_minutes,
    upcoming_reminders,
    user_for_api_token,
    walkthrough,
)
from .verification import is_verified, read_token, seconds_until_resend, send_verification_email


def _parse_date(raw, fallback=None):
    """?date=YYYY-MM-DD, falling back if it's missing or someone edited the URL."""
    fallback = fallback or timezone.localdate()
    if not raw:
        return fallback
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return fallback


def _safe_next(request, fallback):
    """A ?next= / next= value, but only a path on this site. Anything else gets the fallback."""
    candidate = request.POST.get("next") or request.GET.get("next")
    if candidate and url_has_allowed_host_and_scheme(candidate, allowed_hosts={request.get_host()}):
        return candidate
    return fallback


def healthz(request):
    """For a host's health check. Public, and only says whether the database answers."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except DatabaseError:
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})


def home(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    return render(request, "planner/home.html")


def register(request):
    form = RegisterForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        # Signed in straight away so the holding page knows who to resend to,
        # but the middleware keeps them there until the link is opened.
        if send_verification_email(request, user):
            messages.success(request, f"Account created. We sent a confirmation link to {user.email}.")
        else:
            messages.warning(
                request, "Account created, but we couldn't send the confirmation email. Use the button below to retry."
            )
        return redirect("verify_pending")
    return render(request, "registration/register.html", {"form": form})


@login_required
def verify_pending(request):
    if is_verified(request.user):
        return redirect("dashboard")
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return render(
        request,
        "registration/verify_pending.html",
        {
            "email": request.user.email,
            "wait": seconds_until_resend(profile),
            "console_email": settings.EMAIL_BACKEND.endswith("console.EmailBackend"),
        },
    )


def verify_email(request, token):
    """The link in the email. Works signed in or out, the token says who it is for."""
    user = read_token(token)
    if user is None:
        return render(request, "registration/verify_invalid.html", status=400)

    profile, _ = Profile.objects.get_or_create(user=user)
    newly_verified = not profile.email_verified
    if newly_verified:
        profile.email_verified = True
        profile.save(update_fields=["email_verified"])
        messages.success(request, "Email confirmed.")
    else:
        messages.info(request, "That email was already confirmed.")

    if not request.user.is_authenticated:
        messages.info(request, "Sign in to continue.")
        return redirect("login")
    if request.user == user:
        if newly_verified:
            # Straight to the schedule, not the dashboard -- with no commitments
            # entered there's no free time yet and the dashboard looks broken.
            messages.success(request, "Welcome to MediaFlow. Start by telling us when you are busy.")
        return redirect("weekly_schedule")
    return redirect("dashboard")


@login_required
@require_POST
def resend_verification(request):
    if is_verified(request.user):
        return redirect("dashboard")
    profile, _ = Profile.objects.get_or_create(user=request.user)
    wait = seconds_until_resend(profile)
    if wait:
        messages.info(request, f"Please wait {wait} second{'s' if wait != 1 else ''} before asking for another email.")
    elif send_verification_email(request, request.user):
        messages.success(request, f"Sent a new confirmation link to {request.user.email}.")
    else:
        messages.error(request, "We couldn't send the email right now. Please try again in a minute.")
    return redirect("verify_pending")


@login_required
def dashboard(request):
    today = timezone.localdate()
    profile, _ = Profile.objects.get_or_create(user=request.user)
    hidden = profile.hidden_panel_keys
    plan = day_plan(request.user, today)
    free_minutes = sum(gap.minutes for gap in plan["free"])

    upcoming = (
        request.user.suggestions.filter(date__gt=today, status=Suggestion.Status.ACCEPTED)
        .select_related("backlog_item")[:5]
    )
    backlog = request.user.backlog_items.exclude(status=BacklogItem.Status.COMPLETED)
    stats = progress_stats(request.user, today) if profile.weekly_goal_minutes else None

    context = {
        "plan": plan,
        "followup": stale_sittings(request.user) if "followup" not in hidden else [],
        "invites_in": request.user.received_invites.filter(status=WatchInvite.Status.PENDING).select_related("host"),
        "invites_out": request.user.sent_invites.filter(status=WatchInvite.Status.PENDING).select_related("guest"),
        "goal": {
            "week_time": stats["week_time"],
            "goal_time": stats["weekly_goal_time"],
            "percent": stats["goal_percent"],
            "met": stats["goal_met"],
        }
        if stats
        else None,
        "free_minutes": free_minutes,
        "free_label": humanize_minutes(free_minutes),
        "longest_gap": max(plan["free"], key=lambda g: g.minutes, default=None),
        "upcoming": upcoming,
        "backlog": backlog[:6],
        "backlog_count": backlog.count(),
        "in_progress": backlog.filter(status=BacklogItem.Status.IN_PROGRESS)[:4],
        "recent_reviews": request.user.reviews.select_related("backlog_item")[:3],
        "walkthrough": walkthrough(request.user),
        "needs_schedule": not request.user.recurring_blocks.exists(),
        "reminders": upcoming_reminders(request.user),
        # Which panels this user has taken off the page, and the full list for
        # the customizer at the bottom.
        "hidden": hidden,
        "hidden_count": len(hidden),
        "panels": [
            {"key": key, "title": title, "detail": detail, "shown": key not in hidden}
            for key, title, detail in DASHBOARD_PANELS
        ],
    }
    return render(request, "planner/dashboard.html", context)


@login_required
@require_POST
def dashboard_panels(request):
    """Which panels Today shows.

    Posted either as one `hide=<key>` from a panel's own Hide button, one
    `show=<key>`, or the whole set of `panels` ticked in the customizer, in
    which case anything not ticked is hidden.
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)
    hidden = set(profile.hidden_panel_keys)
    if "hide" in request.POST:
        hidden.add(request.POST["hide"])
        messages.info(request, "Hidden. Bring it back from Customize this page, at the bottom of Today.")
    elif "show" in request.POST:
        hidden.discard(request.POST["show"])
    else:
        shown = set(request.POST.getlist("panels"))
        hidden = {key for key in DASHBOARD_PANEL_KEYS if key not in shown}
        messages.success(request, "Today updated.")
    profile.set_hidden_panels(hidden)
    profile.save(update_fields=["hidden_panels"])
    return redirect(_safe_next(request, reverse("dashboard")))


@login_required
@require_POST
def dismiss_walkthrough(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    profile.walkthrough_dismissed = True
    profile.save(update_fields=["walkthrough_dismissed"])
    messages.info(request, "Hidden. Every page it pointed at is still in the menu above.")
    return redirect(request.POST.get("next") or "dashboard")


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


def _mark_accepted(suggestion):
    """Status change for an accepted suggestion. The caller saves the suggestion."""
    suggestion.status = Suggestion.Status.ACCEPTED
    if suggestion.backlog_item.status == BacklogItem.Status.BACKLOG:
        suggestion.backlog_item.status = BacklogItem.Status.IN_PROGRESS
        suggestion.backlog_item.save(update_fields=["status"])


@login_required
@require_POST
def plan_week(request):
    """Replace the pending suggestions for the next seven days with a fresh plan."""
    start = timezone.localdate()
    end = start + timedelta(days=6)
    request.user.suggestions.filter(date__range=(start, end), status=Suggestion.Status.PENDING).delete()
    plan = suggest_for_week(request.user, start)

    created = sum(len(day) for day in plan.values())
    days_covered = sum(1 for day in plan.values() if day)

    # Same three causes as a single day, so say which one it was.
    if created:
        messages.success(
            request,
            f"Planned {created} session{'s' if created != 1 else ''} across {days_covered} "
            f"day{'s' if days_covered != 1 else ''}. Add the ones you like to your calendar.",
        )
    elif not request.user.backlog_items.exclude(status=BacklogItem.Status.COMPLETED).exists():
        messages.info(request, "Add something to your backlog first, then we can plan around it.")
    elif not any(free_intervals(request.user, day) for day in plan):
        messages.info(request, "No usable free time in the next seven days.")
    else:
        messages.info(request, "Nothing in your backlog fits the gaps you have this week.")
    return redirect("weekly_schedule")


@login_required
@require_POST
def accept_week(request):
    """Add every pending suggestion in the next seven days to the calendar."""
    start = timezone.localdate()
    pending = request.user.suggestions.filter(
        date__range=(start, start + timedelta(days=6)), status=Suggestion.Status.PENDING
    ).select_related("backlog_item")
    now = timezone.now()
    count = 0
    for suggestion in pending:
        _mark_accepted(suggestion)
        suggestion.responded_at = now
        suggestion.save(update_fields=["status", "responded_at"])
        count += 1
    if count:
        messages.success(request, f"Added {count} session{'s' if count != 1 else ''} to your calendar.")
    else:
        messages.info(request, "Nothing waiting to be added.")
    return redirect("weekly_schedule")


@login_required
@require_POST
def respond_to_suggestion(request, pk, action):
    suggestion = get_object_or_404(Suggestion, pk=pk, user=request.user)

    if action == "accept":
        _mark_accepted(suggestion)
        messages.success(request, f"Added {suggestion.backlog_item.title} to your calendar.")

    elif action == "reject":
        suggestion.status = Suggestion.Status.REJECTED
        messages.info(request, "Noted. We will ease off on that one for a while.")

    elif action == "later":
        # Not now, not penalised: back on the table from tomorrow.
        if suggestion.status != Suggestion.Status.PENDING:
            raise Http404
        item = suggestion.backlog_item
        item.snoozed_until = suggestion.date + timedelta(days=1)
        item.save(update_fields=["snoozed_until"])
        suggestion.delete()
        messages.info(request, f"Later it is. {item.title} is back in the running from tomorrow.")
        return redirect(_safe_next(request, reverse("dashboard")))

    elif action == "cancel":
        # Taking an accepted sitting off the calendar. The time is free again
        # and nothing is held against the title.
        if suggestion.status != Suggestion.Status.ACCEPTED:
            raise Http404
        title = suggestion.backlog_item.title
        suggestion.delete()
        messages.info(request, f"Took {title} off your calendar.")
        return redirect(_safe_next(request, reverse("dashboard")))

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
        results = results.order_by(form.cleaned_data.get("sort") or "title", "pk")

    # So the cards can show "already on your backlog" instead of an Add button.
    owned = set(
        request.user.backlog_items.filter(catalog_item__isnull=False).values_list("catalog_item_id", flat=True)
    )
    # The catalog runs to hundreds of rows once it has been imported into.
    paginator = Paginator(results, 24)
    page = paginator.get_page(request.GET.get("page"))
    # The other filters, so the page links keep them.
    params = request.GET.copy()
    params.pop("page", None)
    context = {
        "form": form,
        "results": page,
        "page": page,
        "page_query": params.urlencode(),
        "result_count": paginator.count,
        "owned_ids": owned,
        "free_today": humanize_minutes(total_free_minutes(request.user, timezone.localdate())),
        # The lookup button: shown with a search term, naming the sources that are set up.
        "lookup_query": (form.cleaned_data.get("q") or "").strip() if form.is_valid() else "",
        "lookup_sources": sorted(source_keys(os.environ)),
    }
    return render(request, "planner/discover.html", context)


def _read_upload(upload, limit=5 * 1024 * 1024):
    """An uploaded text file as a string, or None if it is too big or not text."""
    if upload is None or upload.size > limit:
        return None
    raw = upload.read()
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


@login_required
@require_POST
def lookup_catalog(request):
    """Ask Open Library, TMDB and RAWG for a title right now, then show the results on Discover."""
    query = (request.POST.get("q") or "").strip()[:120]
    media_type = request.POST.get("media_type", "")
    if media_type not in MediaType.values:
        media_type = ""
    if len(query) < 2:
        messages.info(request, "Type a title to look up first.")
        return redirect("discover")
    keys = source_keys(os.environ)
    if not keys:
        messages.info(request, "No catalog sources are set up on this server.")
        return redirect(f"{reverse('discover')}?q={query}")

    contact = os.environ.get("CATALOG_CONTACT_EMAIL", "").strip() or settings.EMAIL_HOST_USER
    user_agent = f"MediaFlow-CSE3311 ({contact})" if contact else "MediaFlow-CSE3311"

    def fetcher_for(source_class):
        return HttpFetcher(user_agent, pause=source_class.pause(bool(contact)), retries=1, timeout=10)

    items, failures = lookup_title(query, keys, fetcher_for, media_type=media_type)
    if items:
        messages.success(request, f"Found {len(items)} title{'s' if len(items) != 1 else ''} for “{query}” and added them to the catalog.")
    elif failures:
        messages.error(request, f"Could not reach {', '.join(failures)} right now. Try again in a minute.")
    else:
        messages.info(request, f"Nothing out there matched “{query}”.")
    params = {"q": query}
    if media_type:
        params["media_type"] = media_type
    from urllib.parse import urlencode

    return redirect(f"{reverse('discover')}?{urlencode(params)}")


@login_required
def import_backlog(request):
    """A Letterboxd or Goodreads export, onto the backlog."""
    form = BacklogImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        text = _read_upload(form.cleaned_data["file"])
        plan = read_media_csv(text, default_status=form.cleaned_data["status"]) if text else None
        if plan is None:
            form.add_error("file", "That does not look like a Letterboxd or Goodreads export. See the note below.")
        else:
            counts = apply_media(request.user, plan)
            parts = [f"{number} {word}" for word, number in counts.items() if number]
            messages.success(
                request,
                f"Read {len(plan.rows)} row{'s' if len(plan.rows) != 1 else ''} from {plan.source.capitalize()}: "
                + (", ".join(parts) or "nothing new")
                + ".",
            )
            return redirect("backlog")
    return render(
        request,
        "planner/form.html",
        {
            "form": form,
            "heading": "Import a watchlist or reading list",
            "intro": "Letterboxd: Settings, Import & Export, Export your data, then upload watchlist.csv (or watched.csv, ratings.csv). "
            "Goodreads: My Books, Import and export, Export library, then upload the file. Titles already in the catalog are matched; the rest are added by name.",
            "cancel_url": "backlog",
            "submit_label": "Import",
            "multipart": True,
        },
    )


@login_required
def import_schedule(request):
    """A calendar file (.ics) onto My week: weekly repeats become blocks, the rest commitments."""
    form = ScheduleImportForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        text = _read_upload(form.cleaned_data["file"])
        plan = read_ics(text) if text and "BEGIN:VCALENDAR" in text else None
        if plan is None:
            form.add_error("file", "That is not a calendar (.ics) file.")
        elif plan.empty:
            reasons = ", ".join(f"{reason} {number}" for reason, number in plan.skipped.most_common())
            form.add_error("file", f"Nothing in there to add{f' (skipped: {reasons})' if reasons else ''}.")
        else:
            counts = apply_schedule(request.user, plan)
            parts = [f"{number} {word}" for word, number in counts.items() if number]
            note = ""
            if plan.skipped:
                note = " Left out " + ", ".join(f"{number} {reason}" for reason, number in plan.skipped.most_common()) + "."
            messages.success(request, f"Imported {', '.join(parts)}.{note}")
            return redirect("weekly_schedule")
    return render(
        request,
        "planner/form.html",
        {
            "form": form,
            "heading": "Import a calendar",
            "intro": "Export your class schedule or calendar as an .ics file (MyMav, Google Calendar and Outlook all can). "
            "Events that repeat weekly become recurring blocks; one-off events in the next year become commitments. "
            "All-day events and anything already over are left out.",
            "cancel_url": "weekly_schedule",
            "submit_label": "Import",
            "multipart": True,
        },
    )


def _fits_today(user, catalog_item):
    """True/False, or None when there's no free time to compare against."""
    gaps = free_intervals(user, timezone.localdate())
    if not gaps:
        return None
    return max(gap.minutes for gap in gaps) >= catalog_item.typical_minutes


@login_required
def catalog_detail(request, slug):
    item = get_object_or_404(CatalogItem, slug=slug)
    entry = request.user.backlog_items.filter(catalog_item=item).select_related("review").first()
    my_review = getattr(entry, "review", None) if entry else None
    return render(
        request,
        "planner/catalog_detail.html",
        {
            "item": item,
            "entry": entry,
            "my_review": my_review,
            "community": shared_reviews(item, exclude_user=request.user),
            "fits_today": _fits_today(request.user, item),
            "next_in_series": next_in_series(item, request.user),
            "series_count": CatalogItem.objects.filter(series=item.series, media_type=item.media_type).count() if item.series else 0,
        },
    )


# ------------------------------------------------------------ watch together


@login_required
def watch_together(request, pk):
    """Invite a classmate to a title at a time that is free for both.

    Two steps on one page: name the classmate (step one finds the shared
    gaps), then pick a slot (step two sends the invite).
    """
    item = get_object_or_404(CatalogItem, pk=pk)
    form = WatchInviteForm(request.POST or None, host=request.user)
    slots = []
    guest = None
    if request.method == "POST" and form.is_valid():
        guest = form.cleaned_data["guest"]
        slots = shared_slots(request.user, guest, item.typical_minutes)
        chosen = request.POST.get("slot", "")
        if chosen:
            for day, start, end in slots:
                if chosen == f"{day:%Y-%m-%d}|{start:%H:%M}":
                    invite = WatchInvite.objects.create(
                        host=request.user,
                        guest=guest,
                        catalog_item=item,
                        title=item.title,
                        media_type=item.media_type,
                        minutes=item.typical_minutes,
                        date=day,
                        start_time=start,
                        end_time=end,
                        message=form.cleaned_data["message"],
                    )
                    messages.success(
                        request,
                        f"Invited {guest.username} to {item.title} on {day:%A, %B} {day.day} at {start:%I:%M %p}. "
                        "It lands on both calendars once they accept.".replace(" 0", " "),
                    )
                    return redirect(item.get_absolute_url())
            messages.error(request, "That slot is no longer free for both of you. Pick another.")
    return render(
        request,
        "planner/watch_together.html",
        {
            "item": item,
            "form": form,
            "guest": guest,
            "slots": [(f"{day:%Y-%m-%d}|{start:%H:%M}", day, start, end) for day, start, end in slots],
            "searched": request.method == "POST" and form.is_valid(),
        },
    )


@login_required
@require_POST
def respond_to_invite(request, pk, action):
    invite = get_object_or_404(WatchInvite, pk=pk, status=WatchInvite.Status.PENDING)
    if action in ("accept", "decline") and invite.guest_id == request.user.pk:
        if action == "accept":
            accept_invite(invite)
            messages.success(request, f"{invite.title} with {invite.host.username} is on both your calendars.")
        else:
            invite.status = WatchInvite.Status.DECLINED
            invite.responded_at = timezone.now()
            invite.save(update_fields=["status", "responded_at"])
            messages.info(request, f"Declined. {invite.host.username} will see that.")
    elif action == "cancel" and invite.host_id == request.user.pk:
        invite.status = WatchInvite.Status.CANCELLED
        invite.responded_at = timezone.now()
        invite.save(update_fields=["status", "responded_at"])
        messages.info(request, "Invite withdrawn.")
    else:
        raise Http404
    return redirect(_safe_next(request, reverse("dashboard")))


# ------------------------------------------------------------ semester view


@login_required
def semester(request):
    """Busy hours per week for the coming term, so finals week is visible from here."""
    try:
        weeks = max(4, min(26, int(request.GET.get("weeks", 16))))
    except ValueError:
        weeks = 16
    return render(request, "planner/semester.html", {"overview": semester_overview(request.user, weeks=weeks), "weeks": weeks})


# ------------------------------------------------------------ api


def api_token_required(view):
    """Bearer-token auth for the JSON API. No session, no CSRF, no cookies."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        raw = header[7:].strip() if header.lower().startswith("bearer ") else ""
        user = user_for_api_token(raw)
        if user is None:
            response = JsonResponse({"error": "A valid bearer token is required. Make one on the Settings page."}, status=401)
            response["WWW-Authenticate"] = 'Bearer realm="MediaFlow"'
            return response
        request.api_user = user
        return view(request, *args, **kwargs)

    return wrapped


def _api_item(item):
    return {
        "id": item.pk,
        "title": item.title,
        "media_type": item.media_type,
        "priority": item.priority,
        "status": item.status,
        "estimated_minutes": item.estimated_minutes,
        "minutes_completed": item.minutes_completed,
        "remaining_minutes": item.remaining_minutes,
        "progress": item.progress_label,
        "catalog_url": item.catalog_item.get_absolute_url() if item.catalog_item else None,
    }


def _api_sitting(suggestion):
    return {
        "id": suggestion.pk,
        "title": suggestion.backlog_item.title,
        "media_type": suggestion.backlog_item.media_type,
        "date": suggestion.date.isoformat(),
        "start_time": suggestion.start_time.isoformat(timespec="minutes"),
        "end_time": suggestion.end_time.isoformat(timespec="minutes"),
        "minutes": suggestion.minutes,
        "status": suggestion.status,
        "reason": suggestion.reason,
    }


@api_token_required
def api_me(request):
    user = request.api_user
    profile = getattr(user, "profile", None)
    return JsonResponse(
        {
            "username": user.username,
            "display_name": profile.public_name if profile else user.username,
            "day_start": profile.day_start.isoformat(timespec="minutes") if profile else None,
            "day_end": profile.day_end.isoformat(timespec="minutes") if profile else None,
            "weekly_goal_minutes": profile.weekly_goal_minutes if profile else 0,
        }
    )


@api_token_required
def api_backlog(request):
    rows = request.api_user.backlog_items.select_related("catalog_item")
    status = request.GET.get("status")
    if status in BacklogItem.Status.values:
        rows = rows.filter(status=status)
    return JsonResponse({"items": [_api_item(item) for item in rows]})


@api_token_required
def api_day(request):
    """Free gaps, busy blocks and sittings for one date (today by default)."""
    day = _parse_date(request.GET.get("date"))
    plan = day_plan(request.api_user, day)
    return JsonResponse(
        {
            "date": day.isoformat(),
            "free": [
                {"start": gap.start.isoformat(timespec="minutes"), "end": gap.end.isoformat(timespec="minutes"), "minutes": gap.minutes}
                for gap in plan["free"]
            ],
            "busy": [
                {"start": b.start.isoformat(timespec="minutes"), "end": b.end.isoformat(timespec="minutes"), "label": b.label, "source": b.source}
                for b in plan["busy"]
            ],
            "planned": [_api_sitting(s) for s in plan["planned"]],
            "pending": [_api_sitting(s) for s in plan["pending"]],
        }
    )


@api_token_required
def api_progress(request):
    stats = progress_stats(request.api_user)
    return JsonResponse(
        {
            "week_minutes": stats["week_minutes"],
            "weekly_goal_minutes": stats["weekly_goal"],
            "month_minutes": stats["month_minutes"],
            "total_minutes": stats["total_minutes"],
            "current_streak": stats["current_streak"],
            "best_streak": stats["best_streak"],
            "finished_total": stats["finished_total"],
        }
    )


@login_required
@require_POST
def create_api_token(request):
    form = ApiTokenForm(request.POST)
    if form.is_valid():
        _, raw = make_api_token(request.user, form.cleaned_data["name"])
        messages.success(
            request,
            f"Token for {form.cleaned_data['name']}: {raw} — copy it now, it will not be shown again.",
            extra_tags="token",
        )
    else:
        messages.error(request, "Give the token a name.")
    return redirect(f"{reverse('settings')}#api")


@login_required
@require_POST
def revoke_api_token(request, pk):
    token = get_object_or_404(ApiToken, pk=pk, user=request.user)
    token.delete()
    messages.info(request, f"Token {token.prefix}… revoked.")
    return redirect(f"{reverse('settings')}#api")


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
        {
            "items": items,
            "active_status": status or "",
            "statuses": BacklogItem.Status.choices,
            "priorities": BacklogItem.PRIORITY_CHOICES,
        },
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
def log_time(request, pk):
    """Minutes spent on an item outside a planned sitting, on a date, so Progress can chart them."""
    item = get_object_or_404(BacklogItem, pk=pk, user=request.user)
    next_url = _safe_next(request, reverse("backlog"))
    form = LogTimeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        log = form.save(commit=False)
        log.user = request.user
        log.backlog_item = item
        log.save()
        item.minutes_completed = min(item.estimated_minutes, item.minutes_completed + log.minutes)
        if item.remaining_minutes == 0:
            item.status = BacklogItem.Status.COMPLETED
            item.completed_at = timezone.now()
            messages.success(request, f"Finished {item.title}. Want to review it?")
        else:
            item.status = BacklogItem.Status.IN_PROGRESS
            messages.success(request, f"Logged {humanize_minutes(log.minutes)} on {item.title}.")
        item.save()
        return redirect(next_url)
    return render(
        request,
        "planner/form.html",
        {
            "form": form,
            "heading": f"Log time on {item.title}",
            "intro": f"{item.remaining_minutes} min left. Time logged here counts on Progress on the date you pick.",
            "cancel_url": "backlog",
            "cancel_href": next_url,
            "next_url": next_url,
            "submit_label": "Log it",
        },
    )


@login_required
def weekly_schedule(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)

    # Four forms on the page, told apart by which submit button was used.
    saving_profile = "save_profile" in request.POST
    adding_block = "add_block" in request.POST
    saving_notifications = "save_notifications" in request.POST
    saving_goal = "save_goal" in request.POST
    profile_form = ProfileForm(request.POST if saving_profile else None, instance=profile)
    block_form = RecurringBlockForm(request.POST if adding_block else None)
    notification_form = NotificationSettingsForm(request.POST if saving_notifications else None, instance=profile)
    goal_form = WeeklyGoalForm(request.POST if saving_goal else None, instance=profile)

    if saving_profile and profile_form.is_valid():
        profile_form.save()
        messages.success(request, "Waking hours updated.")
        return redirect("weekly_schedule")
    if saving_goal and goal_form.is_valid():
        goal_form.save()
        messages.success(request, "Weekly goal saved." if profile.weekly_goal_minutes else "Weekly goal turned off.")
        return redirect("weekly_schedule")
    if saving_notifications and notification_form.is_valid():
        notification_form.save()
        messages.success(request, "Reminder settings saved.")
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

    # One query for the week's sessions rather than one per day.
    sessions_by_day = {}
    week_sessions = request.user.suggestions.filter(
        date__range=(today, today + timedelta(days=6)),
        status__in=[Suggestion.Status.PENDING, Suggestion.Status.ACCEPTED],
    ).select_related("backlog_item")
    for suggestion in week_sessions:
        sessions_by_day.setdefault(suggestion.date, []).append(suggestion)

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
                "sessions": sessions_by_day.get(day, []),
            }
        )

    return render(
        request,
        "planner/weekly_schedule.html",
        {
            "profile_form": profile_form,
            "block_form": block_form,
            "notification_form": notification_form,
            "goal_form": goal_form,
            "exceptions": request.user.block_exceptions.filter(date__gte=today).select_related("block")[:20],
            "week": week,
            "pending_count": sum(1 for s in week_sessions if s.status == Suggestion.Status.PENDING),
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
def edit_block(request, pk):
    block = get_object_or_404(RecurringBlock, pk=pk, user=request.user)
    form = RecurringBlockForm(request.POST or None, instance=block)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Updated {block.label}.")
        return redirect("weekly_schedule")
    return render(
        request,
        "planner/form.html",
        {"form": form, "heading": f"Edit {block.label}", "cancel_url": "weekly_schedule"},
    )


@login_required
def add_commitment(request):
    """Add a one-off commitment. From the calendar, ?date= starts the form on that day."""
    next_url = _safe_next(request, reverse("weekly_schedule"))
    initial = {}
    if request.GET.get("date"):
        day = _parse_date(request.GET.get("date"))
        # Start it in the first free stretch of that day, or mid-morning if
        # the day is full or already gone.
        gaps = free_intervals(request.user, day)
        start = datetime.combine(day, gaps[0].start if gaps else time(9, 0))
        initial = {"starts_at": start, "ends_at": start + timedelta(hours=1)}
    form = CommitmentForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        commitment = form.save(commit=False)
        commitment.user = request.user
        commitment.save()
        messages.success(request, "Commitment saved.")
        return redirect(next_url)
    return render(
        request,
        "planner/form.html",
        {
            "form": form,
            "heading": "Add a one-off commitment",
            "cancel_url": "weekly_schedule",
            "cancel_href": next_url,
            "next_url": next_url,
        },
    )


@login_required
def edit_commitment(request, pk):
    commitment = get_object_or_404(Commitment, pk=pk, user=request.user)
    next_url = _safe_next(request, reverse("weekly_schedule"))
    form = CommitmentForm(request.POST or None, instance=commitment)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Commitment updated.")
        return redirect(next_url)
    return render(
        request,
        "planner/form.html",
        {
            "form": form,
            "heading": f"Edit {commitment.title}",
            "cancel_url": "weekly_schedule",
            "cancel_href": next_url,
            "next_url": next_url,
        },
    )


@login_required
def plan_session(request):
    """Put something from the backlog on a day yourself, from the calendar.

    Saves an accepted Suggestion, so the sitting shows up, blocks time and is
    marked done exactly like one the planner proposed. With ?move=<pk> it
    edits an accepted sitting instead: new day, time or length, or all three.
    """
    moving = None
    if request.GET.get("move"):
        moving = get_object_or_404(
            Suggestion, pk=request.GET["move"], user=request.user, status=Suggestion.Status.ACCEPTED
        )
    day = _parse_date(request.GET.get("date"), moving.date if moving else None)
    next_url = _safe_next(request, None)
    gaps = free_intervals(request.user, day)
    if moving:
        initial = {
            "backlog_item": moving.backlog_item_id,
            "start_time": moving.start_time,
            "minutes": moving.minutes,
            "date": moving.date,
        }
    elif gaps:
        initial = {"start_time": gaps[0].start, "minutes": min(gaps[0].minutes, 60)}
    else:
        initial = {}
    form = PlanSessionForm(request.POST or None, user=request.user, day=day, moving=moving, initial=initial)
    if request.method == "POST" and form.is_valid():
        item = form.cleaned_data["backlog_item"]
        minutes = form.cleaned_data["minutes"]
        day = form.cleaned_data["date"]
        back = next_url or f"{reverse('calendar')}?date={day:%Y-%m-%d}"
        if moving:
            moving.backlog_item = item
            moving.date = day
            moving.start_time = form.cleaned_data["start_time"]
            moving.end_time = _from_minutes(form.cleaned_data["end_minutes"])
            moving.minutes = minutes
            moving.save()
            messages.success(request, f"Moved {item.title} to {day:%A} at {moving.start_time:%I:%M %p}.".replace(" 0", " "))
            return redirect(back)
        suggestion = Suggestion(
            user=request.user,
            backlog_item=item,
            date=day,
            start_time=form.cleaned_data["start_time"],
            end_time=_from_minutes(form.cleaned_data["end_minutes"]),
            minutes=minutes,
            reason="You put this on the calendar yourself.",
            responded_at=timezone.now(),
        )
        _mark_accepted(suggestion)
        suggestion.save()
        messages.success(request, f"Planned {humanize_minutes(minutes)} of {item.title} on {day:%A}.")
        return redirect(back)
    back = next_url or f"{reverse('calendar')}?date={day:%Y-%m-%d}"
    has_items = form.fields["backlog_item"].queryset.exists()
    return render(
        request,
        "planner/form.html",
        {
            "form": form,
            "heading": f"Move {moving.backlog_item.title}" if moving else f"Plan something for {day:%A, %B} {day.day}",
            "intro": (
                f"{humanize_minutes(sum(gap.minutes for gap in gaps))} free that day."
                if gaps
                else "No free time recorded that day, but you can still plan around it."
            )
            if has_items
            else "Your backlog is empty. Add something to it first, then come back.",
            "cancel_url": "calendar",
            "cancel_href": back,
            "submit_label": "Save the move" if moving else "Put it on the calendar",
        },
    )


# ------------------------------------------------------------ block exceptions


@login_required
@require_POST
def skip_block(request, pk):
    """This recurring block does not happen on `date`: no class this Thursday."""
    block = get_object_or_404(RecurringBlock, pk=pk, user=request.user)
    day = _parse_date(request.POST.get("date"))
    if day.weekday() != block.weekday:
        messages.error(request, f"{block.label} is not on a {day:%A}.")
    else:
        _, created = BlockException.objects.get_or_create(user=request.user, block=block, date=day)
        if created:
            messages.success(request, f"Skipping {block.label} on {day:%A, %B} {day.day}. That time is free.")
        else:
            messages.info(request, f"{block.label} was already skipped that day.")
    return redirect(_safe_next(request, f"{reverse('calendar')}?date={day:%Y-%m-%d}"))


@login_required
@require_POST
def unskip_block(request, pk):
    exception = get_object_or_404(BlockException, pk=pk, user=request.user)
    label, day = exception.block.label, exception.date
    exception.delete()
    messages.info(request, f"{label} is back on {day:%A, %B} {day.day}.")
    return redirect(_safe_next(request, reverse("weekly_schedule")))


@login_required
@require_POST
def day_off(request):
    """Skip every recurring block on one date: a holiday, a sick day, a week off done a day at a time."""
    day = _parse_date(request.POST.get("date"))
    blocks = request.user.recurring_blocks.filter(weekday=day.weekday())
    made = 0
    for block in blocks:
        _, created = BlockException.objects.get_or_create(user=request.user, block=block, date=day)
        made += int(created)
    if made:
        messages.success(request, f"{day:%A, %B} {day.day} is a day off: {made} block{'s' if made != 1 else ''} skipped.")
    elif blocks.exists():
        messages.info(request, "Everything that day was already skipped.")
    else:
        messages.info(request, "Nothing recurring on that day to skip.")
    return redirect(_safe_next(request, f"{reverse('calendar')}?date={day:%Y-%m-%d}"))


@login_required
@require_POST
def delete_commitment(request, pk):
    get_object_or_404(Commitment, pk=pk, user=request.user).delete()
    messages.success(request, "Commitment removed.")
    return redirect("weekly_schedule")


@login_required
def reminders_json(request):
    """Polled by static/js/reminders.js. Empty unless the user opted in."""
    profile = getattr(request.user, "profile", None)
    enabled = bool(profile and profile.notify_enabled)
    reminders = upcoming_reminders(request.user) if enabled else []
    return JsonResponse(
        {
            "enabled": enabled,
            "reminders": [
                {
                    "key": r["key"],
                    "title": r["title"],
                    "body": f"{r['detail']} · starts in {r['minutes_until']} min"
                    if r["minutes_until"]
                    else f"{r['detail']} · starting now",
                }
                for r in reminders
            ],
        }
    )


# ------------------------------------------------------------ push reminders


def service_worker(request):
    """The service worker, at /sw.js so its scope is the whole site. Public: browsers fetch it on their own."""
    response = render(request, "planner/sw.js", content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


@login_required
def push_key(request):
    """Whether background push is on, and the public key the browser subscribes with."""
    return JsonResponse(
        {"enabled": settings.PUSH_ENABLED, "public_key": settings.VAPID_PUBLIC_KEY if settings.PUSH_ENABLED else ""}
    )


def _subscription_from(request):
    """The PushSubscription JSON a browser posts, or None if it is not one."""
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return None
    endpoint = data.get("endpoint") if isinstance(data, dict) else None
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(endpoint, str) or not endpoint.startswith("https://") or len(endpoint) > 500:
        return None
    if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        return None
    return endpoint, str(keys["p256dh"])[:200], str(keys["auth"])[:100]


@login_required
@require_POST
def push_subscribe(request):
    """Save (or refresh) this browser's subscription. Called by reminders.js, as JSON."""
    if not settings.PUSH_ENABLED:
        return JsonResponse({"ok": False, "error": "Push reminders are off on this server."}, status=409)
    parsed = _subscription_from(request)
    if parsed is None:
        return HttpResponseBadRequest("That is not a push subscription.")
    endpoint, p256dh, auth = parsed
    # An endpoint is one browser. If it was registered under another account
    # on a shared machine, it now belongs to whoever is signed in.
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user": request.user,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:200],
        },
    )
    return JsonResponse({"ok": True, "count": request.user.push_subscriptions.count()})


@login_required
@require_POST
def push_unsubscribe(request):
    """Remove one browser. JSON from reminders.js, or a plain form from the settings page."""
    if request.content_type == "application/json":
        try:
            endpoint = json.loads(request.body or b"{}").get("endpoint", "")
        except (ValueError, AttributeError):
            endpoint = ""
    else:
        endpoint = request.POST.get("endpoint", "")
    deleted, _ = request.user.push_subscriptions.filter(endpoint=endpoint).delete()
    if request.content_type != "application/json":
        messages.info(request, "That browser will not get background reminders any more." if deleted else "Nothing to remove.")
        return redirect(_safe_next(request, reverse("settings")))
    return JsonResponse({"ok": True, "removed": deleted})


# ------------------------------------------------------------ settings


@login_required
def settings_page(request):
    """One page for everything about the account. Four small forms, told apart by their submit button."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    which = next((name for name in ("save_hours", "save_goal", "save_notifications", "save_name") if name in request.POST), None)
    forms = {
        "save_hours": (ProfileForm, "Waking hours updated."),
        "save_goal": (WeeklyGoalForm, "Weekly goal saved."),
        "save_notifications": (NotificationSettingsForm, "Reminder settings saved."),
        "save_name": (DisplayNameForm, "Display name saved."),
    }
    bound = {}
    for name, (form_class, _message) in forms.items():
        bound[name] = form_class(request.POST if which == name else None, instance=profile)
    if which and bound[which].is_valid():
        bound[which].save()
        messages.success(request, forms[which][1])
        return redirect(f"{reverse('settings')}#{which.removeprefix('save_')}")

    hidden = profile.hidden_panel_keys
    return render(
        request,
        "planner/settings.html",
        {
            "profile_form": bound["save_hours"],
            "goal_form": bound["save_goal"],
            "notification_form": bound["save_notifications"],
            "name_form": bound["save_name"],
            "public_name": profile.public_name,
            "panels": [
                {"key": key, "title": title, "detail": detail, "shown": key not in hidden}
                for key, title, detail in DASHBOARD_PANELS
            ],
            "subscriptions": request.user.push_subscriptions.all(),
            "push_enabled": settings.PUSH_ENABLED,
            "tokens": request.user.api_tokens.all(),
            "token_form": ApiTokenForm(),
        },
    )


def _export_payload(user):
    """Everything the user put in, as plain dicts. Dates and times as ISO strings."""
    profile = getattr(user, "profile", None)

    def iso(value):
        return value.isoformat() if value is not None else None

    return {
        "exported_at": timezone.now().isoformat(),
        "account": {"username": user.username, "email": user.email, "joined": iso(user.date_joined)},
        "settings": {
            "day_start": iso(profile.day_start) if profile else None,
            "day_end": iso(profile.day_end) if profile else None,
            "display_name": profile.display_name if profile else "",
            "weekly_goal_minutes": profile.weekly_goal_minutes if profile else 0,
            "notify_enabled": profile.notify_enabled if profile else False,
            "notify_lead_minutes": profile.notify_lead_minutes if profile else None,
            "hidden_panels": sorted(profile.hidden_panel_keys) if profile else [],
        },
        "backlog": [
            {
                "title": item.title,
                "media_type": item.media_type,
                "catalog_title": item.catalog_item.title if item.catalog_item else None,
                "priority": item.priority,
                "estimated_minutes": item.estimated_minutes,
                "minutes_completed": item.minutes_completed,
                "unit_minutes": item.unit_minutes,
                "status": item.status,
                "notes": item.notes,
                "added_at": iso(item.added_at),
                "completed_at": iso(item.completed_at),
            }
            for item in user.backlog_items.select_related("catalog_item")
        ],
        "sittings": [
            {
                "title": s.backlog_item.title,
                "date": iso(s.date),
                "start_time": iso(s.start_time),
                "end_time": iso(s.end_time),
                "minutes": s.minutes,
                "status": s.status,
                "reason": s.reason,
            }
            for s in user.suggestions.select_related("backlog_item")
        ],
        "time_logs": [
            {"title": log.backlog_item.title, "date": iso(log.date), "minutes": log.minutes, "note": log.note}
            for log in user.time_logs.select_related("backlog_item")
        ],
        "reviews": [
            {
                "title": r.backlog_item.title,
                "rating": r.rating,
                "recommend": r.recommend,
                "headline": r.headline,
                "body": r.body,
                "contains_spoilers": r.contains_spoilers,
                "shared": r.shared,
                "anonymous": r.anonymous,
                "created_at": iso(r.created_at),
            }
            for r in user.reviews.select_related("backlog_item")
        ],
        "recurring_blocks": [
            {
                "label": b.label,
                "kind": b.kind,
                "weekday": b.weekday,
                "start_time": iso(b.start_time),
                "end_time": iso(b.end_time),
                "skipped_on": [iso(e.date) for e in b.exceptions.all()],
            }
            for b in user.recurring_blocks.prefetch_related("exceptions")
        ],
        "commitments": [
            {"title": c.title, "starts_at": iso(c.starts_at), "ends_at": iso(c.ends_at)} for c in user.commitments.all()
        ],
    }


@login_required
def export_data(request):
    """Everything the user put in, as one JSON download."""
    response = JsonResponse(_export_payload(request.user), json_dumps_params={"indent": 2, "ensure_ascii": False})
    response["Content-Disposition"] = (
        f'attachment; filename="mediaflow-{request.user.username}-{timezone.localdate():%Y%m%d}.json"'
    )
    return response


@login_required
def delete_account(request):
    """Gone for good, after the password is typed again."""
    form = DeleteAccountForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        user = request.user
        logout(request)
        user.delete()
        messages.info(request, "Your account and everything in it has been deleted.")
        return redirect("home")
    return render(request, "planner/delete_account.html", {"form": form})


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
            "has_recurring": request.user.recurring_blocks.filter(weekday=selected.weekday()).exists(),
            "skipped_blocks": request.user.block_exceptions.filter(date=selected).select_related("block"),
        },
    )


@login_required
def stats(request):
    return render(request, "planner/stats.html", {"stats": progress_stats(request.user)})


REVIEW_SORTS = {
    "newest": ("Newest first", "-created_at"),
    "highest": ("Highest rated", "-rating"),
    "lowest": ("Lowest rated", "rating"),
    "title": ("Title A-Z", "backlog_item__title"),
}


@login_required
def reviews(request):
    rows = request.user.reviews.select_related("backlog_item", "backlog_item__catalog_item")
    media_type = request.GET.get("type", "")
    if media_type in MediaType.values:
        rows = rows.filter(backlog_item__media_type=media_type)
    else:
        media_type = ""
    sort = request.GET.get("sort", "newest")
    if sort not in REVIEW_SORTS:
        sort = "newest"
    rows = rows.order_by(REVIEW_SORTS[sort][1], "-created_at")

    everything = request.user.reviews
    verdicts = [value for value in everything.values_list("recommend", flat=True) if value is not None]
    totals = everything.aggregate(count=Count("id"), average=Avg("rating"))
    profile, _ = Profile.objects.get_or_create(user=request.user)
    # Finished something in a series? Offer what comes next.
    up_next = []
    finished = request.user.backlog_items.filter(
        status=BacklogItem.Status.COMPLETED, catalog_item__isnull=False
    ).exclude(catalog_item__series="").select_related("catalog_item").order_by("-completed_at")[:10]
    for done in finished:
        for candidate in next_in_series(done.catalog_item, request.user, limit=1):
            if candidate not in [row for row, _ in up_next]:
                up_next.append((candidate, done.title))
    return render(
        request,
        "planner/reviews.html",
        {
            "reviews": rows,
            "up_next": up_next[:6],
            "name_form": DisplayNameForm(instance=profile),
            "public_name": profile.public_name,
            "awaiting": request.user.backlog_items.filter(
                status=BacklogItem.Status.COMPLETED, review__isnull=True
            ),
            "active_type": media_type,
            "media_types": MediaType.choices,
            "sort": sort,
            "sorts": [(key, label) for key, (label, _) in REVIEW_SORTS.items()],
            "summary": {
                "count": totals["count"],
                "average": round(totals["average"], 1) if totals["average"] is not None else None,
                "recommend_percent": round(sum(verdicts) / len(verdicts) * 100) if verdicts else None,
            },
        },
    )


@login_required
@require_POST
def set_display_name(request):
    """The name signed on shared reviews. Blank goes back to the username."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    form = DisplayNameForm(request.POST, instance=profile)
    if form.is_valid():
        form.save()
        messages.success(request, f"Shared reviews are now signed {profile.public_name}.")
    else:
        messages.error(request, "That name didn't save. Keep it to 40 characters.")
    return redirect(_safe_next(request, reverse("reviews")))


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
        return redirect(request.POST.get("next") or "reviews")
    profile, _ = Profile.objects.get_or_create(user=request.user)
    return render(
        request,
        "planner/review_form.html",
        {
            "form": form,
            "item": item,
            "review": instance,
            "next_url": request.GET.get("next", ""),
            "public_name": profile.public_name,
        },
    )


# ------------------------------------------------------------ passwords

# Django's own reset and change views, with this app's templates and a couple
# of touches: a mail server that is down becomes a message rather than a 500,
# and a reset that succeeds also confirms the email, since opening the link
# proves the address is theirs just as well as the sign-up link does.


class PasswordResetView(auth_views.PasswordResetView):
    template_name = "registration/password_reset_form.html"
    email_template_name = "registration/password_reset_email.txt"
    subject_template_name = "registration/password_reset_subject.txt"

    def form_valid(self, form):
        try:
            return super().form_valid(form)
        except (smtplib.SMTPException, OSError):
            messages.error(self.request, "We couldn't send the email right now. Please try again in a minute.")
            return self.form_invalid(form)


class PasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "registration/password_reset_done.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["console_email"] = settings.EMAIL_BACKEND.endswith("console.EmailBackend")
        return context


class PasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "registration/password_reset_confirm.html"
    post_reset_login = True

    def form_valid(self, form):
        response = super().form_valid(form)
        profile, _ = Profile.objects.get_or_create(user=self.user)
        if not profile.email_verified:
            profile.email_verified = True
            profile.save(update_fields=["email_verified"])
        messages.success(self.request, "Password changed. You're signed in.")
        return response


class PasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "registration/password_reset_complete.html"


class PasswordChangeView(auth_views.PasswordChangeView):
    template_name = "registration/password_change_form.html"

    def form_valid(self, form):
        messages.success(self.request, "Password changed.")
        return super().form_valid(form)


class PasswordChangeDoneView(auth_views.PasswordChangeDoneView):
    template_name = "registration/password_change_done.html"
