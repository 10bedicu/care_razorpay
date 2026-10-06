from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from abdm.models import AbhaNumber, HealthFacility, PaymentOrder
from abdm.models.payment_order import PaymentOrderStatus
from abdm.service.helper import uuid
from abdm.utils import user as abdm_user
from care_razorpay.api.serializers.payment_link import CreatePaymentLinkRequest
from care_razorpay.locks import RazorpayPaymentLock
from care_razorpay.models import (
    RazorpayAccount,
    RazorpayPayment,
    RazorpayQRCode,
    RazorpayWebhookEvent,
)
from care_razorpay.settings import plugin_settings as settings
from care_razorpay.webhooks import dispatch_payment_link_event
from django.core.cache import cache
from model_bakery import baker
from pydantic import ValidationError
from rest_framework import status

from care.emr.models.invoice import Invoice
from care.emr.models.payment_reconciliation import PaymentReconciliation
from care.emr.resources.invoice.spec import InvoiceStatusOptions
from care.emr.resources.payment_reconciliation.spec import (
    PaymentReconciliationOutcomeOptions,
    PaymentReconciliationStatusOptions,
    PaymentReconciliationTypeOptions,
)
from care.security.permissions.payment_reconciliation import (
    PaymentReconciliationPermissions,
)
from care.utils.lock import ObjectLocked
from care.utils.tests.base import CareAPITestBase
from care.utils.time_util import care_now
from care_razorpay import payments, tasks, webhooks
from care_razorpay import provider as razorpay_provider


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _paid_event(link_id, payment_id="pay_test", amount=10000, invoice_id=None):
    notes = {"invoice_id": str(invoice_id)} if invoice_id else {}
    return {
        "event": "payment_link.paid",
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "amount": amount,
                    "created_at": 1_700_000_000,
                }
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
        invoice = self.make_invoice("INV-RZP-REC")
        return PaymentOrder.objects.create(
            open_order_request_id=uuid(),
            abha_number=self.abha_number,
            health_facility=self.health_facility,
            invoice=invoice,
            order_number="INV-RZP-REC",
            payment_link_id=payment_link_id,
            provider="razorpay",
            status=PaymentOrderStatus.PAYMENT_INITIATED,
            amount=invoice.total_gross,
        )

    def make_payment(self, link_id, number="INV-PAY", **fields):
        invoice = self.make_invoice(number)
        return RazorpayPayment.objects.create(
            payment_link_id=link_id,
            invoice=invoice,
            amount=invoice.total_gross,
            **fields,
        )

    def record_payment(self, invoice, amount, is_credit_note=False, reference="x"):
        return baker.make(
            PaymentReconciliation,
            facility=self.facility,
            account=self.account,
            target_invoice=invoice,
            reconciliation_type=PaymentReconciliationTypeOptions.payment.value,
            status=PaymentReconciliationStatusOptions.active.value,
            outcome=PaymentReconciliationOutcomeOptions.complete.value,
            amount=Decimal(amount),
            tendered_amount=Decimal(amount),
            returned_amount=Decimal(0),
            is_credit_note=is_credit_note,
            reference_number=reference,
        )

    def past_deadline(self, extra_seconds=60):
        return care_now() - timedelta(
            seconds=settings.RAZORPAY_PAYMENT_MAX_AGE
            + settings.RAZORPAY_EXPIRY_GRACE_SECONDS
            + extra_seconds
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
        self.assertEqual(result["amount"], Decimal(100))
        payload = mock_client.payment_link.create.call_args.args[0]
        self.assertEqual(payload["amount"], 10000)
        transfer = payload["options"]["order"]["transfers"][0]
        self.assertEqual(transfer["account"], "acc_test")
        self.assertEqual(transfer["amount"], 10000)
        # scan-and-pay links expire when ABDM gives up on the order
        expected = _epoch(
            care_now()
            + timedelta(
                seconds=razorpay_provider.abdm_settings.ABDM_SCAN_AND_PAY_ORDER_MAX_AGE
            )
        )
        self.assertAlmostEqual(payload["expire_by"], expected, delta=5)
        self.assertFalse(
            RazorpayPayment.objects.filter(payment_link_id="plink_test").exists()
        )

    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_link_expiry_follows_abdm_max_age(self, mock_client):
        mock_client.payment_link.create.return_value = {
            "id": "plink_short",
            "short_url": "https://rzp.io/short",
        }

        with patch.object(
            razorpay_provider.abdm_settings, "ABDM_SCAN_AND_PAY_ORDER_MAX_AGE", 1800
        ):
            razorpay_provider.RazorpayProvider().create_payment_link(
                self.make_invoice()
            )

        payload = mock_client.payment_link.create.call_args.args[0]
        expected = _epoch(care_now() + timedelta(seconds=1800))
        self.assertAlmostEqual(payload["expire_by"], expected, delta=5)

        # Razorpay rejects expiries under 15 minutes, so that is the floor
        with patch.object(
            razorpay_provider.abdm_settings, "ABDM_SCAN_AND_PAY_ORDER_MAX_AGE", 60
        ):
            razorpay_provider.RazorpayProvider().create_payment_link(
                self.make_invoice("INV-RZP-FLOOR")
            )

        payload = mock_client.payment_link.create.call_args.args[0]
        expected = _epoch(care_now() + timedelta(seconds=payments.MIN_EXPIRY_SECONDS))
        self.assertAlmostEqual(payload["expire_by"], expected, delta=5)

    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_link_requires_facility_account(self, mock_client):
        invoice = self.make_invoice()
        invoice.facility = self.create_facility(self.user)

        with self.assertRaises(payments.RazorpayError):
            razorpay_provider.RazorpayProvider().create_payment_link(invoice)

        mock_client.payment_link.create.assert_not_called()

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
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

        with self.captureOnCommitCallbacks(execute=True):
            razorpay_provider.RazorpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.SUCCESS)
        self.assertEqual(order.transaction_id, "pay_test")
        self.assertTrue(
            PaymentReconciliation.objects.filter(target_invoice=order.invoice).exists()
        )
        mock_task.assert_called_once()

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    @patch("care_razorpay.payments.razorpay_client")
    def test_reconcile_records_amount_paid_not_invoice_total(self, mock_client, _task):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_test",
            "status": "paid",
            "amount_paid": 10000,
            "payments": [{"id": "pay_test", "amount": 10000, "status": "captured"}],
        }
        order = self.make_order("plink_test")
        # invoice edited after the link went out
        Invoice.objects.filter(pk=order.invoice_id).update(total_gross=Decimal(150))

        razorpay_provider.RazorpayProvider().reconcile_order(order)

        reconciliation = PaymentReconciliation.objects.get(target_invoice=order.invoice)
        self.assertEqual(reconciliation.amount, Decimal(100))

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    @patch("care_razorpay.payments.razorpay_client")
    def test_reconcile_ignores_unpaid(self, mock_client, mock_task):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_test",
            "status": "created",
            "expire_by": _epoch(care_now() + timedelta(hours=1)),
        }
        order = self.make_order("plink_test")

        razorpay_provider.RazorpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.PAYMENT_INITIATED)
        self.assertFalse(
            PaymentReconciliation.objects.filter(target_invoice=order.invoice).exists()
        )
        mock_task.assert_not_called()

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    @patch("care_razorpay.payments.razorpay_client")
    def test_reconcile_fails_expired_order(self, mock_client, mock_task):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_test",
            "status": "expired",
        }
        order = self.make_order("plink_test")

        with self.captureOnCommitCallbacks(execute=True):
            razorpay_provider.RazorpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.FAIL)
        self.assertEqual(
            order.invoice.status, InvoiceStatusOptions.entered_in_error.value
        )
        self.assertFalse(
            PaymentReconciliation.objects.filter(target_invoice=order.invoice).exists()
        )
        # the PHR is told the payment failed
        payload = mock_task.call_args.args[0]
        self.assertEqual(payload["acknowledgement"]["status"], "FAIL")

    @patch("care_razorpay.payments.razorpay_client")
    def test_reconcile_fails_order_past_expire_by(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_test",
            "status": "created",
            "expire_by": _epoch(care_now() - timedelta(minutes=1)),
        }
        order = self.make_order("plink_test")

        razorpay_provider.RazorpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.FAIL)

    @patch("care_razorpay.payments.razorpay_client")
    def test_reconcile_cancels_cancelled_order(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_test",
            "status": "cancelled",
        }
        order = self.make_order("plink_test")

        razorpay_provider.RazorpayProvider().reconcile_order(order)

        order.refresh_from_db()
        self.assertEqual(order.status, PaymentOrderStatus.CANCELED)


