from django.db import models

from care.utils.models.base import BaseModel


class RazorpayPayment(BaseModel):
    """Tracks a standalone Razorpay payment so webhooks and polling can reconcile it.

    Independent of ABDM; scan-and-pay orders are tracked by ABDM's PaymentOrder.
    """

    class Status(models.TextChoices):
        CREATED = "created"
        PARTIALLY_PAID = "partially_paid"
        PAID = "paid"
        EXPIRED = "expired"
        CANCELLED = "cancelled"
        # replaced by a newer link for the same invoice (e.g. amount changed)
        SUPERSEDED = "superseded"

    payment_link_id = models.CharField(max_length=255, unique=True)
    invoice = models.ForeignKey("emr.Invoice", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CREATED
    )
    reference = models.CharField(max_length=255, blank=True, default="")
    payment_url = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    needs_review = models.BooleanField(default=False, db_index=True)
    review_reason = models.TextField(blank=True, default="")

    def flag_for_review(self, reason: str) -> None:
        self.needs_review = True
        self.review_reason = f"{self.review_reason}\n{reason}".strip()
