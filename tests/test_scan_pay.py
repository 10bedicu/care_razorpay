from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from abdm.models import AbhaNumber, HealthFacility, PaymentOrder
from abdm.models.payment_order import PaymentOrderStatus
from abdm.service.helper import uuid
from abdm.utils import user as abdm_user
from model_bakery import baker
from rest_framework import status

from care.emr.models.invoice import Invoice
from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from care.utils.tests.base import CareAPITestBase
from care.utils.time_util import care_now
from care_razorpay import payments
from care_razorpay import provider as razorpay_provider
from care_razorpay.api.viewsets.webhook import dispatch_payment_link_event
from care_razorpay.models import RazorpayAccount, RazorpayPayment
from care_razorpay.settings import plugin_settings as settings


def _paid_event(link_id, payment_id="pay_test", amount=10000, invoice_id=None):
    notes = {"invoice_id": str(invoice_id)} if invoice_id else {}
    return {
        "event": "payment_link.paid",
        "payload": {
            "payment": {
                "entity": {"id": payment_id, "amount": amount, "created_at": 1_700_000_000}
            },
            "payment_link": {"entity": {"id": link_id, "notes": notes}},
        },
    }


class RazorpayTestBase(CareAPITestBase):
    def setUp(self):
        abdm_user.ABDM_USER = None
        self.user = self.create_super_user()
        self.facility = self.create_facility(self.user)
        self.health_facility = baker.make(
            HealthFacility, facility=self.facility, hf_id="TEST_HIP"
        )
        self.patient = self.create_patient(phone_number="+919999999999")
        self.abha_number = baker.make(
            AbhaNumber,
            patient=self.patient,
            health_id="testpatient@sbx",
            abha_number="91123456789012",
        )
        self.account = baker.make(
            "emr.Account", facility=self.facility, patient=self.patient
        )
        self.razorpay_account = baker.make(
            RazorpayAccount, facility=self.facility, account_id="acc_test"
        )

    def make_invoice(self, number="INV-RZP"):
        return baker.make(
            Invoice,
            facility=self.facility,
            patient=self.patient,
            account=self.account,
            status=InvoiceStatusOptions.issued.value,
            total_gross=Decimal(100),
            number=number,
            title="Consultation",
        )

    def make_order(self, payment_link_id="plink_abdm"):
        return PaymentOrder.objects.create(
            open_order_request_id=uuid(),
            abha_number=self.abha_number,
            health_facility=self.health_facility,
            invoice=self.make_invoice("INV-RZP-REC"),
            order_number="INV-RZP-REC",
            payment_link_id=payment_link_id,
            provider="razorpay",
            status=PaymentOrderStatus.PAYMENT_INITIATED,
        )


