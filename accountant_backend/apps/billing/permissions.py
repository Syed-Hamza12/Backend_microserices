from rest_framework.permissions import BasePermission

from .exceptions import FeatureNotOnPlan
from .models import Subscription


class HasFeature(BasePermission):
    """Gate a view behind a plan feature. Set `required_feature = "ai_chat"` (etc.) on the
    view; views with no `required_feature` attribute pass through unaffected."""

    def has_permission(self, request, view):
        feature_key = getattr(view, "required_feature", None)
        if not feature_key:
            return True
        business = getattr(request.user, "business", None)
        if business is None or not Subscription.business_has_feature(business, feature_key):
            raise FeatureNotOnPlan()
        return True
