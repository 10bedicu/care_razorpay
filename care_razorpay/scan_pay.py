from abdm.service.v3.payment_providers import PaymentProvider, register_provider

from care_razorpay.utils.razorpay import razorpay_client


@register_provider
class RazorpayProvider(PaymentProvider):
    name = "razorpay"

    def create_payment_link(self, invoice) -> dict:
        link = razorpay_client.payment_link.create(
            {
                "amount": round(float(invoice.total_gross) * 100),
                "currency": "INR",
                "accept_partial": False,
                "description": invoice.title,
                "customer": {
                    "name": invoice.patient.name,
                    "contact": invoice.patient.phone_number,
                },
                "notify": {"sms": False, "email": False},
                "reminder_enable": False,
                "notes": {
                    "invoice_id": str(invoice.external_id),
                    "account_id": str(invoice.account.external_id),
                    "patient_id": str(invoice.patient.external_id),
                    "facility_id": str(invoice.facility.external_id),
                },
            }
        )
        return {
            "order_number": invoice.number or str(invoice.external_id),
            "payment_link_id": link.get("id"),
            "payment_url": link.get("short_url"),
        }
