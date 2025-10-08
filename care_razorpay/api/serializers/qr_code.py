from datetime import UTC, datetime
from enum import Enum

from pydantic import UUID4, BaseModel, field_validator, model_validator

from care.emr.models.invoice import Invoice


class QRCodeUsage(str, Enum):
    SINGLE_USE = "single_use"
    MULTIPLE_USE = "multiple_use"


class CreateQRCodeRequest(BaseModel):
    invoice_id: UUID4
    usage: QRCodeUsage
    is_amount_fixed: bool
    closes_at: datetime | None = None

    @field_validator("invoice_id")
    @classmethod
    def validate_invoice_id(cls, value):
        if value and not Invoice.objects.filter(external_id=value).exists():
            raise ValueError("Invoice not found")
        return value

    @field_validator("closes_at")
    @classmethod
    def validate_expires_at(cls, value):
        if value:
            now = datetime.now(UTC)
            if value <= now:
                raise ValueError("Closing date must be in the future")
        return value

    @model_validator(mode="after")
    def validate_is_amount_fixed(self):
        if self.usage == QRCodeUsage.SINGLE_USE and not self.is_amount_fixed:
            raise ValueError("Amount should be fixed when usage is single use")
        return self


class QRCodeStatus(str, Enum):
    ACTIVE = "active"
    CLOSED = "closed"


class QRCode(BaseModel):
    id: str
    image_url: str
    close_by: datetime | None = None
    created_at: datetime
    payment_amount: float
    payments_amount_received: float
    payments_count_received: int
    status: QRCodeStatus

    @field_validator("close_by", mode="before")
    @classmethod
    def convert_close_by_to_datetime(cls, value):
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

    @field_validator("payment_amount", "payments_amount_received")
    @classmethod
    def convert_amount_to_float(cls, value):
        return value / 100
