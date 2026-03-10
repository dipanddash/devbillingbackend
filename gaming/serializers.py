from rest_framework import serializers
from django.utils import timezone
from .models import (
    SnookerBoard, Console, GameSession, SessionItem,
    SessionPayment, SessionAuditLog,
)


class SnookerBoardSerializer(serializers.ModelSerializer):
    is_occupied = serializers.SerializerMethodField()

    class Meta:
        model = SnookerBoard
        fields = ["id", "number", "label", "is_active", "is_occupied"]

    def get_is_occupied(self, obj):
        return obj.sessions.filter(status="ACTIVE").exists()


class ConsoleSerializer(serializers.ModelSerializer):
    is_occupied = serializers.SerializerMethodField()

    class Meta:
        model = Console
        fields = ["id", "name", "console_type", "is_active", "is_occupied"]

    def get_is_occupied(self, obj):
        return obj.sessions.filter(status="ACTIVE").exists()


class SessionItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionItem
        fields = [
            "id", "session", "product", "item_name",
            "quantity", "unit_price", "total_price", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate(self, data):
        data["total_price"] = data["quantity"] * data["unit_price"]
        return data


class SessionPaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionPayment
        fields = ["id", "session", "method", "amount", "reference_id", "paid_at"]
        read_only_fields = ["id", "paid_at"]


class SessionAuditLogSerializer(serializers.ModelSerializer):
    changed_by_username = serializers.CharField(source="changed_by.username", read_only=True, default="")

    class Meta:
        model = SessionAuditLog
        fields = [
            "id", "session", "field_changed", "old_value", "new_value",
            "reason", "changed_by", "changed_by_username", "created_at",
        ]
        read_only_fields = ["id", "created_at"]


class GameSessionListSerializer(serializers.ModelSerializer):
    staff_username = serializers.CharField(source="staff.username", read_only=True, default="")
    board_numbers = serializers.SerializerMethodField()
    console_name = serializers.CharField(source="console.name", read_only=True, default="")
    items = SessionItemSerializer(many=True, read_only=True)
    payments = SessionPaymentSerializer(many=True, read_only=True)
    running_duration_minutes = serializers.SerializerMethodField()
    running_service_amount = serializers.SerializerMethodField()
    food_total = serializers.SerializerMethodField()
    running_total = serializers.SerializerMethodField()

    class Meta:
        model = GameSession
        fields = [
            "id", "customer_name", "customer_phone", "customer_email",
            "service_type", "status",
            "board_numbers", "price_per_board_per_hour",
            "console", "console_name", "console_type", "price_per_person_per_hour",
            "num_players", "check_in", "check_out",
            "discount_amount", "final_amount",
            "staff", "staff_username", "notes",
            "items", "payments",
            "running_duration_minutes", "running_service_amount",
            "food_total", "running_total",
        ]

    def get_board_numbers(self, obj):
        return list(obj.boards.values_list("number", flat=True))

    def get_running_duration_minutes(self, obj):
        end = obj.check_out or timezone.now()
        delta = end - obj.check_in
        return round(delta.total_seconds() / 60, 1)

    def get_running_service_amount(self, obj):
        end = obj.check_out or timezone.now()
        hours = (end - obj.check_in).total_seconds() / 3600
        if obj.service_type == "SNOOKER":
            board_count = obj.boards.count() or 1
            return round(float(obj.price_per_board_per_hour) * board_count * hours, 2)
        else:
            return round(float(obj.price_per_person_per_hour) * obj.num_players * hours, 2)

    def get_food_total(self, obj):
        return float(sum(item.total_price for item in obj.items.all()))

    def get_running_total(self, obj):
        service = self.get_running_service_amount(obj)
        food = self.get_food_total(obj)
        return round(service + food - float(obj.discount_amount), 2)


class GameSessionCreateSerializer(serializers.ModelSerializer):
    board_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, write_only=True
    )

    class Meta:
        model = GameSession
        fields = [
            "customer_name", "customer_phone", "customer_email",
            "service_type",
            "board_ids", "price_per_board_per_hour",
            "console", "console_type", "price_per_person_per_hour",
            "num_players", "notes",
        ]

    def validate(self, data):
        service_type = data.get("service_type")

        if service_type == "SNOOKER":
            board_ids = data.get("board_ids", [])
            if not board_ids:
                raise serializers.ValidationError({"board_ids": "At least one board must be selected."})
            # Check availability
            occupied = SnookerBoard.objects.filter(
                id__in=board_ids,
                sessions__status="ACTIVE",
            ).values_list("number", flat=True)
            if occupied:
                raise serializers.ValidationError(
                    {"board_ids": f"Board(s) {list(occupied)} already in use."}
                )
            if not data.get("price_per_board_per_hour"):
                raise serializers.ValidationError(
                    {"price_per_board_per_hour": "Price per board per hour is required."}
                )

        elif service_type == "CONSOLE":
            console = data.get("console")
            if not console:
                raise serializers.ValidationError({"console": "Console is required."})
            if console.sessions.filter(status="ACTIVE").exists():
                raise serializers.ValidationError(
                    {"console": f"Console '{console.name}' is already in use."}
                )
            if not data.get("price_per_person_per_hour"):
                raise serializers.ValidationError(
                    {"price_per_person_per_hour": "Price per person per hour is required."}
                )
            data["console_type"] = console.console_type

        return data

    def create(self, validated_data):
        board_ids = validated_data.pop("board_ids", [])
        session = GameSession.objects.create(**validated_data)
        if board_ids:
            session.boards.set(board_ids)
        return session


class CheckoutSerializer(serializers.Serializer):
    discount_amount = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, default=0)
    final_amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    payments = SessionPaymentSerializer(many=True, required=False)
