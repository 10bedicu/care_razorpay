from django.db import models

from care.utils.models.base import BaseModel


class RazorpayQRCode(BaseModel):
    """Tracks a Razorpay UPI QR code raised for an invoice so staff can check it later."""

    class Status(models.TextChoices):
        ACTIVE = "active"
        CLOSED = "closed"

    qr_id = models.CharField(max_length=255, unique=True)
    invoice = models.ForeignKey("emr.Invoice", on_delete=models.PROTECT)
    # None when the QR code accepts any amount
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    usage = models.CharField(max_length=16, blank=True, default="")
    image_url = models.TextField(blank=True, default="")
    close_by = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    amount_received = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    payments_count = models.PositiveIntegerField(default=0)
