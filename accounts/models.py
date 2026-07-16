"""Пользователи, роли, сессии, 2FA (ТЗ §5)."""
import uuid

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone

from common.fields import EncryptedTextField


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Email обязателен")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("status", User.Status.ACTIVE)
        if extra.get("tenant_id") is None and extra.get("tenant") is None:
            from tenancy.models import Organization

            org, _ = Organization.objects.get_or_create(
                slug="default", defaults={"name": "Default Organization"}
            )
            extra["tenant"] = org
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    class Status(models.TextChoices):
        INVITED = "invited"
        ACTIVE = "active"
        SUSPENDED = "suspended"
        ARCHIVED = "archived"

    class WorkStatus(models.TextChoices):
        WORKING = "working"
        VACATION = "vacation"
        SICK_LEAVE = "sick_leave"
        DAY_OFF = "day_off"

    class Presence(models.TextChoices):
        ONLINE = "online"
        AWAY = "away"
        BUSY = "busy"
        OFFLINE = "offline"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenancy.Organization", on_delete=models.PROTECT, related_name="users")
    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INVITED)

    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    middle_name = models.CharField(max_length=100, blank=True)
    avatar = models.FileField(upload_to="avatars/", null=True, blank=True)

    position = models.CharField(max_length=150, blank=True)
    department = models.CharField(max_length=150, blank=True)
    manager = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="subordinates"
    )
    work_phone = models.CharField(max_length=32, blank=True)
    internal_phone = models.CharField(max_length=16, blank=True)
    telegram = models.CharField(max_length=64, blank=True)
    hired_at = models.DateField(null=True, blank=True)
    work_status = models.CharField(max_length=12, choices=WorkStatus.choices, default=WorkStatus.WORKING)
    presence = models.CharField(max_length=8, choices=Presence.choices, default=Presence.OFFLINE)
    timezone = models.CharField(max_length=63, default="Asia/Bishkek")
    language = models.CharField(max_length=8, default="ru")
    sla_response_minutes = models.PositiveIntegerField(null=True, blank=True)

    is_staff = models.BooleanField(default=False)  # доступ в Django admin
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        db_table = "accounts_user"
        indexes = [
            models.Index(fields=["tenant", "status"]),
            models.Index(fields=["tenant", "last_name", "first_name"]),
        ]

    def __str__(self) -> str:
        return self.get_full_name() or self.email

    def get_full_name(self) -> str:
        return " ".join(p for p in [self.last_name, self.first_name, self.middle_name] if p)

    @property
    def is_active(self) -> bool:  # используется Django auth
        return self.status == self.Status.ACTIVE


class Role(models.Model):
    """Роль в рамках tenant. permissions — строки-коды из каталога, хранятся в БД."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey("tenancy.Organization", on_delete=models.CASCADE, related_name="roles")
    code = models.SlugField(max_length=63)
    name = models.CharField(max_length=150)
    description = models.TextField(blank=True)
    is_system = models.BooleanField(default=False)  # системные роли нельзя удалять
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_role"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="uniq_role_code_per_tenant"),
        ]

    def __str__(self) -> str:
        return self.name


class RolePermission(models.Model):
    id = models.BigAutoField(primary_key=True)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="permissions")
    permission_code = models.CharField(max_length=100)

    class Meta:
        db_table = "accounts_role_permission"
        constraints = [
            models.UniqueConstraint(fields=["role", "permission_code"], name="uniq_role_permission"),
        ]


class UserRole(models.Model):
    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="user_roles")
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="user_roles")
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "accounts_user_role"
        constraints = [
            models.UniqueConstraint(fields=["user", "role"], name="uniq_user_role"),
        ]


class UserServiceAccess(models.Model):
    """Область прав по типу услуги (ТЗ §5.3): каким kind оператор может
    бронировать/выписывать/возвращать."""

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="service_access")
    service_kind = models.CharField(max_length=32)  # avia/rail/hotel/...
    allowed_actions = models.JSONField(default=list)  # ["search","book","issue",...]

    class Meta:
        db_table = "accounts_user_service_access"
        constraints = [
            models.UniqueConstraint(fields=["user", "service_kind"], name="uniq_user_service_kind"),
        ]


class UserPreference(models.Model):
    user = models.OneToOneField(User, primary_key=True, on_delete=models.CASCADE,
                                related_name="preferences")
    theme = models.CharField(max_length=16, default="light")
    date_format = models.CharField(max_length=16, default="DD.MM.YYYY")
    time_format = models.CharField(max_length=8, default="24h")
    base_currency = models.CharField(max_length=3, default="USD")
    language = models.CharField(max_length=8, default="ru")
    page_size = models.PositiveSmallIntegerField(default=25)
    start_page = models.CharField(max_length=64, default="dashboard")
    notification_channels = models.JSONField(default=dict, blank=True)
    notification_categories = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "accounts_user_preference"


class UserSession(models.Model):
    """Активная сессия (refresh token) пользователя (ТЗ §5.1)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sessions")
    refresh_jti = models.CharField(max_length=64, unique=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_user_session"
        indexes = [models.Index(fields=["user", "-last_seen_at"])]

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and self.expires_at > timezone.now()

    def revoke(self) -> None:
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])


class TwoFactorConfig(models.Model):
    user = models.OneToOneField(User, primary_key=True, on_delete=models.CASCADE,
                                related_name="two_factor")
    totp_secret = EncryptedTextField()
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_two_factor"

    @property
    def is_enabled(self) -> bool:
        return self.confirmed_at is not None


class PasswordResetToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_password_reset_token"


class FailedLoginAttempt(models.Model):
    """Счётчик неуспешных входов для блокировки brute force (без Redis)."""

    id = models.BigAutoField(primary_key=True)
    identifier = models.CharField(max_length=255)  # email или ip
    attempted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_failed_login"
        indexes = [models.Index(fields=["identifier", "-attempted_at"])]
