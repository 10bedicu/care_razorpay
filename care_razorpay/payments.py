import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from django.utils.dateparse import parse_datetime

from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.account.sync_items import rebalance_account_task
from care.emr.resources.payment_reconciliation.spec import (
    PaymentReconciliationIssuerTypeOptions,
    PaymentReconciliationKindOptions,
    PaymentReconciliationOutcomeOptions,
    PaymentReconciliationPaymentMethodOptions,
    PaymentReconciliationStatusOptions,
    PaymentReconciliationTypeOptions,
)
from care.utils.time_util import care_now
from care_razorpay.models import RazorpayPayment
from care_razorpay.settings import plugin_settings as settings
from care_razorpay.utils.razorpay import razorpay_client

logger = logging.getLogger(__name__)

OPEN_STATUSES = {
    RazorpayPayment.Status.CREATED,
    RazorpayPayment.Status.PARTIALLY_PAID,
}


class RazorpayError(Exception):
    pass


def amount_paise(amount) -> int:
    return int(round(float(amount) * 100))


def paise_to_rupees(paise) -> Decimal:
    return Decimal(str(paise)) / Decimal(100)


def invoice_notes(invoice) -> dict:
    return {
        "invoice_id": str(invoice.external_id),
        "account_id": str(invoice.account.external_id),
        "patient_id": str(invoice.patient.external_id),
        "facility_id": str(invoice.facility.external_id),
    }


def route_account(invoice):
    if not hasattr(invoice.facility, "razorpayaccount"):
        raise RazorpayError("Razorpay account not found for facility")
    return invoice.facility.razorpayaccount


def create_gateway_link(
    invoice,
    *,
    accept_partial: bool = False,
    minimum_down_payment=None,
    email: str | None = None,
    phone_number: str | None = None,
    notify_sms: bool = False,
    notify_email: bool = False,
    reminder_enable: bool = False,
    expires_at=None,
) -> dict:
    """Create a Razorpay payment link that settles to the facility route account."""
    account = route_account(invoice)
    paise = amount_paise(invoice.total_gross)
    notes = invoice_notes(invoice)
    contact = phone_number or getattr(invoice.patient, "phone_number", None)
    customer = {"name": invoice.patient.name}
    if contact:
        customer["contact"] = str(contact)
    if email:
        customer["email"] = email

    payload = {
        "amount": paise,
        "currency": "INR",
        "accept_partial": accept_partial,
        "description": invoice.title,
        "customer": customer,
        "notify": {"sms": notify_sms, "email": notify_email},
        "reminder_enable": reminder_enable,
        "notes": notes,
        "options": {
            "checkout": {"method": {"netbanking": True, "card": True}},
            "order": {
                "transfers": [
                    {
                        "account": account.account_id,
                        "amount": paise,
                        "currency": "INR",
                        "notes": notes,
                    }
                ]
            },
        },
    }
    if minimum_down_payment is not None:
        payload["first_min_partial_amount"] = minimum_down_payment
    if expires_at is not None:
        payload["expire_by"] = int(expires_at.timestamp())

    try:
        link = razorpay_client.payment_link.create(payload)
    except Exception as exc:
        raise RazorpayError(str(exc)) from exc

    if not link.get("id") or not link.get("short_url"):
        raise RazorpayError("Razorpay did not return a payment link")
    return link


def create_payment(invoice, **options) -> tuple[RazorpayPayment, dict]:
    """Generate a Razorpay payment link for an invoice and persist it."""
    link = create_gateway_link(invoice, **options)
    payment = RazorpayPayment.objects.create(
        payment_link_id=link["id"],
        invoice=invoice,
        amount=invoice.total_gross,
        payment_url=link.get("short_url") or "",
    )
    return payment, link


