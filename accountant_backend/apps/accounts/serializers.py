from django.contrib.auth import authenticate
from rest_framework import serializers

from .models import Business, User


class EmailRegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def create(self, validated_data):
        email = validated_data["email"]
        user = User.objects.create_user(
            username=email,
            email=email,
            password=validated_data["password"],
            auth_provider="email",
        )
        return user


class EmailLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = authenticate(username=attrs["email"], password=attrs["password"])
        if user is None:
            raise serializers.ValidationError("Invalid email or password.")
        attrs["user"] = user
        return attrs


class BusinessSerializer(serializers.ModelSerializer):
    # Whether a logo exists, so the app knows to fetch it from
    # /business/logo/file/ rather than trying to load `logo_url` directly —
    # that path is only served while DEBUG is on.
    has_logo = serializers.SerializerMethodField()

    def get_has_logo(self, business):
        return bool(business.logo_url)

    class Meta:
        model = Business
        fields = [
            "id",
            "business_name",
            "business_category",
            "business_type",
            "special_instructions",
            "currency_code",
            "logo_url",
            "has_logo",
            "language",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
