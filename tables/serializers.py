from rest_framework import serializers
from .models import Table, TableSession

class TableSerializer(serializers.ModelSerializer):
    token_number = serializers.SerializerMethodField()

    class Meta:
        model = Table
        fields = [
            "id",
            "number",
            "floor",
            "capacity",
            "status",
            "token_number",
        ]

    def get_token_number(self, obj):
        if obj.status != "OCCUPIED":
            return None

        active_sessions = getattr(obj, "active_sessions", None)
        if active_sessions is not None:
            for session in active_sessions:
                return session.token_number
            return None

        session = (
            TableSession.objects
            .filter(
                table=obj,
                is_active=True,
                closed_at__isnull=True,
                table__status="OCCUPIED",
            )
            .order_by("-created_at")
            .first()
        )
        return session.token_number if session else None

class TableSessionSerializer(serializers.ModelSerializer):
    guest_count = serializers.IntegerField(min_value=1, required=True)

    table_number = serializers.CharField(
        source="table.number",
        read_only=True
    )

    def validate(self, attrs):
        table = attrs.get("table")
        guest_count = attrs.get("guest_count")

        if table and guest_count and guest_count > table.capacity:
            raise serializers.ValidationError(
                {"guest_count": "Guest count cannot exceed table capacity."}
            )
        return attrs

    class Meta:
        model = TableSession
        fields = "__all__"
        read_only_fields = [
            "id",
            "token_number",
            "is_active",
            "created_at",
            "closed_at"
        ]
