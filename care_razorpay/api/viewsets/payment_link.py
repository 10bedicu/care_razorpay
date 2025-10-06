from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.models.invoice import Invoice
from care_razorpay.care_razorpay.api.serializers.payment_link import (
    CreatePaymentLinkRequest,
    CreatePaymentLinkResponse,
)
from care_razorpay.utils.razorpay import razorpay_client


class PaymentLinkViewSet(GenericViewSet):
    permission_classes = (IsAuthenticated,)

    def get_exception_handler(self):
        return emr_exception_handler

    @extend_schema(
        description="Create a Razorpay payment link",
        request=CreatePaymentLinkRequest,
        responses={200: CreatePaymentLinkResponse},
    )
    def create(self, request):
        data = CreatePaymentLinkRequest.model_validate(request.data)

        invoice = Invoice.objects.get(external_id=data.invoice_id)

        try:
            payment_link = razorpay_client.payment_link.create(
                {
                    "amount": float(invoice.total_gross) * 100,
                    "currency": "INR",
                    "accept_partial": data.is_partial_payment_allowed,
                    "first_min_partial_amount": data.minimum_down_payment,
                    "description": invoice.title,
                    "customer": {
                        "name": invoice.patient.name,
                        "email": data.email,
                        "contact": data.phone_number,
                    },
                    "notify": {
                        "sms": data.phone_number is not None,
                        "email": data.email is not None,
                    },
                    "reminder_enable": True,
                    "reference_id": str(invoice.external_id),
                }
            )
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            CreatePaymentLinkResponse.model_validate(payment_link).model_dump(),
            status=status.HTTP_201_CREATED,
        )
