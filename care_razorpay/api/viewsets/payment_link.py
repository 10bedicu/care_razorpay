from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.models.invoice import Invoice
from care_razorpay import payments
from care_razorpay.api.serializers.payment_link import (
    CreatePaymentLinkRequest,
    PaymentLink,
)
from care_razorpay.utils.razorpay import razorpay_client


class PaymentLinkViewSet(GenericViewSet):
    permission_classes = (IsAuthenticated,)

    def get_exception_handler(self):
        return emr_exception_handler

    @extend_schema(
        description="Retrieve a Razorpay payment link",
        responses={200: PaymentLink},
    )
    def retrieve(self, request, pk):
        try:
            payment_link = razorpay_client.payment_link.fetch(pk)
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            PaymentLink.model_validate(payment_link).model_dump(),
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        description="Create a Razorpay payment link",
        request=CreatePaymentLinkRequest,
        responses={200: PaymentLink},
    )
    def create(self, request):
        data = CreatePaymentLinkRequest.model_validate(request.data)

        invoice = Invoice.objects.get(external_id=data.invoice_id)

        try:
            _payment, payment_link = payments.create_payment(
                invoice,
                accept_partial=data.is_partial_payment_allowed,
                minimum_down_payment=data.minimum_down_payment,
                email=data.email,
                phone_number=data.phone_number,
                notify_sms=data.phone_number is not None,
                notify_email=data.email is not None,
                reminder_enable=True,
                expires_at=data.expires_at,
            )
        except payments.RazorpayError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            PaymentLink.model_validate(payment_link).model_dump(),
            status=status.HTTP_201_CREATED,
        )
