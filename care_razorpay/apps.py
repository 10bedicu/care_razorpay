import logging

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)

PLUGIN_NAME = "care_razorpay"


class CareRazorpayConfig(AppConfig):
    name = PLUGIN_NAME
    verbose_name = _("Care Razorpay")

    def ready(self):
        # Register the scan-and-pay provider only if the ABDM plug is installed.
        try:
            import care_razorpay.scan_pay  # noqa: F401
        except ImportError:
            logger.info(
                "care_abdm not installed; skipping Razorpay scan-and-pay provider registration"
            )
