from rest_framework import status
from rest_framework.exceptions import APIException


class FeatureNotOnPlan(APIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = "This feature is not available on your current plan. Please upgrade to use it."
    error_code = "FEATURE_NOT_ON_PLAN"


class UsageCapExceeded(APIException):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_detail = "You have reached your monthly usage limit for this feature."
    error_code = "USAGE_CAP_EXCEEDED"
