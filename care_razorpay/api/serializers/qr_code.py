from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from pydantic import UUID4, BaseModel, field_validator, model_validator


class QRCodeUsage(str, Enum):
    SINGLE_USE = "single_use"
    MULTIPLE_USE = "multiple_use"


class CreateQRCodeRequest(BaseModel):
    invoice_id: UUID4
    usage: QRCodeUsage
    is_amount_fixed: bool
    closes_at: datetime | None = None

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


class QRCodeRecord(BaseModel):
    """What CARE knows about a QR code, independent of the gateway."""

    id: UUID4
    invoice_id: UUID4
    qr_id: str
    image_url: str
    amount: Decimal | None = None
    usage: str = ""
    close_by: datetime | None = None
    status: str
    amount_received: Decimal
    payments_count: int
    created_date: datetime
    modified_date: datetime

    @classmethod
    def from_record(cls, record) -> "QRCodeRecord":
        return cls(
            id=record.external_id,
            invoice_id=record.invoice.external_id,
            qr_id=record.qr_id,
            image_url=record.image_url,
            amount=record.amount,
            usage=record.usage,
            close_by=record.close_by,
            status=record.status,
            amount_received=record.amount_received,
            payments_count=record.payments_count,
            created_date=record.created_date,
            modified_date=record.modified_date,
        )
