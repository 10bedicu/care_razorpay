import logging

from celery import current_app, shared_task

from care.utils.lock import Lock, ObjectLocked
from care_razorpay import payments, webhooks
from care_razorpay.settings import plugin_settings as settings

logger = logging.getLogger(__name__)

POLL_LOCK_KEY = "razorpay:poll_pending_payments"
POLL_LOCK_TIMEOUT = 300


@shared_task
def poll_pending_payments() -> None:
    if not settings.RAZORPAY_POLLING_ENABLED:
        return
    try:
        with Lock(POLL_LOCK_KEY, POLL_LOCK_TIMEOUT):
            payments.poll_pending_payments()
    except ObjectLocked:
        logger.info("Previous Razorpay poll is still running; skipping this tick")


@shared_task
def replay_failed_webhook_events() -> None:
    replayed = webhooks.replay_failed()
    if replayed:
        logger.info("Replayed %s failed Razorpay webhook(s)", replayed)


@current_app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs) -> None:
    sender.add_periodic_task(
        settings.RAZORPAY_WEBHOOK_REPLAY_INTERVAL,
        replay_failed_webhook_events.s(),
        name="razorpay_replay_failed_webhook_events",
    )
    if not settings.RAZORPAY_POLLING_ENABLED:
        return
    sender.add_periodic_task(
        settings.RAZORPAY_POLLING_INTERVAL,
        poll_pending_payments.s(),
        name="razorpay_poll_pending_payments",
    )
