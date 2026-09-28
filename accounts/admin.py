from django.contrib import admin

from .models import TeacherProfile


@admin.register(TeacherProfile)
class TeacherProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "email_verified", "email_verified_at"]
    list_filter = ["email_verified"]
    search_fields = ["user__email"]
    raw_id_fields = ["user"]
