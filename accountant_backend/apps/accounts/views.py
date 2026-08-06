import mimetypes
from pathlib import Path

from django.conf import settings
from django.http import FileResponse
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token as google_id_token
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.billing.services import start_trial

from .models import Business, User
from .serializers import BusinessSerializer, EmailLoginSerializer, EmailRegisterSerializer
from .throttling import AuthRateThrottle
from .uploads import save_validated_image


def _tokens_for_user(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


class EmailRegisterView(APIView):
    permission_classes = [AllowAny]
    # Tighter than the global 'anon' rate — this is the endpoint someone
    # would actually script against (mass account creation, credential
    # stuffing setup), not just an occasional unauthenticated GET.
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        serializer = EmailRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        return Response(
            {"success": True, "data": {"tokens": _tokens_for_user(user), "hasBusiness": False}},
            status=status.HTTP_201_CREATED,
        )


class EmailLoginView(APIView):
    permission_classes = [AllowAny]
    # Brute-force protection: caps guesses per IP regardless of how many
    # different accounts are being tried against.
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        serializer = EmailLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        has_business = Business.objects.filter(owner=user).exists()
        return Response(
            {"success": True, "data": {"tokens": _tokens_for_user(user), "hasBusiness": has_business}}
        )


class GoogleAuthView(APIView):
    """
    Verifies the ID token the phone got from the native Google Sign-In SDK.
    `settings.GOOGLE_OAUTH_CLIENT_ID` must be the **Web application** OAuth
    Client ID (not the Android/iOS one) — the Flutter app is configured with
    this same ID as `GoogleSignIn`'s `serverClientId`, which is what makes
    the ID token's `aud` claim match what we verify against here. Using the
    Android/iOS client ID here would make every token fail verification.
    """

    permission_classes = [AllowAny]
    throttle_classes = [AuthRateThrottle]

    def post(self, request):
        raw_token = request.data.get("idToken")
        if not raw_token:
            return Response(
                {"success": False, "error": {"code": "VALIDATION_ERROR", "message": "idToken is required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not settings.GOOGLE_OAUTH_CLIENT_ID:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "NOT_IMPLEMENTED",
                        "message": "Google sign-in is not configured yet on this server.",
                    },
                },
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        try:
            payload = google_id_token.verify_oauth2_token(
                raw_token, google_auth_requests.Request(), settings.GOOGLE_OAUTH_CLIENT_ID
            )
        except (ValueError, GoogleAuthError):
            return Response(
                {
                    "success": False,
                    "error": {"code": "INVALID_TOKEN", "message": "Invalid or expired Google sign-in token."},
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        google_sub = payload.get("sub")
        email = payload.get("email")
        if not google_sub or not email:
            return Response(
                {
                    "success": False,
                    "error": {
                        "code": "INVALID_TOKEN",
                        "message": "Google account did not provide the required profile info.",
                    },
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(google_sub=google_sub).first()
        if user is None:
            # Same email previously registered via email/password — link
            # this Google identity to that existing account instead of
            # creating a duplicate user for the same person.
            #
            # Only ever for a Google-verified address. Linking on an unverified
            # email means whoever can get a token carrying that address takes
            # over the existing account, password and all — the email claim
            # alone is not proof of ownership.
            if not payload.get("email_verified"):
                return Response(
                    {
                        "success": False,
                        "error": {
                            "code": "EMAIL_NOT_VERIFIED",
                            "message": "Your Google account's email address isn't verified.",
                        },
                    },
                    status=status.HTTP_403_FORBIDDEN,
                )
            user = User.objects.filter(email__iexact=email).first()
            if user is not None:
                user.google_sub = google_sub
                user.save(update_fields=["google_sub"])
            else:
                user = User.objects.create_user(
                    username=email,
                    email=email,
                    auth_provider="google",
                    google_sub=google_sub,
                )
                user.set_unusable_password()
                user.save(update_fields=["password"])

        has_business = Business.objects.filter(owner=user).exists()
        return Response(
            {"success": True, "data": {"tokens": _tokens_for_user(user), "hasBusiness": has_business}}
        )


class LogoutView(APIView):
    def post(self, request):
        refresh_token = request.data.get("refresh")
        if refresh_token:
            try:
                RefreshToken(refresh_token).blacklist()
            except Exception:
                pass
        return Response({"success": True, "data": {}})


class BusinessProfileView(APIView):
    def get(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"success": True, "data": BusinessSerializer(business).data})

    def post(self, request):
        if Business.objects.filter(owner=request.user).exists():
            return Response(
                {"success": False, "error": {"code": "BUSINESS_EXISTS", "message": "Business already created."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = BusinessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        business = serializer.save(owner=request.user)
        # Without this the business exists on no plan at all, and every
        # feature-gated endpoint (AI chat, WhatsApp send, receipt OCR) answers
        # 403 from the user's very first tap. start_trial never raises, so a
        # billing problem cannot cost the user the business they just created.
        start_trial(business)
        return Response(
            {"success": True, "data": BusinessSerializer(business).data}, status=status.HTTP_201_CREATED
        )

    def patch(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )
        serializer = BusinessSerializer(business, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"success": True, "data": serializer.data})


class BusinessTypeChoicesView(APIView):
    """The fixed list of business types the domain-knowledge layer
    (apps.chat.domain_knowledge) has a matching code for — the single
    source of truth the mobile app's search/select picker reads from at
    signup and in Settings, instead of hardcoding its own copy that could
    drift out of sync with what the backend actually recognises."""

    def get(self, request):
        return Response(
            {
                "success": True,
                "data": [
                    {"code": code, "label": label}
                    for code, label in Business.BUSINESS_TYPE_CHOICES
                ],
            }
        )


class BusinessLogoUploadView(APIView):
    """Real logo upload (BACKEND_INTEGRATION_GUIDE.md's flagged gap — the
    mock UI's "Add Logo" toggle never actually uploaded anything). Mirrors
    `apps.image_info_extractor.views.UploadChatImageView`'s file-handling
    pattern: save under MEDIA_ROOT, point `logo_url` at the saved file."""

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        image = request.FILES.get("logo")
        if not image:
            return Response(
                {"success": False, "error": {"code": "VALIDATION_ERROR", "message": "logo file is required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        media_path = save_validated_image(image, subdirectory="business_logos")

        business.logo_url = request.build_absolute_uri(media_path)
        business.save(update_fields=["logo_url"])
        return Response({"success": True, "data": BusinessSerializer(business).data})


class BusinessLogoFileView(APIView):
    """Serves the business logo through the API, authenticated.

    Django only serves MEDIA_URL when DEBUG is on, so in production every
    /media/ URL 404s — logos included. Rather than require nginx or a storage
    bucket just to show one small image, the file is streamed from here.

    It is also the tenant-safe answer: a public /media/ path is guessable and
    unauthenticated, whereas this can only ever return the logo belonging to
    the caller's own business.
    """

    def get(self, request):
        try:
            business = request.user.business
        except Business.DoesNotExist:
            return Response(
                {"success": False, "error": {"code": "NO_BUSINESS", "message": "No business created yet."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not business.logo_url:
            return Response(
                {"success": False, "error": {"code": "NO_LOGO", "message": "No logo uploaded."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        relative = business.logo_url.split(settings.MEDIA_URL, 1)[-1]
        media_root = Path(settings.MEDIA_ROOT).resolve()
        path = (media_root / relative).resolve()

        # Containment check: `logo_url` is a stored value, and joining an
        # unchecked relative path onto MEDIA_ROOT is how a traversal turns into
        # serving an arbitrary file off the server.
        if not path.is_relative_to(media_root) or not path.is_file():
            return Response(
                {"success": False, "error": {"code": "NO_LOGO", "message": "Logo file is missing."}},
                status=status.HTTP_404_NOT_FOUND,
            )

        content_type = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        return FileResponse(path.open("rb"), content_type=content_type)
