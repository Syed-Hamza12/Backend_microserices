from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path("auth/email-register/", views.EmailRegisterView.as_view(), name="email-register"),
    path("auth/email-login/", views.EmailLoginView.as_view(), name="email-login"),
    path("auth/google/", views.GoogleAuthView.as_view(), name="google-auth"),
    path("auth/logout/", views.LogoutView.as_view(), name="logout"),
    # ROTATE_REFRESH_TOKENS=True (settings.py) — every refresh call returns a
    # new refresh token too and blacklists the old one, so the mobile app
    # must persist BOTH tokens from this response, not just the access one.
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("business/profile/", views.BusinessProfileView.as_view(), name="business-profile"),
    path("business/logo/", views.BusinessLogoUploadView.as_view(), name="business-logo-upload"),
    # Serves the logo bytes. Django doesn't serve MEDIA_URL with DEBUG off, so
    # this is how a logo reaches the app in production without nginx.
    path("business/logo/file/", views.BusinessLogoFileView.as_view(), name="business-logo-file"),
]
