from django import template

from planner.services import humanize_minutes

register = template.Library()


@register.filter
def duration(minutes):
    """{{ item.remaining_minutes|duration }} -> '2h 35m'"""
    if minutes in (None, ""):
        return ""
    return humanize_minutes(minutes)


@register.filter
def stars(rating):
    """Five characters, filled up to the rating: 3 -> '★★★☆☆'."""
    rating = int(rating or 0)
    return "★" * rating + "☆" * (5 - rating)
