import hashlib
import logging

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from care.emr.models.invoice import Invoice
from care_razorpay import payments
from care_razorpay.locks import RazorpayQRCodeLock
from care_razorpay.models import RazorpayQRCode, RazorpayWebhookEvent
from care_razorpay.settings import plugin_settings as settings

logger = logging.getLogger(__name__)

PAID_EVENTS = {"payment_link.paid", "payment_link.partially_paid"}
EVENT_ID_HEADER = "X-Razorpay-Event-Id"


def _reconcile_via_abdm(payment_link: dict, payment: dict | None, event: str) -> bool:
    # Optional: only wired when the ABDM plug is installed.
    try:
        from care_razorpay.provider import reconcile_abdm_event
    except ImportError:
        logger.info("care_abdm not installed; ignoring unknown Razorpay order")
        return False
    return reconcile_abdm_event(payment_link, payment, event)


def _reconcile_from_invoice_notes(payment: dict, payment_link: dict) -> Response:
    notes = payment_link.get("notes", {})
    invoice_id = notes.get("invoice_id")
    invoice = Invoice.objects.filter(external_id=invoice_id).first()

    if not invoice:
        return Response(
            {"detail": "Invoice not found"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if payments.already_reconciled(
        invoice, [payment.get("id"), payment_link.get("id")]
    ):
        return Response(status=status.HTTP_200_OK)

    payments.record_invoice_payment(
        invoice, payment, "Payment made via Razorpay's payment link."
    )
    return Response(status=status.HTTP_200_OK)


def dispatch_payment_link_event(data: dict) -> Response:
    event = data.get("event")
    payload = data.get("payload") or {}
    payment = (payload.get("payment") or {}).get("entity")
    payment_link = (payload.get("payment_link") or {}).get("entity")

    if event in PAID_EVENTS and (not payment or not payment_link):
        return Response(
            {"detail": "Payment or payment link not found"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if payment_link and payments.reconcile_event(payment_link, payment, event):
        return Response(status=status.HTTP_200_OK)

    if payment_link and _reconcile_via_abdm(payment_link, payment, event):
        return Response(status=status.HTTP_200_OK)

    if event in PAID_EVENTS:
        return _reconcile_from_invoice_notes(payment, payment_link)

    return Response(status=status.HTTP_200_OK)


def dispatch_qr_code_event(data: dict) -> Response:
    if data.get("event") != "qr_code.credited":
        return Response(status=status.HTTP_200_OK)

    payload = data.get("payload") or {}
    payment = (payload.get("payment") or {}).get("entity")
    qr_code = (payload.get("qr_code") or {}).get("entity")
    if not payment or not qr_code:
        return Response(
            {"detail": "Payment or QR code not found"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    invoice_id = (qr_code.get("notes") or {}).get("invoice_id")
    invoice = Invoice.objects.filter(external_id=invoice_id).first()
    if not invoice:
        return Response(
            {"detail": "Invoice not found"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with RazorpayQRCodeLock(qr_code.get("id") or "unknown"), transaction.atomic():
        if payments.already_reconciled(invoice, [payment.get("id")]):
            return Response(status=status.HTTP_200_OK)
        payments.record_invoice_payment(invoice, payment, payments.QR_CODE_NOTE)
        record = RazorpayQRCode.objects.filter(qr_id=qr_code.get("id")).first()
        if record:
            for field, value in payments.qr_record_fields(qr_code).items():
                setattr(record, field, value)
            record.save()
    return Response(status=status.HTTP_200_OK)


DISPATCHERS = {
    RazorpayWebhookEvent.Kind.PAYMENT_LINK: dispatch_payment_link_event,
    RazorpayWebhookEvent.Kind.QR_CODE: dispatch_qr_code_event,
}


def store_event(request, kind: str) -> tuple[RazorpayWebhookEvent, bool]:
    """Persist a signature-verified webhook. Returns ``(event, created)``.

    Razorpay re-deliveries carry the same event id, so they map to one row.
    """
    event_id = (
        request.headers.get(EVENT_ID_HEADER) or hashlib.sha256(request.body).hexdigest()
    )
    data = request.data if isinstance(request.data, dict) else {}
    return RazorpayWebhookEvent.objects.get_or_create(
        event_id=event_id,
        defaults={"kind": kind, "event": data.get("event") or "", "payload": data},
    )


def process_event(event: RazorpayWebhookEvent) -> Response:
    """Apply a stored webhook. Non-2xx dispatch outcomes are final (ignored);
    exceptions mark the event failed for replay and are re-raised."""
    try:
        with transaction.atomic():
            response = DISPATCHERS[event.kind](event.payload)
    except Exception as exc:
        event.mark_failed(f"{type(exc).__name__}: {exc}")
        raise
    if status.is_success(response.status_code):
        event.mark_processed()
    else:
        event.mark_processed(
            RazorpayWebhookEvent.Status.IGNORED, detail=str(response.data)
        )
    return response


def handle(request, kind: str) -> Response:
    """Store the webhook, then process it inline.

    500 means "stored but not applied"; the replay task (or Razorpay's own
    retry) will pick it up. A re-delivered, already handled event is a no-op.
    """
    event, created = store_event(request, kind)
    if not created and event.status in (
        RazorpayWebhookEvent.Status.PROCESSED,
        RazorpayWebhookEvent.Status.IGNORED,
    ):
        return Response(status=status.HTTP_200_OK)
    try:
        return process_event(event)
    except Exception:
        logger.exception("Failed to process Razorpay webhook %s", event.event_id)
        return Response(
            {"detail": "Webhook stored but could not be processed"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def replay_failed(limit: int = 100) -> int:
    """Retry webhooks whose processing failed; returns how many now succeeded."""
    events = RazorpayWebhookEvent.objects.filter(
        status=RazorpayWebhookEvent.Status.FAILED,
        attempts__lt=settings.RAZORPAY_WEBHOOK_MAX_ATTEMPTS,
    ).order_by("created_date")[:limit]
    replayed = 0
    for event in events:
        try:
            process_event(event)
        except Exception:
            logger.exception("Replay of Razorpay webhook %s failed", event.event_id)
            continue
        replayed += 1
    return replayed
