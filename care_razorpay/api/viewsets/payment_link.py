from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.locks.billing import InvoiceLock
from care_razorpay import payments
from care_razorpay.api.collection import READ_PERMISSION, authorize, collectible_invoice
from care_razorpay.api.serializers.payment_link import (
    CreatePaymentLinkRequest,
    PaymentLink,
)
from care_razorpay.models import RazorpayPayment
from care_razorpay.utils.razorpay import razorpay_client


def _serialize(payment_link: dict) -> dict:
    return PaymentLink.model_validate(payment_link).model_dump()


class PaymentLinkViewSet(GenericViewSet):
    permission_classes = (IsAuthenticated,)

    def get_exception_handler(self):
        return emr_exception_handler

    @extend_schema(
        description="Retrieve a Razorpay payment link created through CARE",
        responses={200: PaymentLink},
    )
    def retrieve(self, request, pk):
        payment = (
            RazorpayPayment.objects.select_related("invoice__facility")
            .filter(payment_link_id=pk)
            .first()
        )
        if not payment:
            raise NotFound("Payment link not found")
        authorize(request.user, payment.invoice.facility, READ_PERMISSION)

        try:
            payment_link = razorpay_client.payment_link.fetch(pk)
        except Exception as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(_serialize(payment_link), status=status.HTTP_200_OK)

    @extend_schema(
        description=(
            "Create a Razorpay payment link for an issued invoice's outstanding "
            "balance. Returns the existing live link (200) when one already "
            "covers that balance."
        ),
        request=CreatePaymentLinkRequest,
        responses={200: PaymentLink, 201: PaymentLink},
    )
    def create(self, request):
        data = CreatePaymentLinkRequest.model_validate(request.data)
        invoice = collectible_invoice(request.user, data.invoice_id)

        with InvoiceLock(invoice):
            outstanding = payments.invoice_outstanding(invoice)
            if outstanding <= 0:
                raise ValidationError("Invoice has no outstanding balance")
            existing = payments.pending_payment(invoice)
            if existing and existing.amount == outstanding:
                try:
                    payment_link = razorpay_client.payment_link.fetch(
                        existing.payment_link_id
                    )
                except Exception as e:
                    return Response(
                        {"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST
                    )
                return Response(_serialize(payment_link), status=status.HTTP_200_OK)

            try:
                _payment, payment_link = payments.create_payment(
                    invoice,
                    outstanding,
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
            # retire the stale link only once its replacement exists
            if existing:
                payments.supersede(existing)

        return Response(_serialize(payment_link), status=status.HTTP_201_CREATED)
