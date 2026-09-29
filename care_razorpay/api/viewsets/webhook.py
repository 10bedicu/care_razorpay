from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.viewsets import GenericViewSet

from care_razorpay import webhooks
from care_razorpay.api.authentication import RazorpayWebhookAuthentication
from care_razorpay.models import RazorpayWebhookEvent


class WebhookViewSet(GenericViewSet):
    authentication_classes = (RazorpayWebhookAuthentication,)
    permission_classes = (AllowAny,)

    @extend_schema(
        description="Handle Razorpay payment link events",
    )
    @action(detail=False, methods=["POST"], url_path="payment_link")
    def payment_link(self, request):
        return webhooks.handle(request, RazorpayWebhookEvent.Kind.PAYMENT_LINK)

    @extend_schema(
        description="Handle Razorpay QR code events",
    )
    @action(detail=False, methods=["POST"], url_path="qr_code")
    def qr_code(self, request):
        return webhooks.handle(request, RazorpayWebhookEvent.Kind.QR_CODE)
