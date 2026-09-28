from django.contrib import admin

from .models import Quiz, QuizQuestion


class QuizQuestionInline(admin.StackedInline):
    model = QuizQuestion
    extra = 0


@admin.register(Quiz)
class QuizAdmin(admin.ModelAdmin):
    list_display = ["title", "owner", "updated_at"]
    search_fields = ["title", "owner__email"]
    raw_id_fields = ["owner"]
    inlines = [QuizQuestionInline]
