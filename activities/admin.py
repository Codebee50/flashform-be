from django.contrib import admin

from .models import Activity, ActivityQuestion, Participant, Response


class ActivityQuestionInline(admin.TabularInline):
    model = ActivityQuestion
    extra = 0


@admin.register(Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ["id", "room", "type", "mode", "status", "version", "started_at", "ended_at"]
    list_filter = ["status", "type", "mode"]
    search_fields = ["room__code", "room__name", "quiz_title"]
    raw_id_fields = ["room", "source_quiz"]
    inlines = [ActivityQuestionInline]


@admin.register(Participant)
class ParticipantAdmin(admin.ModelAdmin):
    list_display = ["name", "activity", "is_removed", "created_at", "finished_at"]
    list_filter = ["is_removed"]
    search_fields = ["name"]
    raw_id_fields = ["activity"]
    exclude = ["token_hash"]


@admin.register(Response)
class ResponseAdmin(admin.ModelAdmin):
    list_display = ["participant", "question", "choice_index", "text_answer", "is_correct", "is_locked"]
    list_filter = ["is_locked", "is_correct"]
    raw_id_fields = ["participant", "question"]
