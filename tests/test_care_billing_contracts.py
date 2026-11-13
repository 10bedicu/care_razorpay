from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

from care_razorpay.locks import RazorpayPaymentLock
from care_razorpay.models import RazorpayPayment
from model_bakery import baker

from care.emr.locks.billing import InvoiceLock
from care.emr.models.charge_item import ChargeItem
from care.emr.resources.charge_item.spec import ChargeItemStatusOptions
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from care.security.permissions.payment_reconciliation import (
    PaymentReconciliationPermissions,
)
from care.utils.lock import ObjectLocked
from care.utils.rounding.rounding import care_round
from care_razorpay import payments
from tests.test_scan_pay import PAYMENT_LINK_URL, RazorpayTestBase

ACCOUNT_URL = "/api/care_razorpay/razorpay_account/"


class CareBillingContractTests(RazorpayTestBase):
    def setUp(self):
        super().setUp()
        self.staff = self.create_user()
        role = self.create_role_with_permissions(
            [
                PaymentReconciliationPermissions.can_write_payment_reconciliation.name,
                PaymentReconciliationPermissions.can_read_payment_reconciliation.name,
            ]
        )
        self.organization = self.create_facility_organization(self.facility)
        self.attach_role_facility_organization_user(
            self.organization, self.staff, role
        )
        self.client.force_authenticate(user=self.staff)

    def link_payload(self, link_id, amount):
        return {
            "id": link_id,
            "short_url": f"https://rzp.io/{link_id}",
            "created_at": 1_700_000_000,
            "amount": amount,
            "amount_paid": 0,
            "status": "created",
            "expire_by": int(datetime.now(UTC).timestamp()) + 3600,
        }

    @patch("care_razorpay.payments.razorpay_client")
    def test_payment_link_uses_the_invoice_lock_and_outstanding_balance(
        self, mock_client
    ):
        invoice = self.make_invoice("INV-CONTRACT")
        self.record_payment(invoice, Decimal("30"))
        outstanding = care_round(invoice.total_gross - Decimal("30"))
        mock_client.payment_link.create.return_value = self.link_payload(
            "plink_contract", payments.amount_paise(outstanding)
        )

        response = self.client.post(
            PAYMENT_LINK_URL,
            {"invoice_id": str(invoice.external_id)},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        payload = mock_client.payment_link.create.call_args.args[0]
        self.assertEqual(payload["amount"], payments.amount_paise(outstanding))
        payment = RazorpayPayment.objects.get(payment_link_id="plink_contract")
        self.assertEqual(payment.amount, outstanding)

        draft = self.make_invoice("INV-DRAFT")
        draft.status = InvoiceStatusOptions.draft.value
        draft.save(update_fields=["status"])
        rejected = self.client.post(
            PAYMENT_LINK_URL,
            {"invoice_id": str(draft.external_id)},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400)

    def test_locked_invoice_is_a_client_error(self):
        invoice = self.make_invoice("INV-LOCKED")
        with InvoiceLock(invoice):
            response = self.client.post(
                PAYMENT_LINK_URL,
                {"invoice_id": str(invoice.external_id)},
                format="json",
            )
        self.assertEqual(response.status_code, 423)

    def test_settlement_rebalances_the_account_under_the_payment_lock(self):
        payment = self.make_payment("plink_settle", "INV-SETTLE")
        baker.make(
            ChargeItem,
            facility=self.facility,
            patient=self.patient,
            account=self.account,
            status=ChargeItemStatusOptions.billable.value,
            title="Still open",
            quantity=1,
            total_price=Decimal(25),
        )

        with self.captureOnCommitCallbacks(execute=True):
            payments._settle(
                payment,
                "pay_settle",
                payment.amount,
                datetime.now(UTC),
                final=True,
            )

        payment.refresh_from_db()
        self.account.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
        self.assertEqual(self.account.total_gross, payment.invoice.total_gross)
        self.assertEqual(self.account.total_paid, payment.amount)
        self.assertEqual(self.account.total_billable_charge_items, Decimal(25))

        with RazorpayPaymentLock(payment), self.assertRaises(ObjectLocked):
            payments._settle(
                payment, "pay_again", payment.amount, datetime.now(UTC), final=True
            )

    def test_collection_permission_and_account_membership(self):
        self.client.force_authenticate(user=self.create_user())
        denied = self.client.post(
            PAYMENT_LINK_URL,
            {"invoice_id": str(self.make_invoice("INV-403").external_id)},
            format="json",
        )
        self.assertEqual(denied.status_code, 403)

        self.client.force_authenticate(user=self.staff)
        opened = self.client.get(f"{ACCOUNT_URL}{self.facility.external_id}/")
        self.assertEqual(opened.status_code, 200, getattr(opened, "data", opened.content))

        stranger = self.create_user()
        self.client.force_authenticate(user=stranger)
        hidden = self.client.get(f"{ACCOUNT_URL}{self.facility.external_id}/")
        self.assertEqual(hidden.status_code, 404)

    def test_invalid_phone_is_rejected_by_the_mobile_validator(self):
        response = self.client.post(
            PAYMENT_LINK_URL,
            {
                "invoice_id": str(self.make_invoice("INV-PHONE").external_id),
                "phone_number": "not-a-phone",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("errors", response.data)
