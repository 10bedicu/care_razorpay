import razorpay

from care_razorpay.settings import plugin_settings

razorpay_client = razorpay.Client(
    auth=(
        plugin_settings.RAZORPAY_KEY_ID,
        plugin_settings.RAZORPAY_KEY_SECRET,
    )
)
