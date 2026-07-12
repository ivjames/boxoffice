from django.conf import settings
from django.db import models


class OAuthIdentity(models.Model):
    """A staff accounts.User's link to one external identity (a Google/Facebook
    account they sign in with).

    This exists only for STAFF (accounts.User). Guests are already identified
    purely by their verified email (guests.GuestAccount is email-keyed and
    passwordless), so an OAuth guest login just resolves to the GuestAccount
    for that email -- there's nothing extra to store. Staff, by contrast, are
    real User rows with roles/Memberships, and we want a stable link that
    survives the person later changing the email on their Google account: the
    provider's opaque `uid` (Google `sub`, Facebook `id`) is that anchor, so a
    match on (provider, uid) finds the same User regardless of email drift.

    A User can have several identities (one per provider), and each external
    identity maps to exactly one User -- hence unique (provider, uid). This is
    a global link, NOT tenant-scoped: a User can staff several theaters, but
    it's the same human/Google account signing in; which tenants they may
    actually reach is still decided per-request by Membership (see
    accounts.permissions), never by the mere existence of this row.
    """

    class Provider(models.TextChoices):
        GOOGLE = "google", "Google"
        FACEBOOK = "facebook", "Facebook"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="oauth_identities",
    )
    provider = models.CharField(max_length=32, choices=Provider.choices)
    # The provider's stable subject id for this user. Opaque; never reused.
    uid = models.CharField(max_length=255)
    # The email the provider last reported for this identity -- stored for the
    # admin's benefit / debugging only. Account matching keys on (provider,
    # uid) and on the User's own email, never on this column.
    email = models.EmailField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "uid"], name="unique_provider_identity"
            )
        ]
        indexes = [models.Index(fields=["provider", "uid"])]
        verbose_name = "OAuth identity"
        verbose_name_plural = "OAuth identities"

    def __str__(self):
        return f"{self.get_provider_display()} → {self.user_id}"
