from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from django.core.validators import validate_email
from pydantic import UUID4, BaseModel, field_validator, model_validator

from care.utils.models.validators import mobile_validator


class CreatePaymentLinkRequest(BaseModel):
    invoice_id: UUID4
    email: str | None = None
    phone_number: str | None = None
    is_partial_payment_allowed: bool = False
    minimum_down_payment: float | None = None
    expires_at: datetime | None = None

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
            if value.tzinfo is None:
                raise ValueError("Expiration date must include a timezone")
            if value < datetime.now(UTC) + timedelta(minutes=15):
                raise ValueError(
                    "Expiration date must be at least 15 minutes in the future"
                )
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

    @field_validator("amount", "amount_paid")
    @classmethod
    def convert_amount_to_float(cls, value):
        return value / 100


class PaymentRecord(BaseModel):
    """What CARE knows about a payment link, independent of the gateway."""

    id: UUID4
    invoice_id: UUID4
    payment_link_id: str
    payment_url: str
    amount: Decimal
    status: str
    reference: str = ""
    expires_at: datetime | None = None
    needs_review: bool = False
    review_reason: str = ""
    created_date: datetime
    modified_date: datetime

    @classmethod
    def from_payment(cls, payment) -> "PaymentRecord":
        return cls(
            id=payment.external_id,
            invoice_id=payment.invoice.external_id,
            payment_link_id=payment.payment_link_id,
            payment_url=payment.payment_url,
            amount=payment.amount,
            status=payment.status,
            reference=payment.reference,
            expires_at=payment.expires_at,
            needs_review=payment.needs_review,
            review_reason=payment.review_reason,
            created_date=payment.created_date,
            modified_date=payment.modified_date,
        )
