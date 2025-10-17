from datetime import UTC, datetime

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.models.invoice import Invoice
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
from care_razorpay.api.authentication import RazorpayWebhookAuthentication


class WebhookViewSet(GenericViewSet):
    authentication_classes = (RazorpayWebhookAuthentication,)
    permission_classes = (AllowAny,)

    @extend_schema(
        description="Handle a Razorpay payment link events",
    )
    @action(detail=False, methods=["POST"], url_path="payment_link")
    def payment_link(self, request):
        data = request.data

        if (
            data.get("event") == "payment_link.paid"
            or data.get("event") == "payment_link.partially_paid"
        ):
            payment = data.get("payload", {}).get("payment", {}).get("entity")
            payment_link = data.get("payload", {}).get("payment_link", {}).get("entity")

            if not payment or not payment_link:
                return Response(
                    {"detail": "Payment or payment link not found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            notes = payment_link.get("notes", {})
            invoice_id = notes.get("invoice_id")
            invoice = Invoice.objects.filter(external_id=invoice_id).first()

            if not invoice:
                return Response(
                    {"detail": "Invoice not found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

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
                payment_datetime=datetime.fromtimestamp(payment.get("created_at"), UTC),
                amount=payment.get("amount") / 100,
                tendered_amount=payment.get("amount") / 100,
                returned_amount=0,
                is_credit_note=False,
                authorization="",
                disposition="",
                note="Payment made via Razorpay's payment link.",
                reference_number=payment.get("id"),
            )
            rebalance_account_task.delay(payment_reconciliation.account.id)

        return Response(status=status.HTTP_200_OK)

    @extend_schema(
        description="Handle a Razorpay QR code events",
    )
    @action(detail=False, methods=["POST"], url_path="qr_code")
    def qr_code(self, request):
        data = request.data

        if data.get("event") == "qr_code.credited":
            payment = data.get("payload", {}).get("payment", {}).get("entity")
            qr_code = data.get("payload", {}).get("qr_code", {}).get("entity")

            if not payment or not qr_code:
                return Response(
                    {"detail": "Payment or QR code not found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            notes = qr_code.get("notes", {})
            invoice_id = notes.get("invoice_id")
            invoice = Invoice.objects.filter(external_id=invoice_id).first()

            if not invoice:
                return Response(
                    {"detail": "Invoice not found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

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
                payment_datetime=datetime.fromtimestamp(payment.get("created_at"), UTC),
                amount=payment.get("amount") / 100,
                tendered_amount=payment.get("amount") / 100,
                returned_amount=0,
                is_credit_note=False,
                authorization="",
                disposition="",
                note="Payment made via Razorpay's QR code.",
                reference_number=payment.get("id"),
            )
            rebalance_account_task.delay(payment_reconciliation.account.id)

        return Response(status=status.HTTP_200_OK)
