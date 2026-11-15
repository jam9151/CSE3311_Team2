from django.contrib import admin

from .models import (
    BacklogItem,
    CatalogItem,
    Commitment,
    Profile,
    PushSubscription,
    RecurringBlock,
    Review,
    Suggestion,
    TimeLog,
)


@admin.register(CatalogItem)
class CatalogItemAdmin(admin.ModelAdmin):
    list_display = [
        "title", "media_type", "genre", "creator", "release_date", "typical_minutes", "rating", "source", "locked",
    ]
    list_filter = ["media_type", "source", "locked", "genre"]
    search_fields = ["title", "creator", "description"]
    prepopulated_fields = {"slug": ("title",)}
    readonly_fields = ["source", "source_id"]

    def save_model(self, request, obj, form, change):
        # Editing an imported row by hand locks it, otherwise the next import
        # would quietly put the old values back. Untick "Keep my edits" to let
        # imports refresh it again.
        if change and obj.source and "locked" not in form.changed_data and form.changed_data:
            obj.locked = True
        super().save_model(request, obj, form, change)


@admin.register(BacklogItem)
class BacklogItemAdmin(admin.ModelAdmin):
    list_display = ["title", "user", "media_type", "status", "priority", "estimated_minutes", "minutes_completed"]
    list_filter = ["media_type", "status", "priority"]
    search_fields = ["title", "user__username"]


@admin.register(RecurringBlock)
class RecurringBlockAdmin(admin.ModelAdmin):
    list_display = ["label", "user", "kind", "weekday", "start_time", "end_time"]
    list_filter = ["kind", "weekday"]


@admin.register(Suggestion)
class SuggestionAdmin(admin.ModelAdmin):
    list_display = ["backlog_item", "user", "date", "start_time", "minutes", "status"]
    list_filter = ["status", "date"]


@admin.register(Review)
class ReviewAdmin(admin.ModelAdmin):
    list_display = ["backlog_item", "user", "rating", "recommend", "shared", "created_at"]
    list_filter = ["rating", "recommend", "shared"]


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "email_verified", "day_start", "day_end", "notify_enabled"]
    list_filter = ["email_verified", "notify_enabled"]
    search_fields = ["user__username", "user__email"]

@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ["user", "user_agent", "created_at", "last_sent_at", "failures"]
    readonly_fields = ["endpoint", "p256dh", "auth", "user_agent", "created_at", "last_sent_at"]
    search_fields = ["user__username"]


@admin.register(TimeLog)
class TimeLogAdmin(admin.ModelAdmin):
    list_display = ["backlog_item", "user", "date", "minutes", "note"]
    list_filter = ["date"]
    search_fields = ["backlog_item__title", "user__username"]


admin.site.register(Commitment)
