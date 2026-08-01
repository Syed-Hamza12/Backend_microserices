from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.accounts.models import Business
from apps.billing.models import Plan, Subscription
from apps.billing.services import TRIAL_DURATION_DAYS, TRIAL_PLAN_NAME, start_trial

User = get_user_model()


class SignupTrialTests(APITestCase):
    """Nothing outside the test suite created a Subscription, so every real
    signup landed on no plan and 403'd the first time the user opened AI chat,
    sent a bill on WhatsApp, or photographed a receipt."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="trial", email="trial@x.com", password="pw"
        )
        self.client.force_authenticate(user=self.user)

    def test_creating_a_business_starts_a_trial_with_the_gated_features_on(self):
        response = self.client.post(
            "/api/business/profile/", {"business_name": "Hamza Traders"}, format="json"
        )
        self.assertEqual(response.status_code, 201)

        business = Business.objects.get(owner=self.user)
        subscription = Subscription.active_for(business)
        self.assertIsNotNone(subscription, "signup left the business with no subscription")
        self.assertEqual(subscription.plan.name, TRIAL_PLAN_NAME)
        self.assertEqual(subscription.status, "active")

        # The point of the trial is that the gated endpoints stop returning 403.
        for feature_key in ("ai_chat", "whatsapp_send", "image_extraction"):
            self.assertTrue(
                Subscription.business_has_feature(business, feature_key),
                f"{feature_key} still gated during the trial",
            )

    def test_trial_expires_after_the_configured_window(self):
        business = Business.objects.create(owner=self.user, business_name="Hamza Traders")
        subscription = start_trial(business)

        expected = timezone.now() + timedelta(days=TRIAL_DURATION_DAYS)
        self.assertAlmostEqual(
            subscription.expires_at, expected, delta=timedelta(minutes=1)
        )

        # An expires_at in the past must switch the features off even though
        # `status` is still "active" and no sweep command has run.
        subscription.expires_at = timezone.now() - timedelta(seconds=1)
        subscription.save(update_fields=["expires_at"])
        self.assertIsNone(Subscription.active_for(business))
        self.assertFalse(Subscription.business_has_feature(business, "ai_chat"))

    def test_start_trial_is_idempotent(self):
        business = Business.objects.create(owner=self.user, business_name="Hamza Traders")
        start_trial(business)
        self.assertIsNone(start_trial(business), "a second call created another subscription")
        self.assertEqual(Subscription.objects.filter(business=business).count(), 1)

    def test_a_missing_trial_plan_does_not_break_signup(self):
        """Plans ship in migration 0002_seed_plans, but if one is renamed or
        deleted the user must still get their business — losing it to a billing
        misconfiguration is far worse than starting without a trial."""
        Subscription.objects.all().delete()
        Plan.objects.all().delete()

        response = self.client.post(
            "/api/business/profile/", {"business_name": "Hamza Traders"}, format="json"
        )
        self.assertEqual(response.status_code, 201)
        business = Business.objects.get(owner=self.user)
        self.assertIsNone(Subscription.active_for(business))
