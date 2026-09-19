from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("register/", views.register, name="register"),
    path("dashboard/", views.dashboard, name="dashboard"),
    # Discover and media details
    path("discover/", views.discover, name="discover"),
    path("media/<slug:slug>/", views.catalog_detail, name="catalog_detail"),
    path("media/<int:pk>/add/", views.add_from_catalog, name="add_from_catalog"),
    # Backlog
    path("backlog/", views.backlog, name="backlog"),
    path("backlog/new/", views.backlog_edit, name="backlog_add"),
    path("backlog/<int:pk>/edit/", views.backlog_edit, name="backlog_edit"),
    path("backlog/<int:pk>/priority/", views.backlog_priority, name="backlog_priority"),
    path("backlog/<int:pk>/delete/", views.backlog_delete, name="backlog_delete"),
    # Schedule and calendar
    path("schedule/", views.weekly_schedule, name="weekly_schedule"),
    path("schedule/block/<int:pk>/delete/", views.delete_block, name="delete_block"),
    path("schedule/commitment/new/", views.add_commitment, name="add_commitment"),
    path("calendar/", views.calendar, name="calendar"),
    # Suggestions
    path("suggestions/refresh/", views.refresh_suggestions, name="refresh_suggestions"),
    path("suggestions/<int:pk>/<str:action>/", views.respond_to_suggestion, name="respond_to_suggestion"),
    # Reviews
    path("reviews/", views.reviews, name="reviews"),
    path("reviews/<int:pk>/write/", views.write_review, name="write_review"),
]
