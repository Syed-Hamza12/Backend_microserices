"""Recalculates balances for customers whose future-dated entries have matured.

`current_balance` is "what is owed today", and it is only ever written by
`recalculate_balances`, which runs on write. So a sale dated 15 August has no
effect on the balance when 15 August arrives — nothing writes that day. Without
this command a scheduled bill would sit in the ledger and silently never become
owed, which is worse than not supporting future dates at all.

Run daily, alongside `cleanup_artifacts` and `expire_subscriptions`:

    python manage.py apply_matured_entries
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.customers.models import Customer
from apps.sales.business_date import business_today
from apps.sales.models import ActivityEntry
from apps.sales.services import recalculate_balances


class Command(BaseCommand):
    help = "Recalculates balances for customers whose scheduled entries have come due."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
        parser.add_argument(
            "--lookback-days",
            type=int,
            default=2,
            help=(
                "How many days back to treat as possibly-matured. The default "
                "covers a missed run without recalculating the whole book."
            ),
        )

    def handle(self, *args, **options):
        today = business_today()
        # Anything dated from just before today onwards: entries maturing today,
        # plus a day of slack so a skipped run still gets picked up. Customers
        # with nothing scheduled are untouched, so this stays cheap.
        cutoff = today - timedelta(days=options["lookback_days"])

        customer_ids = (
            ActivityEntry.objects.filter(timestamp__date__gte=cutoff)
            .values_list("customer_id", flat=True)
            .distinct()
        )
        customers = list(Customer.objects.filter(id__in=list(customer_ids)))

        if options["dry_run"]:
            for customer in customers:
                self.stdout.write(
                    f"would recalculate {customer.name}: "
                    f"today={customer.current_balance} projected={customer.projected_balance}"
                )
            self.stdout.write(self.style.WARNING(f"Dry run — {len(customers)} customer(s) considered."))
            return

        changed = 0
        for customer in customers:
            before = customer.current_balance
            # Each customer in its own transaction: one failure must not roll
            # back balances already corrected for everyone else.
            with transaction.atomic():
                recalculate_balances(customer)
            customer.refresh_from_db()
            if customer.current_balance != before:
                changed += 1
                self.stdout.write(
                    f"{customer.name}: {before} -> {customer.current_balance} (matured entries applied)"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Checked {len(customers)} customer(s); {changed} balance(s) updated."
            )
        )
