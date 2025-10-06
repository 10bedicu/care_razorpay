from rest_framework.routers import DefaultRouter

from care_razorpay.api.viewsets.health_check import HealthCheckViewSet
from care_razorpay.care_razorpay.api.viewsets.payment_link import PaymentLinkViewSet

router = DefaultRouter()

router.register("health_check", HealthCheckViewSet, basename="razorpay__health_check")
router.register("payment_link", PaymentLinkViewSet, basename="razorpay__payment_link")


urlpatterns = router.urls
