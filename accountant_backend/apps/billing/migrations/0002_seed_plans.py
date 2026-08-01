from django.db import migrations


PLANS = [
    {"name": "Basic", "price_pkr": "400.00", "is_custom": False, "features": []},
    {"name": "Standard", "price_pkr": "800.00", "is_custom": False, "features": ["ai_chat"]},
    {
        "name": "Premium",
        "price_pkr": "1500.00",
        "is_custom": False,
        "features": ["ai_chat", "voice_reply", "image_extraction", "whatsapp_send"],
    },
    {"name": "Custom", "price_pkr": "0.00", "is_custom": True, "features": []},
]


def seed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    PlanFeature = apps.get_model("billing", "PlanFeature")
    for plan_data in PLANS:
        plan, _ = Plan.objects.get_or_create(
            name=plan_data["name"],
            defaults={"price_pkr": plan_data["price_pkr"], "is_custom": plan_data["is_custom"]},
        )
        for feature_key in plan_data["features"]:
            PlanFeature.objects.get_or_create(plan=plan, feature_key=feature_key, defaults={"enabled": True})


def unseed_plans(apps, schema_editor):
    Plan = apps.get_model("billing", "Plan")
    Plan.objects.filter(name__in=[p["name"] for p in PLANS]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_plans, unseed_plans),
    ]
