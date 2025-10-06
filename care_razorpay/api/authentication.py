from django.utils.encoding import force_str
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from care_razorpay.settings import plugin_settings
from care_razorpay.utils.razorpay import razorpay_client


class RazorpayWebhookAuthentication(BaseAuthentication):
    """
    Authenticates Razorpay webhook requests by verifying the
    X-Razorpay-Signature header against the raw request body using the
    configured webhook secret.

    On success, returns (None, {"source": "razorpay", "verified": True}).
    We intentionally do not associate a Django user with the request.
    """

    signature_header_name = "HTTP_X_RAZORPAY_SIGNATURE"

    def authenticate(self, request) -> tuple[None, dict] | None:
        signature = request.META.get(self.signature_header_name)
        if not signature:
            raise AuthenticationFailed("Missing X-Razorpay-Signature header")

        try:
            # request.body is bytes; SDK expects str
            body_str = force_str(request.body)
            webhook_secret = plugin_settings.RAZORPAY_WEBHOOK_SECRET
            razorpay_client.utility.verify_webhook_signature(
                body_str, signature, webhook_secret
            )
        except Exception as e:
            raise AuthenticationFailed("Invalid webhook signature") from e

        return None, {"source": "razorpay", "verified": True}
