from decimal import Decimal
from unittest.mock import patch

from model_bakery import baker

from care.emr.models.invoice import Invoice
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from care.utils.tests.base import CareAPITestBase

from care_razorpay.scan_pay import RazorpayProvider


class TestRazorpayScanPayProvider(CareAPITestBase):
    def setUp(self):
        self.user = self.create_super_user()
        self.facility = self.create_facility(self.user)
        self.patient = self.create_patient(phone_number="+919999999999")
        self.account = baker.make(
            "emr.Account", facility=self.facility, patient=self.patient
        )

    def make_invoice(self):
        return baker.make(
            Invoice,
            facility=self.facility,
            patient=self.patient,
            account=self.account,
            status=InvoiceStatusOptions.issued.value,
            total_gross=Decimal(100),
            number="INV-RZP",
        )

    @patch("care_razorpay.scan_pay.razorpay_client")
    def test_create_payment_link(self, mock_client):
        mock_client.payment_link.create.return_value = {
            "id": "plink_test",
            "short_url": "https://rzp.io/test",
        }

        result = RazorpayProvider().create_payment_link(self.make_invoice())

        self.assertEqual(result["order_number"], "INV-RZP")
        self.assertEqual(result["payment_link_id"], "plink_test")
        self.assertEqual(result["payment_url"], "https://rzp.io/test")
        self.assertEqual(
            mock_client.payment_link.create.call_args.args[0]["amount"], 10000
        )
