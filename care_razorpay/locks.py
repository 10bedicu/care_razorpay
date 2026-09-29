from django.conf import settings

from care.utils.lock import Lock


class RazorpayPaymentLock(Lock):
    """Serialises settlement of one payment across webhook and polling workers."""

    def __init__(self, payment, timeout=settings.LOCK_TIMEOUT):
        self.key = f"lock:razorpay:payment:{payment.id}"
        self.timeout = timeout