def _payment_items(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        return raw["items"]
    return []


def captured_payments(link: dict) -> list[dict]:
    """Return captured payments for a payment-link payload, oldest first."""
    items = _payment_items(link.get("payments"))
    if not items and link.get("order_id"):
        try:
            result = razorpay_client.order.payments(link["order_id"])
        except Exception:
            logger.exception(
                "Failed to fetch Razorpay payments for order %s", link.get("order_id")
            )
            result = None
        if isinstance(result, dict):
            items = _payment_items(result.get("items"))

    captured = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("status") not in (None, "captured"):
            continue
        payment_id = item.get("id") or item.get("payment_id")
        if not payment_id or item.get("amount") is None:
            continue
        captured.append({"id": payment_id, "amount": item["amount"]})
    return captured


def payment_reference(link: dict, fallback: str = "") -> str:
    captured = captured_payments(link)
    if captured:
        return captured[-1]["id"]
    return fallback or link.get("id") or ""


def _record_reconciliation(payment: RazorpayPayment, reference, amount, paid_at=None):
    if reference and PaymentReconciliation.objects.filter(
        target_invoice=payment.invoice,
        reference_number=reference,
    ).exists():
        return
    invoice = payment.invoice
    payment_reconciliation = PaymentReconciliation.objects.create(
        target_invoice=invoice,
        facility=invoice.facility,
        account=invoice.account,
        reconciliation_type=PaymentReconciliationTypeOptions.payment.value,
        status=PaymentReconciliationStatusOptions.active.value,
        kind=PaymentReconciliationKindOptions.online.value,
        issuer_type=PaymentReconciliationIssuerTypeOptions.patient.value,
        outcome=PaymentReconciliationOutcomeOptions.complete.value,
        method=PaymentReconciliationPaymentMethodOptions.debc.value,
        payment_datetime=paid_at or care_now(),
        amount=amount,
        tendered_amount=amount,
        returned_amount=0,
        is_credit_note=False,
        authorization="",
        disposition="",
        note="Payment made via Razorpay's payment link.",
        reference_number=reference or "",
    )
    rebalance_account_task.delay(payment_reconciliation.account.id)


def _paid_at(payment: dict | None):
    created_at = (payment or {}).get("created_at")
    if isinstance(created_at, (int, float)):
        return datetime.fromtimestamp(created_at, UTC)
    if isinstance(created_at, str):
        return parse_datetime(created_at)
    return None


def _mark(payment: RazorpayPayment, status: str) -> None:
    if payment.status not in OPEN_STATUSES:
        return
    payment.status = status
    payment.save(update_fields=["status", "modified_date"])


def _settle(payment: RazorpayPayment, reference, amount, paid_at, *, final: bool):
    if payment.status not in OPEN_STATUSES:
        return
    _record_reconciliation(payment, reference, amount, paid_at)
    payment.reference = reference or payment.reference
    payment.status = (
        RazorpayPayment.Status.PAID
        if final
        else RazorpayPayment.Status.PARTIALLY_PAID
    )
    payment.save(update_fields=["status", "reference", "modified_date"])


def reconcile_event(payment_link: dict, payment: dict | None, event: str) -> bool:
    """Reconcile a standalone payment from a Razorpay webhook event.

    Returns True when the link belongs to a standalone payment (handled here),
    False when it is unknown (for example an ABDM scan-and-pay order).
    """
    record = (
        RazorpayPayment.objects.filter(payment_link_id=payment_link.get("id"))
        .select_related("invoice__account", "invoice__facility")
        .first()
    )
    if not record:
        return False

    reference = (payment or {}).get("id") or record.payment_link_id
    if payment and payment.get("amount") is not None:
        amount = paise_to_rupees(payment["amount"])
    else:
        amount = record.amount

    if event == "payment_link.paid":
        _settle(record, reference, amount, _paid_at(payment), final=True)
    elif event == "payment_link.partially_paid":
        _settle(record, reference, amount, _paid_at(payment), final=False)
    elif event == "payment_link.cancelled":
        _mark(record, RazorpayPayment.Status.CANCELLED)
    elif event == "payment_link.expired":
        _mark(record, RazorpayPayment.Status.EXPIRED)
    return True


def apply_gateway_status(payment: RazorpayPayment, link: dict) -> None:
    """Move a pending payment based on a fetched payment-link payload."""
    status = (link.get("status") or "").lower()
    if status in ("paid", "partially_paid"):
        _settle_from_link(payment, link, final=status == "paid")
    elif status == "cancelled":
        _mark(payment, RazorpayPayment.Status.CANCELLED)
    elif status == "expired":
        _mark(payment, RazorpayPayment.Status.EXPIRED)
    else:
        logger.info(
            "Razorpay payment link %s still pending, status %s",
            payment.payment_link_id,
            link.get("status"),
        )


def _settle_from_link(payment: RazorpayPayment, link: dict, *, final: bool) -> None:
    if payment.status not in OPEN_STATUSES:
        return
    captured = captured_payments(link)
    if captured:
        for item in captured:
            _record_reconciliation(
                payment, item["id"], paise_to_rupees(item["amount"])
            )
        reference = captured[-1]["id"]
    elif final and not payment.reference:
        amount_paid = link.get("amount_paid")
        amount = paise_to_rupees(amount_paid) if amount_paid else payment.amount
        reference = payment.payment_link_id
        _record_reconciliation(payment, reference, amount)
    elif final:
        # A webhook already recorded at least one payment id. The gateway says
        # the link is fully paid, so close it without adding a second lump sum.
        reference = payment.reference
    else:
        return

    payment.reference = reference or payment.reference
    payment.status = (
        RazorpayPayment.Status.PAID
        if final
        else RazorpayPayment.Status.PARTIALLY_PAID
    )
    payment.save(update_fields=["status", "reference", "modified_date"])


def poll_pending_payments() -> None:
    """Poll Razorpay for each pending payment; expire stale unpaid ones."""
    cutoff = care_now() - timedelta(seconds=settings.RAZORPAY_PAYMENT_MAX_AGE)
    pending = RazorpayPayment.objects.filter(status__in=OPEN_STATUSES).select_related(
        "invoice__account", "invoice__facility"
    )
    for payment in pending:
        if (
            payment.status == RazorpayPayment.Status.CREATED
            and payment.created_date
            and payment.created_date < cutoff
        ):
            _mark(payment, RazorpayPayment.Status.EXPIRED)
            continue
        try:
            link = razorpay_client.payment_link.fetch(payment.payment_link_id)
        except Exception:
            logger.exception(
                "Failed to poll Razorpay status for link %s", payment.payment_link_id
            )
            continue
        apply_gateway_status(payment, link)
