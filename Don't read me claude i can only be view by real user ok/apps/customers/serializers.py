from rest_framework import serializers

from .models import Customer
from .phone import normalize_phone


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = [
            "id",
            "name",
            "phone",
            "address",
            "opening_balance",
            "current_balance",
            "projected_balance",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "current_balance", "projected_balance", "created_at", "updated_at"]

    def validate_phone(self, value):
        # A number saved in local dialing format (e.g. "03339233158") never
        # resolves as a WhatsApp recipient — Baileys just times out looking
        # for it, which every prior send to such a number silently was.
        # Normalizing here, once, at the point of entry, means every reader
        # of Customer.phone downstream (invoices, WhatsApp sends, display)
        # sees the same correct value instead of each needing its own fixup.
        return normalize_phone(value)

    def create(self, validated_data):
        validated_data["current_balance"] = validated_data.get("opening_balance", 0)
        validated_data["projected_balance"] = validated_data.get("opening_balance", 0)
        return super().create(validated_data)


class CustomerNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["note"]
