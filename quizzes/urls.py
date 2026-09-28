from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter(trailing_slash=False)
# Other apps mount routers under /api/ too; none of them should own the /api/ root.
router.include_root_view = False
router.register("quizzes", views.QuizViewSet, basename="quiz")

urlpatterns = router.urls
