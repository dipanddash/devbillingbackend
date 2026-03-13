from rest_framework import serializers
from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework_simplejwt.exceptions import AuthenticationFailed, TokenError
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from .models import Customer, StaffReportAccess, User
from .models import StaffSessionLog
from inventory.models import ManualClosing


class LoginSerializer(serializers.Serializer):

    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        today = timezone.localdate()

        user_obj = User.objects.filter(username=username).first()
        user_role = (getattr(user_obj, "role", "") or "").upper() if user_obj else ""
        if user_obj and user_obj.check_password(password):
            if user_role in ("STAFF", "SNOOKER_STAFF") and not user_obj.is_active:
                if user_obj.day_locked_on and user_obj.day_locked_on < today:
                    user_obj.is_active = True
                    user_obj.day_locked_on = None
                    user_obj.save(update_fields=["is_active", "day_locked_on"])
                elif user_obj.day_locked_on == today:
                    raise serializers.ValidationError("Can't login for the day. Ask admin to activate your account.")
                else:
                    raise serializers.ValidationError("Account is inactive. Contact admin.")
            elif not user_obj.is_active:
                raise serializers.ValidationError("Account is inactive. Contact admin.")

        user = authenticate(
            username=username,
            password=password
        )

        if not user:
            raise serializers.ValidationError("Invalid username or password")

        # Cache credentials for offline login
        try:
            from sync.offline_auth import cache_user_credentials
            cache_user_credentials(user)
        except Exception:
            pass  # Non-critical — don't block login

        data["user"] = user
        return data


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):

    def validate(self, attrs):
        username = str(attrs.get(self.username_field, "")).strip()
        password = str(attrs.get("password", ""))
        today = timezone.localdate()
        user_obj = User.objects.filter(**{self.username_field: username}).first()
        user_role = (getattr(user_obj, "role", "") or "").upper() if user_obj else ""
        if user_obj and user_obj.check_password(password):
            if user_role in ("STAFF", "SNOOKER_STAFF") and not user_obj.is_active:
                if user_obj.day_locked_on and user_obj.day_locked_on < today:
                    user_obj.is_active = True
                    user_obj.day_locked_on = None
                    user_obj.save(update_fields=["is_active", "day_locked_on"])
                elif user_obj.day_locked_on == today:
                    raise serializers.ValidationError("Can't login for the day. Ask admin to activate your account.")
                else:
                    raise serializers.ValidationError("Account is inactive. Contact admin.")
            elif not user_obj.is_active:
                raise serializers.ValidationError("Account is inactive. Contact admin.")

        data = super().validate(attrs)

        current_role = "ADMIN" if self.user.is_superuser else (getattr(self.user, "role", "") or "").upper()
        if current_role == "STAFF":
            already_closed = ManualClosing.objects.filter(
                entered_by=self.user,
                date=today,
                physical_quantity__gt=0,
            ).exists()
            if already_closed:
                raise serializers.ValidationError(
                    "Closing stock already submitted for today. Login is allowed from next day."
                )

        if current_role in ("STAFF", "SNOOKER_STAFF"):
            StaffSessionLog.objects.filter(
                user=self.user,
                logout_at__isnull=True
            ).update(logout_at=timezone.now())
            StaffSessionLog.objects.create(
                user=self.user,
                source=StaffSessionLog.SOURCE_SYSTEM_LOGIN,
            )

        # Cache credentials for offline login
        try:
            from sync.offline_auth import cache_user_credentials
            cache_user_credentials(self.user)
        except Exception:
            pass  # Non-critical — don't block login

        data["id"] = str(self.user.id)
        data["username"] = self.user.username
        data["role"] = current_role
        return data


