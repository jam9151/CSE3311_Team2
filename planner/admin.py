from django.contrib import admin
from .models import AvailabilityWindow, Commitment, MediaItem, ScheduledSession


@admin.register(MediaItem)
class MediaItemAdmin(admin.ModelAdmin):
    list_display = ["title", "user", "media_type", "status", "priority", "estimated_minutes"]
    list_filter = ["media_type", "status", "priority"]
    search_fields = ["title", "user__username"]


admin.site.register(AvailabilityWindow)
admin.site.register(Commitment)
admin.site.register(ScheduledSession)
