from django.contrib import admin

from .models import Room


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "owner", "is_locked", "created_at"]
    list_filter = ["is_locked"]
    search_fields = ["name", "code", "owner__email"]
    raw_id_fields = ["owner"]
