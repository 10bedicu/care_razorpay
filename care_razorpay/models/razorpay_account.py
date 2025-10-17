from django.db import models

from care.utils.models.base import BaseModel


class RazorpayAccount(BaseModel):
    facility = models.OneToOneField(
        "facility.Facility",
        on_delete=models.PROTECT,
        to_field="external_id",
    )
    account_id = models.CharField(max_length=255, null=False, blank=False)
    is_enabled = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
