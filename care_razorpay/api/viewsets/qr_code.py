from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.models.invoice import Invoice
from care_razorpay.care_razorpay.api.serializers.qr_code import (
    CreateQRCodeRequest,
    QRCode,
)
from care_razorpay.utils.razorpay import razorpay_client


class QRCodeViewSet(GenericViewSet):
    permission_classes = (IsAuthenticated,)

    def get_exception_handler(self):
        return emr_exception_handler

    @extend_schema(
        description="Retrieve a Razorpay QR code",
        responses={200: QRCode},
    )
    def retrieve(self, request, pk):
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
        description="Create a Razorpay QR code",
        request=CreateQRCodeRequest,
        responses={200: QRCode},
    )
    def create(self, request):
        data = CreateQRCodeRequest.model_validate(request.data)

        invoice = Invoice.objects.get(external_id=data.invoice_id)

        try:
            qr_code = razorpay_client.qrcode.create(
                {
                    "type": "upi_qr",
                    "name": invoice.title or f"Invoice {invoice.external_id}",
                    "usage": data.usage.value,
                    "fixed_amount": data.is_amount_fixed,
                    "payment_amount": float(invoice.total_gross) * 100
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

        return Response(
            QRCode.model_validate(qr_code).model_dump(),
            status=status.HTTP_201_CREATED,
        )