class TestRazorpayStandalonePayment(RazorpayTestBase):
    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_persists_record(self, mock_client):
        expire_by = _epoch(care_now() + timedelta(hours=1))
        mock_client.payment_link.create.return_value = {
            "id": "plink_standalone",
            "short_url": "https://rzp.io/standalone",
            "expire_by": expire_by,
        }

        payment, link = payments.create_payment(self.make_invoice())

        self.assertEqual(payment.status, RazorpayPayment.Status.CREATED)
        self.assertEqual(payment.payment_link_id, "plink_standalone")
        self.assertEqual(payment.payment_url, "https://rzp.io/standalone")
        self.assertEqual(payment.expires_at, datetime.fromtimestamp(expire_by, UTC))
        self.assertEqual(link["id"], "plink_standalone")
        transfer = mock_client.payment_link.create.call_args.args[0]["options"][
            "order"
        ]["transfers"][0]
        self.assertEqual(transfer["account"], "acc_test")

    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_defaults_expire_by_to_max_age(self, mock_client):
        mock_client.payment_link.create.return_value = {
            "id": "plink_default",
            "short_url": "https://rzp.io/default",
        }

        payments.create_payment(self.make_invoice())

        payload = mock_client.payment_link.create.call_args.args[0]
        expected = _epoch(
            care_now() + timedelta(seconds=settings.RAZORPAY_PAYMENT_MAX_AGE)
        )
        self.assertAlmostEqual(payload["expire_by"], expected, delta=5)

    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_sends_requested_expires_at(self, mock_client):
        mock_client.payment_link.create.return_value = {
            "id": "plink_custom",
            "short_url": "https://rzp.io/custom",
        }
        expires_at = care_now() + timedelta(days=2)

        payments.create_payment(self.make_invoice(), expires_at=expires_at)

        payload = mock_client.payment_link.create.call_args.args[0]
        self.assertEqual(payload["expire_by"], _epoch(expires_at))

    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_charges_outstanding_balance(self, mock_client):
        mock_client.payment_link.create.return_value = {
            "id": "plink_balance",
            "short_url": "https://rzp.io/balance",
        }
        invoice = self.make_invoice()
        self.record_payment(invoice, 30, reference="cash1")
        self.record_payment(invoice, 10, is_credit_note=True, reference="cn1")

        payment, _link = payments.create_payment(invoice)

        self.assertEqual(payment.amount, Decimal("80.00"))
        payload = mock_client.payment_link.create.call_args.args[0]
        self.assertEqual(payload["amount"], 8000)
        self.assertEqual(payload["options"]["order"]["transfers"][0]["amount"], 8000)

    @patch("care_razorpay.payments.razorpay_client")
    def test_create_payment_rejects_settled_invoice(self, mock_client):
        invoice = self.make_invoice()
        self.record_payment(invoice, 100)

        with self.assertRaises(payments.RazorpayError):
            payments.create_payment(invoice)
        mock_client.payment_link.create.assert_not_called()

    def test_amount_paise_is_exact(self):
        self.assertEqual(payments.amount_paise(Decimal("0.29")), 29)
        self.assertEqual(payments.amount_paise(Decimal("1234.565")), 123457)

    @patch("care_razorpay.payments.rebalance_account_task.delay")
    def test_event_reconciles_standalone_payment(self, mock_rebalance):
        invoice = self.make_invoice("INV-STANDALONE")
        payment = RazorpayPayment.objects.create(
            payment_link_id="plink_standalone",
            invoice=invoice,
            amount=invoice.total_gross,
        )

        with self.captureOnCommitCallbacks(execute=True):
            handled = payments.reconcile_event(
                {"id": "plink_standalone"},
                {"id": "pay_555", "amount": 10000, "created_at": 1_700_000_000},
                "payment_link.paid",
            )

        self.assertTrue(handled)
        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
        self.assertEqual(payment.reference, "pay_555")
        self.assertFalse(payment.needs_review)
        reconciliation = PaymentReconciliation.objects.get(target_invoice=invoice)
        self.assertEqual(reconciliation.amount, Decimal(100))
        mock_rebalance.assert_called_once_with(self.account.id)

    @patch("care_razorpay.payments.rebalance_account_task.delay")
    def test_rebalance_waits_for_commit(self, mock_rebalance):
        self.make_payment("plink_commit", "INV-COMMIT")

        with self.captureOnCommitCallbacks() as callbacks:
            payments.reconcile_event(
                {"id": "plink_commit"},
                {"id": "pay_commit", "amount": 10000},
                "payment_link.paid",
            )

        mock_rebalance.assert_not_called()
        self.assertEqual(len(callbacks), 1)

    def test_replayed_event_credits_once(self):
        payment = self.make_payment("plink_replay", "INV-REPLAY")
        event = ({"id": "plink_replay"}, {"id": "pay_replay", "amount": 10000})

        payments.reconcile_event(*event, "payment_link.paid")
        payments.reconcile_event(*event, "payment_link.paid")

        self.assertEqual(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).count(),
            1,
        )

    def test_stale_snapshots_cannot_double_credit(self):
        payment = self.make_payment("plink_stale_snap", "INV-STALE")
        snapshot_a = RazorpayPayment.objects.get(pk=payment.pk)
        snapshot_b = RazorpayPayment.objects.get(pk=payment.pk)

        payments._settle(snapshot_a, "pay_a", Decimal(100), None, final=True)  # noqa: SLF001
        # second worker got the link fetch back after the first already settled
        payments._settle_from_link(  # noqa: SLF001
            snapshot_b, {"id": "plink_stale_snap", "amount_paid": 10000}, final=True
        )

        self.assertEqual(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).count(),
            1,
        )

    def test_settlement_in_progress_elsewhere_raises(self):
        payment = self.make_payment("plink_locked", "INV-LOCK")

        with RazorpayPaymentLock(payment), self.assertRaises(ObjectLocked):
            payments.reconcile_event(
                {"id": "plink_locked"},
                {"id": "pay_locked", "amount": 10000},
                "payment_link.paid",
            )

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.CREATED)
        self.assertFalse(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).exists()
        )

    @patch("care_razorpay.payments.rebalance_account_task.delay")
    def test_partial_event_keeps_payment_open(self, mock_rebalance):
        invoice = self.make_invoice("INV-PARTIAL")
        payment = RazorpayPayment.objects.create(
            payment_link_id="plink_partial",
            invoice=invoice,
            amount=invoice.total_gross,
        )

        with self.captureOnCommitCallbacks(execute=True):
            payments.reconcile_event(
                {"id": "plink_partial"},
                {"id": "pay_partial", "amount": 4000, "created_at": 1_700_000_000},
                "payment_link.partially_paid",
            )

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PARTIALLY_PAID)
        self.assertFalse(payment.needs_review)
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
    def test_paid_event_settles_locally_expired_payment(self, mock_rebalance):
        payment = self.make_payment(
            "plink_late", "INV-LATE", status=RazorpayPayment.Status.EXPIRED
        )

        with self.captureOnCommitCallbacks(execute=True):
            handled = payments.reconcile_event(
                {"id": "plink_late"},
                {"id": "pay_late", "amount": 10000, "created_at": 1_700_000_000},
                "payment_link.paid",
            )

        self.assertTrue(handled)
        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
        self.assertEqual(payment.reference, "pay_late")
        self.assertTrue(payment.needs_review)
        self.assertIn("settled from status expired", payment.review_reason)
        reconciliation = PaymentReconciliation.objects.get(
            target_invoice=payment.invoice
        )
        self.assertIn("Needs review", reconciliation.note)
        mock_rebalance.assert_called_once()

    def test_paid_event_settles_cancelled_and_superseded_payments(self):
        for link_status in (
            RazorpayPayment.Status.CANCELLED,
            RazorpayPayment.Status.SUPERSEDED,
        ):
            payment = self.make_payment(
                f"plink_{link_status}", f"INV-{link_status}", status=link_status
            )

            payments.reconcile_event(
                {"id": f"plink_{link_status}"},
                {"id": f"pay_{link_status}", "amount": 10000},
                "payment_link.paid",
            )

            payment.refresh_from_db()
            self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
            self.assertIn(f"settled from status {link_status}", payment.review_reason)

    def test_terminal_marks_never_overwrite_each_other(self):
        payment = self.make_payment(
            "plink_superseded", "INV-SUP", status=RazorpayPayment.Status.SUPERSEDED
        )

        payments.reconcile_event(
            {"id": "plink_superseded"}, None, "payment_link.expired"
        )

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.SUPERSEDED)

    def test_pending_payment_and_supersede(self):
        invoice = self.make_invoice("INV-PENDING")
        RazorpayPayment.objects.create(
            payment_link_id="plink_dead",
            invoice=invoice,
            amount=Decimal(100),
            expires_at=care_now() - timedelta(hours=1),
        )
        live = RazorpayPayment.objects.create(
            payment_link_id="plink_live",
            invoice=invoice,
            amount=Decimal(100),
            expires_at=care_now() + timedelta(hours=1),
        )

        self.assertEqual(payments.pending_payment(invoice), live)
        self.assertTrue(payments.supersede(live))
        live.refresh_from_db()
        self.assertEqual(live.status, RazorpayPayment.Status.SUPERSEDED)
        self.assertIsNone(payments.pending_payment(invoice))

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
    def test_poll_expires_stale_payment_after_final_check(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_stale",
            "status": "created",
        }
        payment = self.make_payment("plink_stale", "INV-EXPIRE")
        RazorpayPayment.objects.filter(pk=payment.pk).update(
            created_date=self.past_deadline()
        )

        payments.poll_pending_payments()
        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.EXPIRED)
        self.assertFalse(payment.needs_review)
        # one last look at the gateway, then never polled again
        mock_client.payment_link.fetch.assert_called_once_with("plink_stale")

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_keeps_polling_through_grace_period(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_grace",
            "status": "created",
        }
        payment = self.make_payment(
            "plink_grace", "INV-GRACE", expires_at=care_now() - timedelta(seconds=1)
        )

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.CREATED)

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_does_not_expire_while_gateway_is_unreachable(self, mock_client):
        mock_client.payment_link.fetch.side_effect = RuntimeError("gateway down")
        payment = self.make_payment("plink_down", "INV-DOWN")
        RazorpayPayment.objects.filter(pk=payment.pk).update(
            created_date=self.past_deadline()
        )

        payments.poll_pending_payments()
        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.CREATED)
        # an unanswered payment is left alone for the backoff period
        self.assertEqual(mock_client.payment_link.fetch.call_count, 1)
        self.assertTrue(cache.get(payments.backoff_key(payment)))

        cache.delete(payments.backoff_key(payment))
        payments.poll_pending_payments()

        self.assertEqual(mock_client.payment_link.fetch.call_count, 2)

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_backs_off_only_when_gateway_did_not_answer(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_answered",
            "status": "created",
            "expire_by": _epoch(care_now() + timedelta(hours=1)),
        }
        payment = self.make_payment("plink_answered", "INV-ANSWERED")

        payments.poll_pending_payments()
        payments.poll_pending_payments()

        self.assertEqual(mock_client.payment_link.fetch.call_count, 2)
        self.assertIsNone(cache.get(payments.backoff_key(payment)))

    @patch("care_razorpay.tasks.Lock")
    def test_poll_task_does_nothing_when_nothing_is_pending(self, mock_lock):
        self.make_payment(
            "plink_settled", "INV-SETTLED", status=RazorpayPayment.Status.PAID
        )

        tasks.poll_pending_payments()

        mock_lock.assert_not_called()

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_gives_up_after_hard_cap_and_flags_for_review(self, mock_client):
        mock_client.payment_link.fetch.side_effect = RuntimeError("gateway down")
        payment = self.make_payment("plink_capped", "INV-CAP")
        RazorpayPayment.objects.filter(pk=payment.pk).update(
            created_date=self.past_deadline(
                settings.RAZORPAY_EXPIRY_HARD_CAP_SECONDS + 60
            )
        )

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.EXPIRED)
        self.assertTrue(payment.needs_review)
        self.assertIn("unreachable", payment.review_reason)

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_skips_payment_being_settled_elsewhere(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_busy",
            "status": "paid",
            "amount_paid": 10000,
        }
        payment = self.make_payment("plink_busy", "INV-BUSY")

        with RazorpayPaymentLock(payment):
            payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.CREATED)

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_expires_stale_partially_paid_payment(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_partial_stale",
            "status": "partially_paid",
            "payments": [],
        }
        payment = self.make_payment(
            "plink_partial_stale",
            "INV-PARTIAL-STALE",
            status=RazorpayPayment.Status.PARTIALLY_PAID,
            reference="pay_first",
        )
        RazorpayPayment.objects.filter(pk=payment.pk).update(
            created_date=self.past_deadline()
        )

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.EXPIRED)

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_stops_at_link_expiry(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_short",
            "status": "created",
        }
        # fresh row whose link (plus grace) already expired at the gateway
        payment = self.make_payment(
            "plink_short",
            "INV-SHORT",
            expires_at=care_now()
            - timedelta(seconds=settings.RAZORPAY_EXPIRY_GRACE_SECONDS + 1),
        )

        payments.poll_pending_payments()
        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.EXPIRED)
        mock_client.payment_link.fetch.assert_called_once_with("plink_short")

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_keeps_polling_until_link_expiry(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_long",
            "status": "created",
        }
        # old row whose link is still valid at the gateway
        payment = self.make_payment(
            "plink_long", "INV-LONG", expires_at=care_now() + timedelta(days=1)
        )
        RazorpayPayment.objects.filter(pk=payment.pk).update(
            created_date=self.past_deadline()
        )

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.CREATED)
        mock_client.payment_link.fetch.assert_called_once_with("plink_long")

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_backfills_expires_at_from_gateway(self, mock_client):
        expire_by = _epoch(care_now() + timedelta(hours=2))
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_legacy",
            "status": "created",
            "expire_by": expire_by,
        }
        payment = self.make_payment("plink_legacy", "INV-LEGACY-ROW")

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.CREATED)
        self.assertEqual(payment.expires_at, datetime.fromtimestamp(expire_by, UTC))

    @patch("care_razorpay.payments.razorpay_client")
    def test_poll_treats_passed_expire_by_as_expired(self, mock_client):
        mock_client.payment_link.fetch.return_value = {
            "id": "plink_lagging",
            "status": "created",
            "expire_by": _epoch(care_now() - timedelta(minutes=1)),
        }
        payment = self.make_payment("plink_lagging", "INV-LAGGING")

        payments.poll_pending_payments()

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.EXPIRED)


