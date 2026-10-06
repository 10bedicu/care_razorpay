import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("care_razorpay", "0005_payment_integrity"),
        ("emr", "0080_alter_activitydefinition_category_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="RazorpayQRCode",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "external_id",
                    models.UUIDField(db_index=True, default=uuid.uuid4, unique=True),
                ),
                (
                    "created_date",
                    models.DateTimeField(auto_now_add=True, db_index=True, null=True),
                ),
                (
                    "modified_date",
                    models.DateTimeField(auto_now=True, db_index=True, null=True),
                ),
                ("deleted", models.BooleanField(db_index=True, default=False)),
                ("qr_id", models.CharField(max_length=255, unique=True)),
                (
                    "amount",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=14, null=True
                    ),
                ),
                ("usage", models.CharField(blank=True, default="", max_length=16)),
                ("image_url", models.TextField(blank=True, default="")),
                ("close_by", models.DateTimeField(blank=True, null=True)),
                (
                    "status",
                    models.CharField(
                        choices=[("active", "Active"), ("closed", "Closed")],
                        default="active",
                        max_length=16,
                    ),
                ),
                (
                    "amount_received",
                    models.DecimalField(decimal_places=2, default=0, max_digits=14),
                ),
                ("payments_count", models.PositiveIntegerField(default=0)),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="emr.invoice"
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
    ]