class CustomTokenRefreshSerializer(TokenRefreshSerializer):
    """
    Safe refresh serializer that never throws User.DoesNotExist as 500.
    Returns a standard token error when user is missing/inactive.
    """

    def validate(self, attrs):
        refresh = self.token_class(attrs["refresh"])

        user_id = refresh.payload.get(api_settings.USER_ID_CLAIM, None)
        if user_id:
            user = get_user_model().objects.filter(
                **{api_settings.USER_ID_FIELD: user_id}
            ).first()
            if user is None:
                raise TokenError(self.error_messages["no_active_account"])
            if not api_settings.USER_AUTHENTICATION_RULE(user):
                raise AuthenticationFailed(
                    self.error_messages["no_active_account"],
                    "no_active_account",
                )

        data = {"access": str(refresh.access_token)}

        if api_settings.ROTATE_REFRESH_TOKENS:
            if api_settings.BLACKLIST_AFTER_ROTATION:
                try:
                    refresh.blacklist()
                except AttributeError:
                    pass

            refresh.set_jti()
            refresh.set_exp()
            refresh.set_iat()
            refresh.outstand()

            data["refresh"] = str(refresh)

        return data


class CustomerSerializer(serializers.ModelSerializer):
    order_count = serializers.IntegerField(read_only=True)
    visit_count = serializers.IntegerField(read_only=True)
    total_spent = serializers.FloatField(read_only=True)
    last_visit_at = serializers.DateTimeField(read_only=True)

    class Meta:
        model = Customer
        fields = [
            "id",
            "name",
            "phone",
            "created_at",
            "order_count",
            "visit_count",
            "total_spent",
            "last_visit_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
            "order_count",
            "visit_count",
            "total_spent",
            "last_visit_at",
        ]


class StaffUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "is_active",
            "password",
        ]
        read_only_fields = ["id"]

    def validate_role(self, value):
        if value not in ("STAFF", "SNOOKER_STAFF"):
            raise serializers.ValidationError("Only STAFF or SNOOKER_STAFF roles are allowed here.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class AdminUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "is_active",
            "password",
        ]
        read_only_fields = ["id"]

    def validate_role(self, value):
        if value != "ADMIN":
            raise serializers.ValidationError("Only ADMIN role is allowed here.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


class MeProfileSerializer(serializers.ModelSerializer):
    name = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=False)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "name",
            "email",
            "phone",
            "role",
            "is_active",
            "last_login",
            "date_joined",
            "password",
        ]
        read_only_fields = [
            "id",
            "username",
            "role",
            "is_active",
            "last_login",
            "date_joined",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        full_name = f"{instance.first_name} {instance.last_name}".strip()
        data["name"] = full_name or instance.username
        data["role"] = "ADMIN" if instance.is_superuser else (instance.role or "").upper()
        data.pop("password", None)
        return data

    def update(self, instance, validated_data):
        name = validated_data.pop("name", None)
        password = validated_data.pop("password", None)

        if name is not None:
            parts = name.strip().split(" ", 1)
            instance.first_name = parts[0] if parts and parts[0] else ""
            instance.last_name = parts[1] if len(parts) > 1 else ""

        for key, value in validated_data.items():
            setattr(instance, key, value)

        if password:
            instance.set_password(password)

        instance.save()
        return instance


class StaffReportAccessSerializer(serializers.ModelSerializer):
    staff_user_id = serializers.UUIDField(source="staff_user.id", read_only=True)
    username = serializers.CharField(source="staff_user.username", read_only=True)
    allowed_reports = serializers.ListField(
        child=serializers.CharField(),
        allow_empty=True,
    )

    class Meta:
        model = StaffReportAccess
        fields = [
            "staff_user_id",
            "username",
            "allowed_reports",
            "updated_at",
        ]


class SnookerStaffUserSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "first_name",
            "last_name",
            "email",
            "phone",
            "role",
            "is_active",
            "password",
        ]
        read_only_fields = ["id"]

    def validate_role(self, value):
        if value != "SNOOKER_STAFF":
            raise serializers.ValidationError("Only SNOOKER_STAFF role is allowed here.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance
        read_only_fields = ["staff_user_id", "username", "updated_at"]
