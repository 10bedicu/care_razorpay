from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from care.emr.models.invoice import Invoice
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from care.security.authorization.base import AuthorizationController

# collecting a payment is the same privilege as recording one
WRITE_PERMISSION = "can_write_payment_reconciliation_in_facility"
READ_PERMISSION = "can_read_payment_reconciliation_in_facility"


def authorize(user, facility, permission: str = WRITE_PERMISSION) -> None:
    if not AuthorizationController.call(permission, user, facility):
        raise PermissionDenied("Cannot collect payments for this facility")


def collectible_invoice(user, invoice_id) -> Invoice:
    """The invoice a payment may be collected for, after authorization."""
    invoice = (
        Invoice.objects.select_related("facility", "account", "patient")
        .filter(external_id=invoice_id)
        .first()
    )
    if not invoice:
        raise NotFound("Invoice not found")
    authorize(user, invoice.facility)
    if invoice.status != InvoiceStatusOptions.issued.value:
        msg = f"Invoice must be issued to collect payment (status: {invoice.status})"
        raise ValidationError(msg)
    if invoice.is_refund:
        raise ValidationError("Cannot collect payment for a refund invoice")
    return invoice
