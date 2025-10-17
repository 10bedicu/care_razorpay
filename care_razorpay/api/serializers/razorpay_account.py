from rest_framework import serializers

from care.facility.models import Facility
from care_razorpay.models.razorpay_account import RazorpayAccount


class RazorpayAccountSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="external_id", read_only=True)
    facility_id = serializers.UUIDField()
    account_id = serializers.CharField(max_length=255)
    is_enabled = serializers.BooleanField(default=True)
    metadata = serializers.JSONField(read_only=True)
    created_date = serializers.DateTimeField(read_only=True)
    modified_date = serializers.DateTimeField(read_only=True)

    class Meta:
        model = RazorpayAccount
        exclude = ("deleted", "facility")

    def to_representation(self, instance):
        data = super().to_representation(instance)

        if instance.facility:
            data["facility_id"] = instance.facility.external_id
        return data

    def validate_facility_id(self, value):
        """Validate that the facility exists and is accessible."""
        try:
            Facility.objects.get(external_id=value)
            return value
        except Facility.DoesNotExist as e:
            error_msg = f"Facility with external_id {value} does not exist."
            raise serializers.ValidationError(error_msg) from e

    def create(self, validated_data):
        facility_id = validated_data.pop("facility_id")
        facility = Facility.objects.get(external_id=facility_id)
        validated_data["facility"] = facility
        return super().create(validated_data)
