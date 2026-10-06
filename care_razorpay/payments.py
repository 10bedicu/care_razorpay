import logging
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q, Sum
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
from care.utils.lock import ObjectLocked
from care.utils.rounding.rounding import care_round
from care.utils.time_util import care_now
from care_razorpay.locks import RazorpayPaymentLock, RazorpayQRCodeLock
from care_razorpay.models import RazorpayPayment, RazorpayQRCode
from care_razorpay.settings import plugin_settings as settings
from care_razorpay.utils.razorpay import razorpay_client

logger = logging.getLogger(__name__)

OPEN_STATUSES = {
    RazorpayPayment.Status.CREATED,
    RazorpayPayment.Status.PARTIALLY_PAID,
}
MIN_EXPIRY_SECONDS = 15 * 60


class RazorpayError(Exception):
    pass


def default_expires_at() -> datetime:
    max_age = max(settings.RAZORPAY_PAYMENT_MAX_AGE, MIN_EXPIRY_SECONDS)
    return care_now() + timedelta(seconds=max_age)


def link_expires_at(link: dict) -> datetime | None:
    expire_by = link.get("expire_by")
    if isinstance(expire_by, (int, float)) and expire_by > 0:
        return datetime.fromtimestamp(expire_by, UTC)
    return None


def payment_deadline(payment: RazorpayPayment) -> datetime | None:
    """When the link stops being payable at the gateway."""
    if payment.expires_at:
        return payment.expires_at
    if payment.created_date:
        return payment.created_date + timedelta(
            seconds=settings.RAZORPAY_PAYMENT_MAX_AGE
        )
    return None


def polling_deadline(payment: RazorpayPayment) -> datetime | None:
    """Deadline plus a grace period for the gateway to report a last-second payment."""
    deadline = payment_deadline(payment)
    if not deadline:
        return None
    return deadline + timedelta(seconds=settings.RAZORPAY_EXPIRY_GRACE_SECONDS)


