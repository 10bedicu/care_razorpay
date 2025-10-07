from datetime import UTC, datetime
from enum import Enum

from django.core.validators import validate_email
from pydantic import UUID4, BaseModel, field_validator, model_validator

from care.emr.models.invoice import Invoice
from care.utils.models.validators import mobile_validator


class CreatePaymentLinkRequest(BaseModel):
    invoice_id: UUID4
    email: str | None = None
    phone_number: str | None = None
    is_partial_payment_allowed: bool = False
    minimum_down_payment: float | None = None
    expires_at: datetime | None = None

    @field_validator("invoice_id")
    @classmethod
    def validate_invoice_id(cls, value):
        if value and not Invoice.objects.filter(external_id=value).exists():
            raise ValueError("Invoice not found")
        return value

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value):
        try:
            mobile_validator(value)
        except Exception as e:
            raise ValueError("Invalid phone number") from e
        return value

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        try:
            validate_email(value)
        except Exception as e:
            raise ValueError("Invalid email") from e
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value):
        if value:
            now = datetime.now(UTC)
            if value <= now:
                raise ValueError("Expiration date must be in the future")
        return value

    @model_validator(mode="after")
    def validate_minimum_down_payment(self):
        if self.is_partial_payment_allowed and not self.minimum_down_payment:
            raise ValueError(
                "Minimum down payment is required when partial payment is allowed"
            )
        return self


class PaymentLinkStatus(str, Enum):
    CREATED = "created"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    PAID = "paid"
    PARTIALLY_PAID = "partially_paid"


class PaymentLink(BaseModel):
    id: str
    short_url: str
    expire_by: datetime | None = None
    created_at: datetime
    amount: float
    amount_paid: float
    status: PaymentLinkStatus

    @field_validator("expire_by", mode="before")
    @classmethod
    def convert_expire_by_to_datetime(cls, value):
        if value is None or value == 0:
            return None
        if isinstance(value, int):
            return datetime.fromtimestamp(value, tz=UTC)
        return value

    @field_validator("created_at", mode="before")
    @classmethod
    def convert_created_at_to_datetime(cls, value):
        if isinstance(value, int):
            return datetime.fromtimestamp(value, tz=UTC)
        return value
