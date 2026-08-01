"""Renames the terminal success status from `sent` to `accepted`.

The choices change in 0004 only alters validation; existing rows keep whatever
string they were saved with. Without this, deliveries recorded before the rename
would sit on a status the model no longer recognises — invisible in filtered
views and unlabelled in the app.

The rename itself is a truthfulness fix: Baileys returns once WhatsApp has
*accepted* a message and exposes no delivery receipt we act on, so "sent" was
claiming more than the system can observe.
"""

from django.db import migrations


def sent_to_accepted(apps, schema_editor):
    DocumentDelivery = apps.get_model("documents", "DocumentDelivery")
    DocumentDelivery.objects.filter(status="sent").update(status="accepted")


def accepted_to_sent(apps, schema_editor):
    DocumentDelivery = apps.get_model("documents", "DocumentDelivery")
    DocumentDelivery.objects.filter(status="accepted").update(status="sent")


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0004_rename_sent_at_documentdelivery_accepted_at_and_more"),
    ]

    operations = [
        migrations.RunPython(sent_to_accepted, accepted_to_sent),
    ]
