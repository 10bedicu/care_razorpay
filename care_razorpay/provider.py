import logging

from abdm.models.payment_order import PAYMENT_ORDER_PAID_STATUSES, PaymentOrder
from abdm.service.v3.payment_providers import (
    PaymentProvider,
    create_payment_reconciliation,
    register_provider,
)

from care_razorpay import payments

logger = logging.getLogger(__name__)


def reconcile_abdm_event(payment_link: dict, payment: dict | None, event: str) -> bool:
    """Reconcile an ABDM scan-and-pay order from a Razorpay webhook.

    Returns True when the link matches an unpaid scan-and-pay order. Partial
    payments are left for the invoice-notes fallback, which records the actual
    amount instead of the full invoice total.
    """
    if event != "payment_link.paid":
        return False

    link_id = payment_link.get("id")
    if not link_id:
        return False

    order = (
        PaymentOrder.objects.filter(payment_link_id=link_id)
        .exclude(status__in=PAYMENT_ORDER_PAID_STATUSES)
        .select_related("invoice__account", "health_facility__facility")
        .first()
    )
    if not order or not order.invoice_id:
        return False

    reference = (payment or {}).get("id") or link_id
    create_payment_reconciliation(order, reference)
    return True


@register_provider
class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    def create_payment_link(self, invoice) -> dict:
        # Not stored on RazorpayPayment. ABDM tracks the link on PaymentOrder,
        # and the webhook reconciles it through reconcile_abdm_event.
        link = payments.create_gateway_link(invoice)
        return {
            "order_number": invoice.number or str(invoice.external_id),
            "payment_link_id": link["id"],
            "payment_url": link["short_url"],
        }

    def reconcile_order(self, order) -> None:
        if (
            not order.invoice_id
            or not order.payment_link_id
            or order.status in PAYMENT_ORDER_PAID_STATUSES
        ):
            return
        link = payments.razorpay_client.payment_link.fetch(order.payment_link_id)
        status = (link.get("status") or "").lower()
        if status != "paid":
            logger.info(
                "Razorpay order %s not paid, status %s",
                order.order_number,
                link.get("status"),
            )
            return
        reference = payments.payment_reference(link, order.payment_link_id)
        create_payment_reconciliation(order, reference)
