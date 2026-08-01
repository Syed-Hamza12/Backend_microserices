import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .exceptions import FeatureNotOnPlan, UsageCapExceeded
from .models import Plan, Subscription, UsageCounter

logger = logging.getLogger(__name__)

#: Plan a brand-new business is put on, and for how long.
TRIAL_PLAN_NAME = "Premium"
TRIAL_DURATION_DAYS = 7


def start_trial(business):
    """Puts a newly created business on a time-limited trial.

    Nothing used to create a Subscription outside the test suite, so every real
    signup landed with no plan at all and `business_has_feature` returned False
    for everything. The app worked for customers and sales, then returned 403
    the moment the user opened AI chat, sent a bill on WhatsApp, or photographed
    a receipt — with no indication that a plan was the missing piece.

    Returns the Subscription, or None if one was not created. Deliberately
    never raises: a business that exists without a trial is recoverable by
    assigning a plan in the admin, whereas an exception here would fail the
    signup request and leave the user with no business at all.
    """
    if Subscription.objects.filter(business=business).exists():
        return None

    plan = Plan.objects.filter(name=TRIAL_PLAN_NAME).first()
    if plan is None:
        # Plans ship in migration 0002_seed_plans, so a missing one means it was
        # renamed or deleted after the fact rather than never created — worth
        # shouting about in the log, but not worth failing signup over.
        logger.error(
            "Trial plan %r not found; business %s created with no subscription.",
            TRIAL_PLAN_NAME, business.pk,
        )
        return None

    now = timezone.now()
    subscription = Subscription.objects.create(
        business=business,
        plan=plan,
        status="active",
        started_at=now,
        expires_at=now + timedelta(days=TRIAL_DURATION_DAYS),
        notes=f"Automatic {TRIAL_DURATION_DAYS}-day trial started at signup.",
    )
    logger.info(
        "Started %s-day %s trial for business %s (expires %s).",
        TRIAL_DURATION_DAYS, plan.name, business.pk, subscription.expires_at.isoformat(),
    )
    return subscription


def enforce_feature_gate(business, feature_key):
    """Raises FeatureNotOnPlan / UsageCapExceeded if this business can't use feature_key right
    now; otherwise increments the usage counter (if the plan has a cap) and returns silently.
    Call this before any Groq/Gemini call — never after (see claude_rule.md Section 2)."""
    subscription = Subscription.active_for(business)
    if subscription is None or not subscription.has_feature(feature_key):
        raise FeatureNotOnPlan()

    cap = subscription.plan.feature_cap(feature_key)
    if cap is None:
        return

    # localdate(), not now().date(): the quota month must follow the business's
    # calendar, or a plan resets five hours late every month.
    period_start = timezone.localdate().replace(day=1)

    # Claim a slot atomically. The previous read-then-save left a window where
    # several concurrent requests all read the same count, all saw room under
    # the cap, and all wrote count+1 — the counter advanced by one while
    # several paid AI calls went out. The conditional UPDATE lets exactly one
    # request per slot through, and a zero row count means the cap is full.
    with transaction.atomic():
        UsageCounter.objects.get_or_create(
            business=business, feature_key=feature_key, period_start=period_start
        )
        claimed = UsageCounter.objects.filter(
            business=business, feature_key=feature_key, period_start=period_start, count__lt=cap
        ).update(count=F("count") + 1)

    if not claimed:
        raise UsageCapExceeded()


def refund_feature_usage(business, feature_key):
    """Gives back a slot claimed by `enforce_feature_gate` when the work it was
    claimed for never happened (the provider was unreachable, the job died
    before calling out). Charging a business's monthly quota for a call that
    was never made is the kind of quiet unfairness that costs trust — and since
    the gate must claim up-front to stay race-free, the refund belongs here."""
    period_start = timezone.localdate().replace(day=1)
    UsageCounter.objects.filter(
        business=business, feature_key=feature_key, period_start=period_start, count__gt=0
    ).update(count=F("count") - 1)
