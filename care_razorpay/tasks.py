from celery import current_app, shared_task

from care_razorpay import payments
from care_razorpay.settings import plugin_settings as settings


@shared_task
def poll_pending_payments() -> None:
    if not settings.RAZORPAY_POLLING_ENABLED:
        return
    payments.poll_pending_payments()


@current_app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs) -> None:
    if not settings.RAZORPAY_POLLING_ENABLED:
        return
    sender.add_periodic_task(
        settings.RAZORPAY_POLLING_INTERVAL,
        poll_pending_payments.s(),
        name="razorpay_poll_pending_payments",
    )
