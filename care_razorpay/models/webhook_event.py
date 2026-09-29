from django.db import models
from django.utils import timezone

from care.utils.models.base import BaseModel


class RazorpayWebhookEvent(BaseModel):
    """A signature-verified Razorpay webhook, stored before it is processed.

    Lets an event that failed to process be replayed, and makes Razorpay's
    re-deliveries of the same event idempotent.
    """

    class Kind(models.TextChoices):
        PAYMENT_LINK = "payment_link"
        QR_CODE = "qr_code"

    class Status(models.TextChoices):
        RECEIVED = "received"
        PROCESSED = "processed"
        # authentic but not actionable (unknown invoice, malformed payload)
        IGNORED = "ignored"
        FAILED = "failed"

    # X-Razorpay-Event-Id, or a hash of the body when the header is absent
    event_id = models.CharField(max_length=128, unique=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    event = models.CharField(max_length=64, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RECEIVED, db_index=True
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    error_message = models.TextField(blank=True, default="")
    processed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.kind}:{self.event}:{self.event_id}:{self.status}"

    def mark_processed(self, status: str = Status.PROCESSED, detail: str = "") -> None:
        self.status = status
        self.processed_at = timezone.now()
        self.error_message = (detail or "")[:8000]
        self.save(
            update_fields=["status", "processed_at", "error_message", "modified_date"]
        )

    def mark_failed(self, error_message: str) -> None:
        self.status = self.Status.FAILED
        self.attempts = (self.attempts or 0) + 1
        self.error_message = (error_message or "")[:8000]
        self.save(
            update_fields=["status", "attempts", "error_message", "modified_date"]
        )