def amount_paise(amount) -> int:
    return int(
        (Decimal(str(amount)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )


def paise_to_rupees(paise) -> Decimal:
    return Decimal(str(paise)) / Decimal(100)


def invoice_outstanding(invoice) -> Decimal:
    """Invoice total less settled payments, plus credit notes (as the account does)."""
    totals = PaymentReconciliation.objects.filter(
        target_invoice=invoice,
        status=PaymentReconciliationStatusOptions.active.value,
        outcome=PaymentReconciliationOutcomeOptions.complete.value,
    ).aggregate(
        paid=Sum("amount", filter=Q(is_credit_note=False)),
        credited=Sum("amount", filter=Q(is_credit_note=True)),
    )
    paid = totals["paid"] or Decimal(0)
    credited = totals["credited"] or Decimal(0)
    return care_round(Decimal(invoice.total_gross) - paid + credited)


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
    amount=None,
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
    paise = amount_paise(invoice.total_gross if amount is None else amount)
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
    if expires_at is None:
        expires_at = default_expires_at()
    payload["expire_by"] = int(expires_at.timestamp())

    try:
        link = razorpay_client.payment_link.create(payload)
    except Exception as exc:
        raise RazorpayError(str(exc)) from exc

    if not link.get("id") or not link.get("short_url"):
        raise RazorpayError("Razorpay did not return a payment link")
    return link


def create_payment(
    invoice, amount: Decimal | None = None, **options
) -> tuple[RazorpayPayment, dict]:
    """Generate a Razorpay payment link for an invoice and persist it.

    Defaults to the invoice's outstanding balance so a partially settled
    invoice is never charged in full again.
    """
    amount = care_round(amount if amount is not None else invoice_outstanding(invoice))
    if amount <= 0:
        raise RazorpayError("Invoice has no outstanding balance")
    link = create_gateway_link(invoice, amount=amount, **options)
    payment = RazorpayPayment.objects.create(
        payment_link_id=link["id"],
        invoice=invoice,
        amount=amount,
        payment_url=link.get("short_url") or "",
        expires_at=link_expires_at(link),
    )
    return payment, link


def pending_payment(invoice) -> RazorpayPayment | None:
    """The invoice's newest link that is still payable at the gateway, if any."""
    now = care_now()
    for payment in RazorpayPayment.objects.filter(
        invoice=invoice, status__in=OPEN_STATUSES
    ).order_by("-created_date"):
        deadline = payment_deadline(payment)
        if not deadline or deadline > now:
            return payment
    return None


def supersede(payment: RazorpayPayment) -> bool:
    """Retire a live link that no longer matches what the invoice needs.

    If the gateway still collects on it, the payment settles and is flagged
    for review.
    """
    return _mark(payment, RazorpayPayment.Status.SUPERSEDED)


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
    if (
        reference
        and PaymentReconciliation.objects.filter(
            target_invoice=payment.invoice,
            reference_number=reference,
        ).exists()
    ):
        return
    invoice = payment.invoice
    note = "Payment made via Razorpay's payment link."
    if payment.needs_review:
        note += f" Needs review: {payment.review_reason}"
    PaymentReconciliation.objects.create(
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
        note=note,
        reference_number=reference or "",
    )
    account_id = invoice.account_id
    transaction.on_commit(lambda: rebalance_account_task.delay(account_id))


def paid_at(payment: dict | None):
    created_at = (payment or {}).get("created_at")
    if isinstance(created_at, (int, float)):
        return datetime.fromtimestamp(created_at, UTC)
    if isinstance(created_at, str):
        return parse_datetime(created_at)
    return None


def already_reconciled(invoice, references) -> bool:
    references = [reference for reference in references if reference]
    if not references:
        return False
    return PaymentReconciliation.objects.filter(
        target_invoice=invoice,
        reference_number__in=references,
    ).exists()


def record_invoice_payment(invoice, payment: dict, note: str) -> None:
    """Record one captured Razorpay payment (webhook or API shape) against an invoice."""
    amount = paise_to_rupees(payment.get("amount") or 0)
    PaymentReconciliation.objects.create(
        target_invoice=invoice,
        facility=invoice.facility,
        account=invoice.account,
        reconciliation_type=PaymentReconciliationTypeOptions.payment.value,
        status=PaymentReconciliationStatusOptions.active.value,
        kind=PaymentReconciliationKindOptions.online.value,
        issuer_type=PaymentReconciliationIssuerTypeOptions.patient.value,
        outcome=PaymentReconciliationOutcomeOptions.complete.value,
        method=PaymentReconciliationPaymentMethodOptions.debc.value,
        payment_datetime=paid_at(payment) or care_now(),
        amount=amount,
        tendered_amount=amount,
        returned_amount=0,
        is_credit_note=False,
        authorization="",
        disposition="",
        note=note,
        reference_number=payment.get("id"),
    )
    account_id = invoice.account_id
    transaction.on_commit(lambda: rebalance_account_task.delay(account_id))


QR_CODE_NOTE = "Payment made via Razorpay's QR code."


def _epoch_to_datetime(value) -> datetime | None:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, UTC)
    return None


def create_qr_record(invoice, qr: dict, usage: str) -> RazorpayQRCode:
    amount = qr.get("payment_amount")
    return RazorpayQRCode.objects.create(
        qr_id=qr["id"],
        invoice=invoice,
        amount=paise_to_rupees(amount) if amount else None,
        usage=usage,
        image_url=qr.get("image_url") or "",
        close_by=_epoch_to_datetime(qr.get("close_by")),
        status=(qr.get("status") or RazorpayQRCode.Status.ACTIVE),
        amount_received=paise_to_rupees(qr.get("payments_amount_received") or 0),
        payments_count=qr.get("payments_count_received") or 0,
    )


def qr_record_fields(qr: dict) -> dict:
    """Local fields to update from a gateway QR payload (API or webhook entity)."""
    fields = {}
    if qr.get("payments_amount_received") is not None:
        fields["amount_received"] = paise_to_rupees(qr["payments_amount_received"])
    if qr.get("payments_count_received") is not None:
        fields["payments_count"] = qr["payments_count_received"]
    if qr.get("status") in RazorpayQRCode.Status.values:
        fields["status"] = qr["status"]
    close_by = _epoch_to_datetime(qr.get("close_by"))
    if close_by:
        fields["close_by"] = close_by
    if qr.get("image_url"):
        fields["image_url"] = qr["image_url"]
    return fields


def credit_qr_payments(record: RazorpayQRCode, qr: dict, items) -> int:
    """Record captured QR payments the webhook has not delivered yet; returns how many."""
    recorded = 0
    with RazorpayQRCodeLock(record.qr_id), transaction.atomic():
        record = RazorpayQRCode.objects.select_related(
            "invoice__account", "invoice__facility"
        ).get(pk=record.pk)
        for item in _payment_items(items):
            if not isinstance(item, dict) or item.get("status") != "captured":
                continue
            if not item.get("id") or item.get("amount") is None:
                continue
            if already_reconciled(record.invoice, [item["id"]]):
                continue
            record_invoice_payment(record.invoice, item, QR_CODE_NOTE)
            recorded += 1
        for field, value in qr_record_fields(qr).items():
            setattr(record, field, value)
        record.save()
    return recorded


def fetch_gateway_qr(record: RazorpayQRCode) -> tuple[dict, list] | None:
    """Fetch the QR code and its payments once; None when the gateway did not answer."""
    try:
        qr = razorpay_client.qrcode.fetch(record.qr_id)
        result = razorpay_client.qrcode.fetch_all_payments(record.qr_id)
    except Exception:
        logger.exception("Failed to poll Razorpay QR code %s", record.qr_id)
        return None
    items = result.get("items") if isinstance(result, dict) else result
    return qr, _payment_items(items)


def check_qr_code(record: RazorpayQRCode) -> bool:
    """Fetch the QR code from the gateway once and credit it; returns whether it answered."""
    fetched = fetch_gateway_qr(record)
    if fetched is None:
        return False
    credit_qr_payments(record, *fetched)
    return True


def _fresh(payment: RazorpayPayment) -> RazorpayPayment:
    return RazorpayPayment.objects.select_related(
        "invoice__account", "invoice__facility"
    ).get(pk=payment.pk)


def _mark(payment: RazorpayPayment, status: str, review: str = "") -> bool:
    """Move a still-open payment to a terminal unpaid state."""
    with RazorpayPaymentLock(payment), transaction.atomic():
        payment = _fresh(payment)
        if payment.status not in OPEN_STATUSES:
            return False
        payment.status = status
        if review:
            payment.flag_for_review(review)
        payment.save(
            update_fields=["status", "needs_review", "review_reason", "modified_date"]
        )
    return True


def _prepare_settlement(payment: RazorpayPayment) -> RazorpayPayment | None:
    """Fresh row to settle, or None when it is already fully paid.

    A verified payment means money moved, so any unpaid row settles; settling
    from a non-open state (expired, cancelled, superseded) is flagged for
    review because a replacement link may also have been paid.
    """
    payment = _fresh(payment)
    if payment.status == RazorpayPayment.Status.PAID:
        return None
    if payment.status not in OPEN_STATUSES:
        payment.flag_for_review(f"settled from status {payment.status}")
    return payment


def _finish_settlement(payment: RazorpayPayment, reference, *, final: bool) -> None:
    payment.reference = reference or payment.reference
    payment.status = (
        RazorpayPayment.Status.PAID if final else RazorpayPayment.Status.PARTIALLY_PAID
    )
    payment.save(
        update_fields=[
            "status",
            "reference",
            "needs_review",
            "review_reason",
            "modified_date",
        ]
    )
    if payment.needs_review:
        logger.warning(
            "Razorpay link %s settled but needs review: %s",
            payment.payment_link_id,
            payment.review_reason,
        )


def _settle(payment: RazorpayPayment, reference, amount, paid_at, *, final: bool):
    """Record one confirmed payment against the link, exactly once.

    Runs under the payment lock with a fresh copy of the row so concurrent
    webhook/polling workers cannot both credit the invoice.
    """
    with RazorpayPaymentLock(payment), transaction.atomic():
        payment = _prepare_settlement(payment)
        if payment is None:
            return
        _record_reconciliation(payment, reference, amount, paid_at)
        _finish_settlement(payment, reference, final=final)


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
        _settle(record, reference, amount, paid_at(payment), final=True)
    elif event == "payment_link.partially_paid":
        _settle(record, reference, amount, paid_at(payment), final=False)
    elif event == "payment_link.cancelled":
        _mark(record, RazorpayPayment.Status.CANCELLED)
    elif event == "payment_link.expired":
        _mark(record, RazorpayPayment.Status.EXPIRED)
    return True


def apply_gateway_status(payment: RazorpayPayment, link: dict) -> None:
    expires_at = link_expires_at(link)
    if expires_at and payment.expires_at != expires_at:
        payment.expires_at = expires_at
        payment.save(update_fields=["expires_at", "modified_date"])

    status = (link.get("status") or "").lower()
    if status in ("paid", "partially_paid"):
        _settle_from_link(payment, link, final=status == "paid")
    elif status == "cancelled":
        _mark(payment, RazorpayPayment.Status.CANCELLED)
    elif status == "expired" or (expires_at and expires_at <= care_now()):
        _mark(payment, RazorpayPayment.Status.EXPIRED)
    else:
        logger.info(
            "Razorpay payment link %s still pending, status %s",
            payment.payment_link_id,
            link.get("status"),
        )


def _settle_from_link(payment: RazorpayPayment, link: dict, *, final: bool) -> None:
    with RazorpayPaymentLock(payment), transaction.atomic():
        payment = _prepare_settlement(payment)
        if payment is None:
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
        _finish_settlement(payment, reference, final=final)


def fetch_gateway_link(payment: RazorpayPayment) -> dict | None:
    """Fetch the link from the gateway once; None when it did not answer."""
    try:
        return razorpay_client.payment_link.fetch(payment.payment_link_id)
    except Exception:
        logger.exception(
            "Failed to poll Razorpay status for link %s", payment.payment_link_id
        )
        return None


def check_gateway(payment: RazorpayPayment) -> bool:
    """Fetch the link from the gateway once and apply it; returns whether it answered."""
    link = fetch_gateway_link(payment)
    if link is None:
        return False
    apply_gateway_status(payment, link)
    return True


def _poll(payment: RazorpayPayment, now) -> None:
    answered = check_gateway(payment)
    deadline = polling_deadline(payment)
    if not deadline or deadline > now:
        return
    if answered:
        # the gateway had its final say; a later webhook can still settle the row
        _mark(payment, RazorpayPayment.Status.EXPIRED)
        return
    hard_cap = deadline + timedelta(seconds=settings.RAZORPAY_EXPIRY_HARD_CAP_SECONDS)
    if hard_cap <= now:
        _mark(
            payment,
            RazorpayPayment.Status.EXPIRED,
            review="gateway unreachable at expiry; final status unconfirmed",
        )


def poll_pending_payments() -> None:
    """Poll Razorpay for each open payment; expire those past their deadline.

    A payment is only expired after the gateway answered a query past the
    deadline (plus grace), so an unreachable gateway cannot make us drop a
    payment that actually succeeded. Rows being settled by another worker are
    skipped this cycle.
    """
    now = care_now()
    pending = RazorpayPayment.objects.filter(status__in=OPEN_STATUSES).select_related(
        "invoice__account", "invoice__facility"
    )
    for payment in pending:
        try:
            _poll(payment, now)
        except ObjectLocked:
            logger.info(
                "Razorpay link %s is being settled elsewhere; skipping",
                payment.payment_link_id,
            )
