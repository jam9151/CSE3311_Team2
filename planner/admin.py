from django.contrib import admin

from .models import BacklogItem, CatalogItem, Commitment, Profile, RecurringBlock, Review, Suggestion


@admin.register(CatalogItem)
class CatalogItemAdmin(admin.ModelAdmin):
    list_display = ["title", "media_type", "genre", "creator", "release_date", "typical_minutes", "rating", "source"]
    list_filter = ["media_type", "source", "genre"]
    search_fields = ["title", "creator", "description"]
    prepopulated_fields = {"slug": ("title",)}


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
    list_display = ["backlog_item", "user", "rating", "created_at"]
    list_filter = ["rating"]


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "email_verified", "day_start", "day_end", "notify_enabled"]
    list_filter = ["email_verified", "notify_enabled"]
    search_fields = ["user__username", "user__email"]

admin.site.register(Commitment)
