from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.models.invoice import Invoice
from care_razorpay import payments
from care_razorpay.api.collection import (
    READ_PERMISSION,
    WRITE_PERMISSION,
    authorize,
    collectible_invoice,
)
from care_razorpay.api.serializers.qr_code import (
    CreateQRCodeRequest,
    QRCode,
    QRCodeRecord,
)
from care_razorpay.models import RazorpayQRCode
from care_razorpay.utils.razorpay import razorpay_client


def _serialize_record(record: RazorpayQRCode) -> dict:
    return QRCodeRecord.from_record(record).model_dump(mode="json")


class QRCodeViewSet(GenericViewSet):
    permission_classes = (IsAuthenticated,)

    def get_exception_handler(self):
        return emr_exception_handler

    def _record(self, qr_id, permission=WRITE_PERMISSION) -> RazorpayQRCode:
        record = (
            RazorpayQRCode.objects.select_related(
                "invoice__account", "invoice__facility"
            )
            .filter(qr_id=qr_id)
            .first()
        )
        if not record:
            raise NotFound("QR code not found")
        authorize(self.request.user, record.invoice.facility, permission)
        return record

    @extend_schema(
        description="List the Razorpay QR codes CARE created for an invoice, newest first.",
    )
    def list(self, request):
        invoice_id = request.query_params.get("invoice")
        if not invoice_id:
            raise ValidationError("invoice is required")
        invoice = (
            Invoice.objects.select_related("facility")
            .filter(external_id=invoice_id)
            .first()
        )
        if not invoice:
            raise NotFound("Invoice not found")
        authorize(request.user, invoice.facility, READ_PERMISSION)
        records = (
            RazorpayQRCode.objects.filter(invoice=invoice)
            .select_related("invoice")
            .order_by("-created_date")
        )
        statuses = request.query_params.get("status")
        if statuses:
            records = records.filter(status__in=statuses.split(","))
        results = [_serialize_record(record) for record in records]
        return Response({"count": len(results), "results": results})

    @extend_schema(
        description="Retrieve a Razorpay QR code created through CARE",
        responses={200: QRCode},
    )
    def retrieve(self, request, pk):
        self._record(pk, READ_PERMISSION)
        try:
            qr_code = razorpay_client.qrcode.fetch(pk)
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            QRCode.model_validate(qr_code).model_dump(),
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description=(
            "Ask Razorpay for the QR code's payments and record any that the "
            "webhook has not delivered. `gateway_checked` tells whether Razorpay "
            "answered; `gateway` is its payload when it did."
        ),
        request=None,
        responses={200: QRCodeRecord},
    )
    @action(detail=True, methods=["POST"])
    def refresh(self, request, pk=None):
        record = self._record(pk)
        fetched = payments.fetch_gateway_qr(record)
        gateway = None
        if fetched is not None:
            payments.credit_qr_payments(record, *fetched)
            gateway = QRCode.model_validate(fetched[0]).model_dump()
            record.refresh_from_db()
        return Response(
            {
                "qr_code": _serialize_record(record),
                "gateway": gateway,
                "gateway_checked": fetched is not None,
            },
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Create a Razorpay QR code",
        request=CreateQRCodeRequest,
        responses={200: QRCode},
    )
    def create(self, request):
        data = CreateQRCodeRequest.model_validate(request.data)
        invoice = collectible_invoice(request.user, data.invoice_id)

        if not hasattr(invoice.facility, "razorpayaccount"):
            return Response(
                {"detail": "Razorpay account not found for facility"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        outstanding = payments.invoice_outstanding(invoice)
        if outstanding <= 0:
            raise ValidationError("Invoice has no outstanding balance")

        # TODO: handle account transfer for QR code

        try:
            qr_code = razorpay_client.qrcode.create(
                {
                    "type": "upi_qr",
                    "name": invoice.title or f"Invoice {invoice.external_id}",
                    "usage": data.usage.value,
                    "fixed_amount": data.is_amount_fixed,
                    "payment_amount": payments.amount_paise(outstanding)
                    if data.is_amount_fixed
                    else None,
                    "description": invoice.title,
                    "close_by": int((data.closes_at).timestamp())
                    if data.closes_at
                    else None,
                    "notes": {
                        "invoice_id": str(invoice.external_id),
                        "account_id": str(invoice.account.external_id),
                        "patient_id": str(invoice.patient.external_id),
                        "facility_id": str(invoice.facility.external_id),
                    },
                }
            )
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payments.create_qr_record(invoice, qr_code, data.usage.value)
        return Response(
            QRCode.model_validate(qr_code).model_dump(),
            status=status.HTTP_201_CREATED,
        )