class TestCreatePaymentLinkRequest(RazorpayTestBase):
    def payload(self, expires_at):
        return {
            "invoice_id": str(self.make_invoice().external_id),
            "expires_at": expires_at.isoformat(),
        }

    def test_accepts_expiry_at_least_15_minutes_ahead(self):
        expires_at = care_now() + timedelta(minutes=16)

        request = CreatePaymentLinkRequest.model_validate(self.payload(expires_at))

        self.assertEqual(request.expires_at, expires_at)

    def test_rejects_expiry_under_15_minutes(self):
        with self.assertRaises(ValidationError):
            CreatePaymentLinkRequest.model_validate(
                self.payload(care_now() + timedelta(minutes=5))
            )

    def test_rejects_naive_expiry(self):
        with self.assertRaises(ValidationError):
            CreatePaymentLinkRequest.model_validate(
                self.payload(datetime(2099, 1, 1, 12, 0))  # no tzinfo
            )


class TestRazorpayWebhookDispatch(RazorpayTestBase):
    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
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

    @patch("abdm.tasks.scan_pay.scan_pay_notify.delay")
    def test_unknown_link_falls_through_to_abdm(self, mock_task):
        order = self.make_order("plink_abdm")

        with self.captureOnCommitCallbacks(execute=True):
            response = dispatch_payment_link_event(
                _paid_event(
                    "plink_abdm",
                    payment_id="pay_abdm",
                    amount=9000,
                    invoice_id=order.invoice.external_id,
                )
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        # short-paid: the confirmed amount is recorded and the order stays pending
        self.assertEqual(order.status, PaymentOrderStatus.PENDING)
        self.assertEqual(order.transaction_id, "pay_abdm")
        reconciliations = PaymentReconciliation.objects.filter(
            target_invoice=order.invoice
        )
        self.assertEqual(reconciliations.count(), 1)
        self.assertEqual(reconciliations.get().amount, Decimal(90))
        self.assertIn("Scan and pay", reconciliations.get().note)
        mock_task.assert_called_once()

    @patch("care_razorpay.payments.rebalance_account_task.delay")
    def test_legacy_notes_reconcile_when_link_is_unknown(self, mock_rebalance):
        invoice = self.make_invoice("INV-LEGACY")

        with self.captureOnCommitCallbacks(execute=True):
            response = dispatch_payment_link_event(
                _paid_event("plink_legacy", invoice_id=invoice.external_id)
            )
            replay = dispatch_payment_link_event(
                _paid_event("plink_legacy", invoice_id=invoice.external_id)
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        reconciliation = PaymentReconciliation.objects.get(target_invoice=invoice)
        self.assertEqual(reconciliation.reference_number, "pay_test")
        self.assertEqual(reconciliation.amount, Decimal(100))
        self.assertEqual(
            reconciliation.note, "Payment made via Razorpay's payment link."
        )
        mock_rebalance.assert_called_once()

    def test_paid_event_without_payload_is_rejected(self):
        response = dispatch_payment_link_event({"event": "payment_link.paid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("care_razorpay.payments.rebalance_account_task.delay")
    def test_qr_code_credit_is_recorded_once(self, mock_rebalance):
        invoice = self.make_invoice("INV-QR")
        event = {
            "event": "qr_code.credited",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_qr",
                        "amount": 2550,
                        "created_at": 1_700_000_000,
                    }
                },
                "qr_code": {
                    "entity": {
                        "id": "qr_1",
                        "notes": {"invoice_id": str(invoice.external_id)},
                    }
                },
            },
        }

        with self.captureOnCommitCallbacks(execute=True):
            first = webhooks.dispatch_qr_code_event(event)
            second = webhooks.dispatch_qr_code_event(event)

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        reconciliation = PaymentReconciliation.objects.get(target_invoice=invoice)
        self.assertEqual(reconciliation.amount, Decimal("25.50"))
        self.assertEqual(reconciliation.reference_number, "pay_qr")
        mock_rebalance.assert_called_once()

    def test_qr_code_credit_updates_local_record(self):
        invoice = self.make_invoice("INV-QR-REC")
        record = RazorpayQRCode.objects.create(
            qr_id="qr_rec", invoice=invoice, amount=invoice.total_gross
        )
        event = {
            "event": "qr_code.credited",
            "payload": {
                "payment": {"entity": {"id": "pay_rec", "amount": 10000}},
                "qr_code": {
                    "entity": {
                        "id": "qr_rec",
                        "status": "closed",
                        "payments_amount_received": 10000,
                        "payments_count_received": 1,
                        "notes": {"invoice_id": str(invoice.external_id)},
                    }
                },
            },
        }

        with self.captureOnCommitCallbacks(execute=True):
            response = webhooks.dispatch_qr_code_event(event)

        self.assertEqual(response.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.status, RazorpayQRCode.Status.CLOSED)
        self.assertEqual(record.amount_received, Decimal(100))
        self.assertEqual(record.payments_count, 1)


WEBHOOK_URL = "/api/care_razorpay/webhook/payment_link/"
PAYMENT_LINK_URL = "/api/care_razorpay/payment_link/"
QR_CODE_URL = "/api/care_razorpay/qr_code/"


@patch("care_razorpay.api.authentication.razorpay_client")
class TestRazorpayWebhookEndpoint(RazorpayTestBase):
    def post_event(self, data, event_id="evt_1", **headers):
        return self.client.post(
            WEBHOOK_URL,
            data,
            format="json",
            headers={
                "X-Razorpay-Signature": "sig",
                "X-Razorpay-Event-Id": event_id,
                **headers,
            },
        )

    def test_rejects_unsigned_or_forged_events(self, mock_client):
        mock_client.utility.verify_webhook_signature.side_effect = ValueError("bad")

        forged = self.post_event(_paid_event("plink_x"))
        unsigned = self.client.post(WEBHOOK_URL, _paid_event("plink_x"), format="json")

        self.assertIn(forged.status_code, (401, 403))
        self.assertIn(unsigned.status_code, (401, 403))
        self.assertFalse(RazorpayWebhookEvent.objects.exists())

    def test_settles_payment_and_stores_event(self, _client):
        payment = self.make_payment("plink_http", "INV-HTTP")

        response = self.post_event(_paid_event("plink_http", payment_id="pay_http"))

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
        event = RazorpayWebhookEvent.objects.get()
        self.assertEqual(event.event_id, "evt_1")
        self.assertEqual(event.event, "payment_link.paid")
        self.assertEqual(event.status, RazorpayWebhookEvent.Status.PROCESSED)
        self.assertEqual(event.payload["event"], "payment_link.paid")

    def test_redelivery_is_idempotent(self, _client):
        payment = self.make_payment("plink_redeliver", "INV-RD")
        data = _paid_event("plink_redeliver", payment_id="pay_rd")

        first = self.post_event(data)
        second = self.post_event(data)

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(RazorpayWebhookEvent.objects.count(), 1)
        self.assertEqual(
            PaymentReconciliation.objects.filter(
                target_invoice=payment.invoice
            ).count(),
            1,
        )

    def test_unactionable_event_is_kept_as_ignored(self, _client):
        response = self.post_event(_paid_event("plink_nobody", invoice_id=uuid()))

        self.assertEqual(response.status_code, 400)
        event = RazorpayWebhookEvent.objects.get()
        self.assertEqual(event.status, RazorpayWebhookEvent.Status.IGNORED)
        self.assertIn("Invoice not found", event.error_message)

    def test_processing_failure_returns_500_then_replays(self, _client):
        payment = self.make_payment("plink_fail", "INV-FAIL")
        data = _paid_event("plink_fail", payment_id="pay_fail")

        with RazorpayPaymentLock(payment):
            response = self.post_event(data)
        self.assertEqual(response.status_code, 500)
        event = RazorpayWebhookEvent.objects.get()
        self.assertEqual(event.status, RazorpayWebhookEvent.Status.FAILED)
        self.assertEqual(event.attempts, 1)
        self.assertIn("ObjectLocked", event.error_message)

        self.assertEqual(webhooks.replay_failed(), 1)

        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
        event.refresh_from_db()
        self.assertEqual(event.status, RazorpayWebhookEvent.Status.PROCESSED)

    def test_replay_gives_up_after_max_attempts(self, _client):
        payment = self.make_payment("plink_giveup", "INV-GIVEUP")
        with RazorpayPaymentLock(payment):
            self.post_event(_paid_event("plink_giveup"))
        RazorpayWebhookEvent.objects.update(
            attempts=settings.RAZORPAY_WEBHOOK_MAX_ATTEMPTS
        )

        self.assertEqual(webhooks.replay_failed(), 0)

    def test_event_without_id_is_keyed_by_body(self, _client):
        self.make_payment("plink_noid", "INV-NOID")
        data = _paid_event("plink_noid", payment_id="pay_noid")

        first = self.client.post(
            WEBHOOK_URL, data, format="json", headers={"X-Razorpay-Signature": "s"}
        )
        second = self.client.post(
            WEBHOOK_URL, data, format="json", headers={"X-Razorpay-Signature": "s"}
        )

        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(RazorpayWebhookEvent.objects.count(), 1)


class TestRazorpayCollectionAPI(RazorpayTestBase):
    def setUp(self):
        super().setUp()
        self.staff = self.create_user()
        role = self.create_role_with_permissions(
            [
                PaymentReconciliationPermissions.can_write_payment_reconciliation.name,
                PaymentReconciliationPermissions.can_read_payment_reconciliation.name,
            ]
        )
        organization = self.create_facility_organization(self.facility)
        self.attach_role_facility_organization_user(organization, self.staff, role)
        self.client.force_authenticate(user=self.staff)

    def link(self, link_id, amount=10000):
        return {
            "id": link_id,
            "short_url": f"https://rzp.io/{link_id}",
            "created_at": 1_700_000_000,
            "amount": amount,
            "amount_paid": 0,
            "status": "created",
            "expire_by": _epoch(care_now() + timedelta(hours=1)),
        }

    def post_link(self, invoice):
        return self.client.post(
            PAYMENT_LINK_URL, {"invoice_id": str(invoice.external_id)}, format="json"
        )

    @patch("care_razorpay.payments.razorpay_client")
    def test_creates_link_for_outstanding_balance(self, mock_client):
        mock_client.payment_link.create.return_value = self.link("plink_api", 7000)
        invoice = self.make_invoice("INV-API")
        self.record_payment(invoice, 30)

        response = self.post_link(invoice)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["id"], "plink_api")
        self.assertEqual(response.data["amount"], 70.0)
        payload = mock_client.payment_link.create.call_args.args[0]
        self.assertEqual(payload["amount"], 7000)
        payment = RazorpayPayment.objects.get(payment_link_id="plink_api")
        self.assertEqual(payment.amount, Decimal("70.00"))

    @patch("care_razorpay.payments.razorpay_client")
    def test_requires_facility_permission(self, mock_client):
        self.client.force_authenticate(user=self.create_user())

        response = self.post_link(self.make_invoice("INV-403"))

        self.assertEqual(response.status_code, 403)
        mock_client.payment_link.create.assert_not_called()

    def test_unknown_invoice_is_404(self):
        response = self.client.post(
            PAYMENT_LINK_URL, {"invoice_id": str(uuid())}, format="json"
        )

        self.assertEqual(response.status_code, 404)

    @patch("care_razorpay.payments.razorpay_client")
    def test_rejects_invoices_that_are_not_issued(self, mock_client):
        for invoice_status in (
            InvoiceStatusOptions.draft,
            InvoiceStatusOptions.balanced,
            InvoiceStatusOptions.cancelled,
            InvoiceStatusOptions.entered_in_error,
        ):
            invoice = self.make_invoice(f"INV-{invoice_status.value}")
            Invoice.objects.filter(pk=invoice.pk).update(status=invoice_status.value)

            response = self.post_link(invoice)

            self.assertEqual(response.status_code, 400, invoice_status)
        mock_client.payment_link.create.assert_not_called()

    @patch("care_razorpay.payments.razorpay_client")
    def test_rejects_invoice_without_outstanding_balance(self, mock_client):
        invoice = self.make_invoice("INV-SETTLED")
        self.record_payment(invoice, 100)

        response = self.post_link(invoice)

        self.assertEqual(response.status_code, 400)
        mock_client.payment_link.create.assert_not_called()

    @patch("care_razorpay.api.viewsets.payment_link.razorpay_client")
    @patch("care_razorpay.payments.razorpay_client")
    def test_reuses_live_link(self, mock_client, mock_view_client):
        mock_client.payment_link.create.return_value = self.link("plink_reuse")
        mock_view_client.payment_link.fetch.return_value = self.link("plink_reuse")
        invoice = self.make_invoice("INV-REUSE")

        first = self.post_link(invoice)
        second = self.post_link(invoice)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["id"], "plink_reuse")
        mock_client.payment_link.create.assert_called_once()
        mock_view_client.payment_link.fetch.assert_called_once_with("plink_reuse")

    @patch("care_razorpay.payments.razorpay_client")
    def test_supersedes_link_when_balance_changes(self, mock_client):
        mock_client.payment_link.create.side_effect = [
            self.link("plink_old"),
            self.link("plink_new", 7000),
        ]
        invoice = self.make_invoice("INV-SUPERSEDE")
        self.post_link(invoice)
        self.record_payment(invoice, 30)

        second = self.post_link(invoice)

        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.data["id"], "plink_new")
        old = RazorpayPayment.objects.get(payment_link_id="plink_old")
        self.assertEqual(old.status, RazorpayPayment.Status.SUPERSEDED)

    @patch("care_razorpay.api.viewsets.payment_link.razorpay_client")
    def test_retrieve_is_scoped_to_known_links(self, mock_view_client):
        mock_view_client.payment_link.fetch.return_value = self.link("plink_known")
        self.make_payment("plink_known", "INV-KNOWN")

        known = self.client.get(f"{PAYMENT_LINK_URL}plink_known/")
        unknown = self.client.get(f"{PAYMENT_LINK_URL}plink_other/")

        self.assertEqual(known.status_code, 200)
        self.assertEqual(unknown.status_code, 404)
        mock_view_client.payment_link.fetch.assert_called_once_with("plink_known")

    @patch("care_razorpay.api.viewsets.payment_link.razorpay_client")
    def test_retrieve_requires_facility_permission(self, mock_view_client):
        self.make_payment("plink_private", "INV-PRIVATE")
        self.client.force_authenticate(user=self.create_user())

        response = self.client.get(f"{PAYMENT_LINK_URL}plink_private/")

        self.assertEqual(response.status_code, 403)
        mock_view_client.payment_link.fetch.assert_not_called()

    @patch("care_razorpay.api.viewsets.qr_code.razorpay_client")
    def test_qr_code_charges_outstanding_and_requires_permission(self, mock_client):
        mock_client.qrcode.create.return_value = {
            "id": "qr_1",
            "image_url": "https://rzp.io/qr",
            "created_at": 1_700_000_000,
            "payment_amount": 7000,
            "payments_amount_received": 0,
            "payments_count_received": 0,
            "status": "active",
        }
        invoice = self.make_invoice("INV-QR-API")
        self.record_payment(invoice, 30)
        body = {
            "invoice_id": str(invoice.external_id),
            "usage": "single_use",
            "is_amount_fixed": True,
        }

        response = self.client.post(QR_CODE_URL, body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            mock_client.qrcode.create.call_args.args[0]["payment_amount"], 7000
        )
        record = RazorpayQRCode.objects.get(qr_id="qr_1")
        self.assertEqual(record.invoice, invoice)
        self.assertEqual(record.amount, Decimal("70.00"))
        self.assertEqual(record.usage, "single_use")
        self.assertEqual(record.status, RazorpayQRCode.Status.ACTIVE)

        self.client.force_authenticate(user=self.create_user())
        self.assertEqual(
            self.client.post(QR_CODE_URL, body, format="json").status_code, 403
        )

    def make_link_record(self, link_id, invoice, **fields):
        return RazorpayPayment.objects.create(
            payment_link_id=link_id,
            invoice=invoice,
            amount=invoice.total_gross,
            payment_url=f"https://rzp.io/{link_id}",
            **fields,
        )

    def test_lists_links_for_invoice_newest_first(self):
        invoice = self.make_invoice("INV-LINK-LIST")
        self.make_link_record(
            "plink_old", invoice, status=RazorpayPayment.Status.SUPERSEDED
        )
        self.make_link_record("plink_live", invoice)
        self.make_payment("plink_other", "INV-ELSEWHERE")

        response = self.client.get(
            PAYMENT_LINK_URL, {"invoice": str(invoice.external_id)}
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            [row["payment_link_id"] for row in response.data["results"]],
            ["plink_live", "plink_old"],
        )
        row = response.data["results"][0]
        self.assertEqual(row["invoice_id"], str(invoice.external_id))
        self.assertEqual(row["payment_url"], "https://rzp.io/plink_live")
        self.assertEqual(row["status"], "created")

        response = self.client.get(
            PAYMENT_LINK_URL,
            {"invoice": str(invoice.external_id), "status": "created"},
        )

        self.assertEqual(
            [row["payment_link_id"] for row in response.data["results"]],
            ["plink_live"],
        )

    def test_list_requires_invoice_and_permission(self):
        invoice = self.make_invoice("INV-LINK-403")

        self.assertEqual(self.client.get(PAYMENT_LINK_URL).status_code, 400)
        self.assertEqual(
            self.client.get(PAYMENT_LINK_URL, {"invoice": str(uuid())}).status_code,
            404,
        )

        self.client.force_authenticate(user=self.create_user())
        response = self.client.get(
            PAYMENT_LINK_URL, {"invoice": str(invoice.external_id)}
        )

        self.assertEqual(response.status_code, 403)

    @patch("care_razorpay.payments.razorpay_client")
    def test_refresh_settles_paid_link_locally(self, mock_client):
        invoice = self.make_invoice("INV-LINK-REFRESH")
        payment = self.make_link_record("plink_refresh", invoice)
        link = self.link("plink_refresh")
        link.update(
            status="paid",
            amount_paid=10000,
            payments=[
                {
                    "id": "pay_refresh",
                    "amount": 10000,
                    "status": "captured",
                    "created_at": 1_700_000_000,
                }
            ],
        )
        mock_client.payment_link.fetch.return_value = link

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"{PAYMENT_LINK_URL}plink_refresh/refresh/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["gateway_checked"])
        self.assertEqual(response.data["payment"]["status"], "paid")
        self.assertEqual(response.data["payment"]["reference"], "pay_refresh")
        self.assertEqual(response.data["link"]["amount_paid"], 100.0)
        payment.refresh_from_db()
        self.assertEqual(payment.status, RazorpayPayment.Status.PAID)
        reconciliation = PaymentReconciliation.objects.get(target_invoice=invoice)
        self.assertEqual(reconciliation.reference_number, "pay_refresh")
        self.assertEqual(reconciliation.amount, Decimal(100))

    @patch("care_razorpay.payments.razorpay_client")
    def test_refresh_reports_unreachable_gateway(self, mock_client):
        invoice = self.make_invoice("INV-LINK-DOWN")
        self.make_link_record("plink_down", invoice)
        mock_client.payment_link.fetch.side_effect = RuntimeError("gateway down")

        response = self.client.post(f"{PAYMENT_LINK_URL}plink_down/refresh/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["gateway_checked"])
        self.assertIsNone(response.data["link"])
        self.assertEqual(response.data["payment"]["status"], "created")

    @patch("care_razorpay.payments.razorpay_client")
    def test_refresh_requires_permission_and_known_link(self, mock_client):
        self.assertEqual(
            self.client.post(f"{PAYMENT_LINK_URL}plink_nope/refresh/").status_code,
            404,
        )
        invoice = self.make_invoice("INV-LINK-PRIV")
        self.make_link_record("plink_priv", invoice)
        self.client.force_authenticate(user=self.create_user())

        response = self.client.post(f"{PAYMENT_LINK_URL}plink_priv/refresh/")

        self.assertEqual(response.status_code, 403)
        mock_client.payment_link.fetch.assert_not_called()

    def make_qr_record(self, qr_id, invoice, **fields):
        return RazorpayQRCode.objects.create(
            qr_id=qr_id,
            invoice=invoice,
            amount=invoice.total_gross,
            image_url=f"https://rzp.io/{qr_id}.png",
            usage="single_use",
            **fields,
        )

    def gateway_qr(self, qr_id, received=0, count=0, qr_status="active"):
        return {
            "id": qr_id,
            "image_url": f"https://rzp.io/{qr_id}.png",
            "created_at": 1_700_000_000,
            "payment_amount": 10000,
            "payments_amount_received": received,
            "payments_count_received": count,
            "status": qr_status,
        }

    @patch("care_razorpay.api.viewsets.qr_code.razorpay_client")
    def test_qr_code_retrieve_is_scoped_to_known_codes(self, mock_client):
        mock_client.qrcode.fetch.return_value = self.gateway_qr("qr_known")
        self.make_qr_record("qr_known", self.make_invoice("INV-QR-KNOWN"))

        known = self.client.get(f"{QR_CODE_URL}qr_known/")
        unknown = self.client.get(f"{QR_CODE_URL}qr_other/")

        self.assertEqual(known.status_code, 200, known.data)
        self.assertEqual(unknown.status_code, 404)
        mock_client.qrcode.fetch.assert_called_once_with("qr_known")

        self.client.force_authenticate(user=self.create_user())
        self.assertEqual(self.client.get(f"{QR_CODE_URL}qr_known/").status_code, 403)

    def test_lists_qr_codes_for_invoice(self):
        invoice = self.make_invoice("INV-QR-LIST")
        self.make_qr_record("qr_closed", invoice, status=RazorpayQRCode.Status.CLOSED)
        self.make_qr_record("qr_open", invoice)
        self.make_qr_record("qr_elsewhere", self.make_invoice("INV-QR-OTHER"))

        response = self.client.get(QR_CODE_URL, {"invoice": str(invoice.external_id)})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["qr_id"] for row in response.data["results"]], ["qr_open", "qr_closed"]
        )
        self.assertEqual(response.data["results"][0]["amount"], "100.00")

        response = self.client.get(
            QR_CODE_URL, {"invoice": str(invoice.external_id), "status": "active"}
        )
        self.assertEqual(
            [row["qr_id"] for row in response.data["results"]], ["qr_open"]
        )

    @patch("care_razorpay.payments.razorpay_client")
    def test_qr_code_refresh_credits_unseen_payments_once(self, mock_client):
        invoice = self.make_invoice("INV-QR-REFRESH")
        record = self.make_qr_record("qr_refresh", invoice)
        mock_client.qrcode.fetch.return_value = self.gateway_qr(
            "qr_refresh", received=2550, count=1, qr_status="closed"
        )
        mock_client.qrcode.fetch_all_payments.return_value = {
            "entity": "collection",
            "count": 2,
            "items": [
                {
                    "id": "pay_qr_ok",
                    "amount": 2550,
                    "status": "captured",
                    "created_at": 1_700_000_000,
                },
                {"id": "pay_qr_failed", "amount": 2550, "status": "failed"},
            ],
        }

        with self.captureOnCommitCallbacks(execute=True):
            first = self.client.post(f"{QR_CODE_URL}qr_refresh/refresh/")
            second = self.client.post(f"{QR_CODE_URL}qr_refresh/refresh/")

        self.assertEqual(first.status_code, 200, first.data)
        self.assertTrue(first.data["gateway_checked"])
        self.assertEqual(first.data["qr_code"]["status"], "closed")
        self.assertEqual(first.data["qr_code"]["amount_received"], "25.50")
        self.assertEqual(first.data["gateway"]["payments_amount_received"], 25.5)
        self.assertEqual(second.status_code, 200)
        reconciliations = PaymentReconciliation.objects.filter(target_invoice=invoice)
        self.assertEqual(reconciliations.count(), 1)
        reconciliation = reconciliations.get()
        self.assertEqual(reconciliation.reference_number, "pay_qr_ok")
        self.assertEqual(reconciliation.amount, Decimal("25.50"))
        self.assertIn("QR code", reconciliation.note)
        record.refresh_from_db()
        self.assertEqual(record.status, RazorpayQRCode.Status.CLOSED)
        self.assertEqual(record.payments_count, 1)

    @patch("care_razorpay.payments.razorpay_client")
    def test_qr_code_refresh_skips_payments_the_webhook_recorded(self, mock_client):
        invoice = self.make_invoice("INV-QR-WEBHOOKED")
        self.make_qr_record("qr_hooked", invoice)
        self.record_payment(invoice, 25.5, reference="pay_hooked")
        mock_client.qrcode.fetch.return_value = self.gateway_qr(
            "qr_hooked", received=2550, count=1
        )
        mock_client.qrcode.fetch_all_payments.return_value = {
            "items": [{"id": "pay_hooked", "amount": 2550, "status": "captured"}]
        }

        response = self.client.post(f"{QR_CODE_URL}qr_hooked/refresh/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            PaymentReconciliation.objects.filter(target_invoice=invoice).count(), 1
        )

    @patch("care_razorpay.payments.razorpay_client")
    def test_qr_code_refresh_reports_unreachable_gateway(self, mock_client):
        invoice = self.make_invoice("INV-QR-DOWN")
        self.make_qr_record("qr_down", invoice)
        mock_client.qrcode.fetch.side_effect = RuntimeError("gateway down")

        response = self.client.post(f"{QR_CODE_URL}qr_down/refresh/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["gateway_checked"])
        self.assertIsNone(response.data["gateway"])
        self.assertEqual(response.data["qr_code"]["status"], "active")

    @patch("care_razorpay.payments.razorpay_client")
    def test_qr_code_refresh_requires_permission(self, mock_client):
        invoice = self.make_invoice("INV-QR-PRIV")
        self.make_qr_record("qr_priv", invoice)
        self.client.force_authenticate(user=self.create_user())

        response = self.client.post(f"{QR_CODE_URL}qr_priv/refresh/")

        self.assertEqual(response.status_code, 403)
        mock_client.qrcode.fetch.assert_not_called()
