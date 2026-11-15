from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("healthz/", views.healthz, name="healthz"),
    path("register/", views.register, name="register"),
    path("verify/", views.verify_pending, name="verify_pending"),
    path("verify/resend/", views.resend_verification, name="resend_verification"),
    path("verify/<str:token>/", views.verify_email, name="verify_email"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("dashboard/walkthrough/hide/", views.dismiss_walkthrough, name="dismiss_walkthrough"),
    path("dashboard/panels/", views.dashboard_panels, name="dashboard_panels"),
    # Discover and media details
    path("discover/", views.discover, name="discover"),
    path("discover/lookup/", views.lookup_catalog, name="lookup_catalog"),
    path("media/<slug:slug>/", views.catalog_detail, name="catalog_detail"),
    path("media/<int:pk>/add/", views.add_from_catalog, name="add_from_catalog"),
    path("media/<int:pk>/watch-together/", views.watch_together, name="watch_together"),
    path("invites/<int:pk>/<str:action>/", views.respond_to_invite, name="respond_to_invite"),
    # Backlog
    path("backlog/", views.backlog, name="backlog"),
    path("backlog/new/", views.backlog_edit, name="backlog_add"),
    path("backlog/import/", views.import_backlog, name="import_backlog"),
    path("backlog/<int:pk>/edit/", views.backlog_edit, name="backlog_edit"),
    path("backlog/<int:pk>/priority/", views.backlog_priority, name="backlog_priority"),
    path("backlog/<int:pk>/delete/", views.backlog_delete, name="backlog_delete"),
    path("backlog/<int:pk>/log/", views.log_time, name="log_time"),
    # Schedule and calendar
    path("schedule/", views.weekly_schedule, name="weekly_schedule"),
    path("schedule/import/", views.import_schedule, name="import_schedule"),
    path("schedule/block/<int:pk>/delete/", views.delete_block, name="delete_block"),
    path("schedule/block/<int:pk>/edit/", views.edit_block, name="edit_block"),
    path("schedule/block/<int:pk>/skip/", views.skip_block, name="skip_block"),
    path("schedule/skip/<int:pk>/delete/", views.unskip_block, name="unskip_block"),
    path("schedule/day-off/", views.day_off, name="day_off"),
    path("schedule/commitment/new/", views.add_commitment, name="add_commitment"),
    path("schedule/commitment/<int:pk>/edit/", views.edit_commitment, name="edit_commitment"),
    path("schedule/commitment/<int:pk>/delete/", views.delete_commitment, name="delete_commitment"),
    path("notifications/upcoming/", views.reminders_json, name="reminders_json"),
    # Background push: the service worker and the subscription endpoints.
    path("sw.js", views.service_worker, name="service_worker"),
    path("notifications/push/key/", views.push_key, name="push_key"),
    path("notifications/push/subscribe/", views.push_subscribe, name="push_subscribe"),
    path("notifications/push/unsubscribe/", views.push_unsubscribe, name="push_unsubscribe"),
    path("calendar/", views.calendar, name="calendar"),
    path("calendar/plan/", views.plan_session, name="plan_session"),
    path("semester/", views.semester, name="semester"),
    # Suggestions
    path("suggestions/refresh/", views.refresh_suggestions, name="refresh_suggestions"),
    path("suggestions/plan-week/", views.plan_week, name="plan_week"),
    path("suggestions/accept-week/", views.accept_week, name="accept_week"),
    path("suggestions/<int:pk>/<str:action>/", views.respond_to_suggestion, name="respond_to_suggestion"),
    # Progress
    path("progress/", views.stats, name="stats"),
    # Reviews
    path("reviews/", views.reviews, name="reviews"),
    path("reviews/<int:pk>/write/", views.write_review, name="write_review"),
    path("reviews/name/", views.set_display_name, name="set_display_name"),
    # Settings, data export and account deletion
    path("settings/", views.settings_page, name="settings"),
    path("settings/export/", views.export_data, name="export_data"),
    path("settings/delete/", views.delete_account, name="delete_account"),
    path("settings/tokens/new/", views.create_api_token, name="create_api_token"),
    path("settings/tokens/<int:pk>/revoke/", views.revoke_api_token, name="revoke_api_token"),
    # Read-only JSON API, bearer tokens from Settings
    path("api/v1/me/", views.api_me, name="api_me"),
    path("api/v1/backlog/", views.api_backlog, name="api_backlog"),
    path("api/v1/day/", views.api_day, name="api_day"),
    path("api/v1/progress/", views.api_progress, name="api_progress"),
]
