from django.db.models import Q
from rest_framework import serializers, status
from rest_framework.decorators import action
from rest_framework.mixins import CreateModelMixin, RetrieveModelMixin, UpdateModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.models.organization import FacilityOrganizationUser, OrganizationUser
from care.facility.models import Facility
from care_razorpay.api.permissions import IsSuperUserOrReadOnly
from care_razorpay.api.serializers.razorpay_account import RazorpayAccountSerializer
from care_razorpay.models.razorpay_account import RazorpayAccount
from care_razorpay.utils.razorpay import razorpay_client


class RazorpayAccountViewSet(
    GenericViewSet,
    CreateModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
):
    permission_classes = (IsSuperUserOrReadOnly,)
    queryset = RazorpayAccount.objects.all()
    serializer_class = RazorpayAccountSerializer
    lookup_field = "facility__external_id"

    def get_facility_queryset(self):
        qs = Facility.objects.all()
        if self.request.user.is_superuser:
            return qs

        organization_ids = list(
            OrganizationUser.objects.filter(user=self.request.user).values_list(
                "organization_id", flat=True
            )
        )
        return qs.filter(
            Q(
                id__in=FacilityOrganizationUser.objects.filter(
                    user=self.request.user
                ).values_list("organization__facility_id")
            )
            | Q(geo_organization_cache__overlap=organization_ids)
        )

    def get_queryset(self):
        queryset = self.queryset
        facilities = self.get_facility_queryset()
        return queryset.filter(facility__in=facilities)

    def sync_razorpay_account(
        self, razorpay_account: RazorpayAccount
    ) -> RazorpayAccount:
        try:
            razorpay_account_details = razorpay_client.account.fetch(
                razorpay_account.account_id
            )
        except Exception as e:
            raise serializers.ValidationError({"detail": str(e)}) from e

        if not razorpay_account_details:
            raise serializers.ValidationError(
                {"detail": "Razorpay account details not found on Razorpay"}
            )

        razorpay_account.metadata = razorpay_account_details
        razorpay_account.save()

        return razorpay_account

    def perform_create(self, serializer):
        razorpay_account = serializer.save()
        self.sync_razorpay_account(razorpay_account)
        return razorpay_account

    def perform_update(self, serializer):
        razorpay_account = serializer.save()
        self.sync_razorpay_account(razorpay_account)
        return razorpay_account

    @action(detail=True, methods=["GET"])
    def details(self, request, facility__external_id):
        razorpay_account = self.get_object()

        self.sync_razorpay_account(razorpay_account)

        return Response(
            RazorpayAccountSerializer(razorpay_account).data, status=status.HTTP_200_OK
        )
