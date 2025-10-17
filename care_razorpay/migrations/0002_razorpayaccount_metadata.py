from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("care_razorpay", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="razorpayaccount",
            name="metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
