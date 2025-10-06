from rest_framework.routers import DefaultRouter

from care_razorpay.api.viewsets.health_check import HealthCheckViewSet

router = DefaultRouter()

router.register("health_check", HealthCheckViewSet, basename="razorpay__health_check")

urlpatterns = router.urls