class TestRazorpayProvider(RazorpayTestBase):
    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_link_uses_facility_route(self, mock_client):
        mock_client.payment_link.create.return_value = {
            "id": "plink_test",
            "short_url": "https://rzp.io/test",
        }

        result = razorpay_provider.RazorpayProvider().create_payment_link(
            self.make_invoice()
        )

        self.assertEqual(result["order_number"], "INV-RZP")
        self.assertEqual(result["payment_link_id"], "plink_test")
        self.assertEqual(result["payment_url"], "https://rzp.io/test")
        payload = mock_client.payment_link.create.call_args.args[0]
        self.assertEqual(payload["amount"], 10000)
        transfer = payload["options"]["order"]["transfers"][0]
        self.assertEqual(transfer["account"], "acc_test")
        self.assertEqual(transfer["amount"], 10000)
        self.assertFalse(
            RazorpayPayment.objects.filter(payment_link_id="plink_test").exists()
        )

    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_link_requires_facility_account(self, mock_client):
        invoice = self.make_invoice()
        invoice.facility = self.create_facility(self.user)

        with self.assertRaises(payments.RazorpayError):
            razorpay_provider.RazorpayProvider().create_payment_link(invoice)

        mock_client.payment_link.create.assert_not_called()

    @patch("abdm.signals.scan_pay.scan_pay_notify.delay")
    @patch("care_razorpay.payments.razorpay_client")
    def test_reconcile_marks_paid_and_notifies(self, mock_client, mock_task):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_test",
            "status": "paid",
            "order_id": "order_test",
        }
        mock_client.order.payments.return_value = {
            "items": [{"id": "pay_test", "amount": 10000, "status": "captured"}]
        }
        order = self.make_order("plink_test")

        razorpay_provider.RazorpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.SUCCESS)
        self.assertEqual(order.transaction_id, "pay_test")
        self.assertTrue(
            PaymentReconciliation.objects.filter(target_invoice=order.invoice).exists()
        )
        mock_task.assert_called_once()

    @patch("abdm.signals.scan_pay.scan_pay_notify.delay")
    @patch("care_razorpay.payments.razorpay_client")
    def test_reconcile_ignores_unpaid(self, mock_client, mock_task):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_test",
            "status": "created",
        }
        order = self.make_order("plink_test")

        razorpay_provider.RazorpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.PAYMENT_INITIATED)
        self.assertFalse(
            PaymentReconciliation.objects.filter(target_invoice=order.invoice).exists()
        )
        mock_task.assert_not_called()


class TestRazorpayStandalonePayment(RazorpayTestBase):
    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_persists_record(self, mock_client):
        mock_client.payment_link.create.return_value = {
            "id": "plink_standalone",
            "short_url": "https://rzp.io/standalone",
        }

        payment, link = payments.create_payment(self.make_invoice())

        self.assertEqual(payment.status, RazorpayPayment.Status.CREATED)
        self.assertEqual(payment.payment_link_id, "plink_standalone")
        self.assertEqual(payment.payment_url, "https://rzp.io/standalone")
        self.assertEqual(link["id"], "plink_standalone")
        transfer = mock_client.payment_link.create.call_args.args[0]["options"]["order"][
            "transfers"
        ][0]
        self.assertEqual(transfer["account"], "acc_test")

    @patch("care_razorpay.payments.rebalance_account_task.delay")
    def test_event_reconciles_standalone_payment(self, mock_rebalance):
        invoice = self.make_invoice("INV-STANDALONE")
        payment = RazorpayPayment.objects.create(
            payment_link_id="plink_standalone",
            invoice=invoice,
            amount=invoice.total_gross,
        )

        handled = payments.reconcile_event(
            {"id": "plink_standalone"},
            {"id": "pay_555", "amount": 10000, "created_at": 1_700_000_000},
            "payment_link.paid",
        )

        self.assertTrue(handled)
        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
        self.assertEqual(payment.reference, "pay_555")
        reconciliation = PaymentReconciliation.objects.get(target_invoice=invoice)
        self.assertEqual(reconciliation.amount, Decimal(100))
        mock_rebalance.assert_called_once()

    @patch("care_razorpay.payments.rebalance_account_task.delay")
    def test_partial_event_keeps_payment_open(self, mock_rebalance):
        invoice = self.make_invoice("INV-PARTIAL")
        payment = RazorpayPayment.objects.create(
            payment_link_id="plink_partial",
            invoice=invoice,
            amount=invoice.total_gross,
        )

        payments.reconcile_event(
            {"id": "plink_partial"},
            {"id": "pay_partial", "amount": 4000, "created_at": 1_700_000_000},
            "payment_link.partially_paid",
        )

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PARTIALLY_PAID)
        reconciliation = PaymentReconciliation.objects.get(target_invoice=invoice)
        self.assertEqual(reconciliation.amount, Decimal(40))
        mock_rebalance.assert_called_once()

    def test_event_returns_false_for_unknown_link(self):
        self.assertFalse(
            payments.reconcile_event(
                {"id": "plink_unknown"},
                {"id": "pay_unknown", "amount": 10000},
                "payment_link.paid",
            )
        )

    @patch("care_razorpay.payments.rebalance_account_task.delay")
    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_reconciles_paid_pending_payment(self, mock_client, mock_rebalance):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_poll",
            "status": "paid",
            "order_id": "order_poll",
        }
        mock_client.order.payments.return_value = {
            "items": [{"id": "pay_poll", "amount": 10000, "status": "captured"}]
        }
        invoice = self.make_invoice("INV-POLL")
        payment = RazorpayPayment.objects.create(
            payment_link_id="plink_poll",
            invoice=invoice,
            amount=invoice.total_gross,
        )

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
        self.assertEqual(payment.reference, "pay_poll")
        self.assertTrue(
            PaymentReconciliation.objects.filter(target_invoice=invoice).exists()
        )

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_expires_stale_payment(self, mock_client):
        invoice = self.make_invoice("INV-EXPIRE")
        payment = RazorpayPayment.objects.create(
            payment_link_id="plink_stale",
            invoice=invoice,
            amount=invoice.total_gross,
        )
        stale = care_now() - timedelta(seconds=settings.RAZORPAY_PAYMENT_MAX_AGE + 60)
        RazorpayPayment.objects.filter(pk=payment.pk).update(created_date=stale)

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.EXPIRED)
        mock_client.payment_link.fetch.assert_not_called()


