from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.billing.models import Subscription


class Command(BaseCommand):
    help = (
        "Marks active subscriptions whose expires_at has passed as 'expired'. "
        "Run daily (cron/Task Scheduler)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be expired without changing anything.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        due = Subscription.objects.filter(status="active", expires_at__isnull=False, expires_at__lte=now)

        if options["dry_run"]:
            for subscription in due.select_related("business", "plan"):
                self.stdout.write(
                    f"would expire: {subscription.business.business_name} "
                    f"({subscription.plan.name}, expired {subscription.expires_at:%Y-%m-%d})"
                )
            self.stdout.write(self.style.WARNING(f"Dry run — {due.count()} subscription(s) would be expired."))
            return

        count = due.update(status="expired")
        self.stdout.write(self.style.SUCCESS(f"Expired {count} subscription(s)."))
