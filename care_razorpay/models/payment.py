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

    payment_link_id = models.CharField(max_length=255, unique=True)
    invoice = models.ForeignKey("emr.Invoice", on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.CREATED
    )
    reference = models.CharField(max_length=255, blank=True, default="")
    payment_url = models.TextField(blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