class TestRazorpayWebhookDispatch(RazorpayTestBase):
    @patch("abdm.signals.scan_pay.scan_pay_notify.delay")
    @patch("care_razorpay.payments.rebalance_account_task.delay")
    def test_standalone_payment_is_handled_before_abdm(self, mock_rebalance, mock_task):
        invoice = self.make_invoice("INV-DISPATCH")
        payment = RazorpayPayment.objects.create(
            payment_link_id="plink_dispatch",
            invoice=invoice,
            amount=invoice.total_gross,
        )
        order = self.make_order("plink_dispatch")
        order.invoice = invoice
        order.save(update_fields=["invoice"])

        response = dispatch_payment_link_event(
            _paid_event("plink_dispatch", invoice_id=invoice.external_id)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
        self.assertEqual(
            PaymentReconciliation.objects.filter(target_invoice=invoice).count(), 1
        )
        reconciliation = PaymentReconciliation.objects.get(target_invoice=invoice)
        self.assertEqual(reconciliation.method, "debc")

    @patch("abdm.signals.scan_pay.scan_pay_notify.delay")
    def test_unknown_link_falls_through_to_abdm(self, mock_task):
        order = self.make_order("plink_abdm")

        response = dispatch_payment_link_event(
            _paid_event(
                "plink_abdm",
                payment_id="pay_abdm",
                invoice_id=order.invoice.external_id,
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.SUCCESS)
        self.assertEqual(order.transaction_id, "pay_abdm")
        reconciliations = PaymentReconciliation.objects.filter(
            target_invoice=order.invoice
        )
        self.assertEqual(reconciliations.count(), 1)
        self.assertIn("Scan and pay", reconciliations.get().note)
        mock_task.assert_called_once()

    @patch("care_razorpay.api.viewsets.webhook.rebalance_account_task.delay")
    def test_legacy_notes_reconcile_when_link_is_unknown(self, mock_rebalance):
        invoice = self.make_invoice("INV-LEGACY")

        response = dispatch_payment_link_event(
            _paid_event("plink_legacy", invoice_id=invoice.external_id)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        reconciliation = PaymentReconciliation.objects.get(target_invoice=invoice)
        self.assertEqual(reconciliation.reference_number, "pay_test")
        self.assertEqual(reconciliation.note, "Payment made via Razorpay's payment link.")
        mock_rebalance.assert_called_once()

    def test_paid_event_without_payload_is_rejected(self):
        response = dispatch_payment_link_event({"event": "payment_link.paid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
