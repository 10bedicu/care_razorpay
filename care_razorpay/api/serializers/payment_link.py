from datetime import datetime

from django.core.validators import validate_email
from pydantic import UUID4, BaseModel, field_validator

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

    @field_validator("is_partial_payment_allowed")
    @classmethod
    def validate_is_partial_payment_allowed(cls, value):
        if value and not cls.minimum_down_payment:
            raise ValueError(
                "Minimum down payment is required when partial payment is allowed"
            )
        return value


class CreatePaymentLinkResponse(BaseModel):
    id: str
    short_url: str
