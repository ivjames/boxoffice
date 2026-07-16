from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models

from tenants.models import Organization


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Users must have an email address.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)

        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")

        return self._create_user(email, password, **extra_fields)


class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user: login is by email, there is no username. Tenant membership
    (which theater(s) a user belongs to, and with what role) lives on
    Membership below — a single User can belong to multiple Organizations
    (e.g. platform staff, or someone who works box office at two venues).
    """

    email = models.EmailField(unique=True)
    first_name = models.CharField(max_length=150, blank=True)
    last_name = models.CharField(max_length=150, blank=True)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)

    date_joined = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        ordering = ["email"]

    def __str__(self):
        return self.email

    def get_full_name(self):
        full_name = f"{self.first_name} {self.last_name}".strip()
        return full_name or self.email

    def get_short_name(self):
        return self.first_name or self.email


class Membership(models.Model):
    """A user's role within one Organization (theater)."""

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        MANAGER = "manager", "Manager"
        BOX_OFFICE = "box_office", "Box office"
        SCANNER = "scanner", "Scanner"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="memberships"
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=20, choices=Role.choices)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"], name="unique_membership_per_org"
            )
        ]
        indexes = [models.Index(fields=["organization", "role"])]

    def __str__(self):
        return f"{self.user} @ {self.organization} ({self.role})"

    # --- role permission helpers ---------------------------------------
    # Roles are cumulative: owner > manager > box_office > scanner. Each
    # helper answers "can this membership do X", so callers don't need to
    # know the ordering / spell out role names themselves.

    def is_owner(self):
        return self.role == self.Role.OWNER

    def is_manager_or_above(self):
        return self.role in (self.Role.OWNER, self.Role.MANAGER)

    def is_box_office_or_above(self):
        return self.role in (self.Role.OWNER, self.Role.MANAGER, self.Role.BOX_OFFICE)

    def can_scan(self):
        # Every role can work the door, not just dedicated scanners.
        return self.role in (
            self.Role.OWNER,
            self.Role.MANAGER,
            self.Role.BOX_OFFICE,
            self.Role.SCANNER,
        )

    def can_manage_events(self):
        return self.is_manager_or_above()

    def can_manage_billing(self):
        return self.is_owner()

    def can_sell_tickets(self):
        return self.is_box_office_or_above()

    def can_manage_team(self):
        # Managers and owners can view the team and add/adjust staff, but
        # granting or changing the OWNER role is gated to owners themselves
        # (enforced in dashboard.views' team handlers) -- a manager can't
        # promote anyone (including themselves) to owner.
        return self.is_manager_or_above()

    def assignable_roles(self):
        """Role values this membership is allowed to grant. Only owners can
        hand out (or move someone into/out of) the owner role."""
        roles = [self.Role.MANAGER, self.Role.BOX_OFFICE, self.Role.SCANNER]
        if self.is_owner():
            roles = [self.Role.OWNER, *roles]
        return [str(r) for r in roles]

    @classmethod
    def owner_count(cls, organization):
        """How many owners `organization` has -- used to protect the
        last-owner invariant (an org must never be left with zero owners)."""
        return cls.objects.filter(
            organization=organization, role=cls.Role.OWNER
        ).count()
