import logging

from celery import current_app, shared_task

from care_razorpay import payments, webhooks
from care_razorpay.settings import plugin_settings as settings

logger = logging.getLogger(__name__)


@shared_task
def poll_pending_payments() -> None:
    if not settings.RAZORPAY_POLLING_ENABLED:
        return
    payments.poll_pending_payments()


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
