import logging
from datetime import timedelta

from abdm.models.payment_order import (
    PAYMENT_ORDER_PAID_STATUSES,
    PaymentOrder,
    PaymentOrderStatus,
)
from abdm.service.v3.payment_providers import (
    PaymentProvider,
    close_payment_order,
    create_payment_reconciliation,
    register_provider,
    remember_transaction_reference,
)
from abdm.settings import plugin_settings as abdm_settings

from care.utils.time_util import care_now
from care_razorpay import payments

logger = logging.getLogger(__name__)

# Webhook events that settle a scan-and-pay order. A partial payment is not
# one of them; that still falls through to the invoice-notes path.
SCAN_PAY_LINK_EVENTS = {
    "payment_link.paid": "paid",
    "payment_link.cancelled": "cancelled",
    "payment_link.expired": "expired",
}


def _scan_pay_order(link_id: str):
    return (
        PaymentOrder.objects.filter(payment_link_id=link_id)
        .select_related(
            "invoice__account", "invoice__facility", "health_facility__facility"
        )
        .first()
    )


def apply_scan_pay_status(order, status: str, *, reference: str, amount=None) -> None:
    """Apply a Razorpay link status to a scan-and-pay order.

    Paid records a reconciliation, and that signal notifies the PHR. Cancelled
    and expired close the order, which notifies too. Any other status leaves
    the order payable.
    """
    status = (status or "").lower()
    if status == "paid":
        create_payment_reconciliation(order, reference, amount=amount)
    elif status == "cancelled":
        close_payment_order(order, PaymentOrderStatus.CANCELED, reference=reference)
    elif status == "expired":
        close_payment_order(order, PaymentOrderStatus.EXPIRED, reference=reference)
    else:
        remember_transaction_reference(order, reference)
        logger.info(
            "Razorpay order %s not paid, status %s", order.order_number, status
        )


def reconcile_abdm_event(payment_link: dict, payment: dict | None, event: str) -> bool:
    """Reconcile an ABDM scan-and-pay order from a Razorpay webhook.

    Returns True when the link matches a scan-and-pay order. Partial payments
    are left for the invoice-notes fallback, which records the actual amount
    instead of the full invoice total.
    """
    status = SCAN_PAY_LINK_EVENTS.get(event)
    if status is None:
        return False

    link_id = payment_link.get("id")
    if not link_id:
        return False

    order = _scan_pay_order(link_id)
    if not order or not order.invoice_id:
        return False

    reference = (payment or {}).get("id") or link_id
    amount = (payment or {}).get("amount")
    apply_scan_pay_status(
        order,
        status,
        reference=reference,
        amount=payments.paise_to_rupees(amount) if amount is not None else None,
    )
    return True


@register_provider
class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    def create_payment_link(self, invoice) -> dict:
        # Not stored on RazorpayPayment. ABDM tracks the link on PaymentOrder,
        # and the webhook reconciles it through reconcile_abdm_event.
        # The link should stop being payable when ABDM gives up on the order
        # (Razorpay rejects expiries under 15 minutes).
        max_age = max(
            abdm_settings.ABDM_SCAN_AND_PAY_ORDER_MAX_AGE, payments.MIN_EXPIRY_SECONDS
        )
        link = payments.create_gateway_link(
            invoice, expires_at=care_now() + timedelta(seconds=max_age)
        )
        return {
            "order_number": invoice.number or str(invoice.external_id),
            "payment_link_id": link["id"],
            "payment_url": link["short_url"],
            "amount": invoice.total_gross,
        }

    def reconcile_order(self, order) -> bool:
        if (
            not order.invoice_id
            or not order.payment_link_id
            or order.status in PAYMENT_ORDER_PAID_STATUSES
        ):
            return False
        link = payments.razorpay_client.payment_link.fetch(order.payment_link_id)
        status = (link.get("status") or "").lower()
        expires_at = payments.link_expires_at(link)
        if (
            status not in ("paid", "cancelled")
            and expires_at
            and expires_at <= care_now()
        ):
            status = "expired"
        reference = payments.payment_reference(link, order.payment_link_id)
        amount_paid = link.get("amount_paid")
        apply_scan_pay_status(
            order,
            status,
            reference=reference,
            amount=payments.paise_to_rupees(amount_paid) if amount_paid else None,
        )
        return True
