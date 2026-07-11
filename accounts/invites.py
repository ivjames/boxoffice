"""Adding a staff member to an Organization.

Staff auth is password-based (accounts.User), so "invite a teammate" has two
shapes, both handled by `add_member` below:

  * The email already belongs to a User (they may already work another
    theater's box office, or this one previously). We just create the
    Membership and send a short "you've been added" note -- they sign in with
    their existing password.

  * The email is new. We create a User with an *unusable* password and email a
    one-time set-password link (Django's signed uid+token, same machinery as
    password reset). The account can't be signed into until that link is used,
    so a half-finished invite is never a live credential.

The link is built from the in-flight admin request, so it lands on the same
tenant subdomain the inviter was on -- no host hardcoded here.
"""

from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import Membership, User


class MemberExistsError(Exception):
    """The email already has a Membership in this organization."""


def add_member(*, organization, email, role, first_name="", last_name="", request):
    """Create (or reuse) the User for `email` and give them `role` in
    `organization`. Returns (membership, created_user, invite_sent).

    Raises MemberExistsError if they're already a member here.
    """
    email = User.objects.normalize_email(email).lower()
    user = User.objects.filter(email__iexact=email).first()

    if user and Membership.objects.filter(user=user, organization=organization).exists():
        raise MemberExistsError(email)

    created_user = False
    invite_sent = False
    if user is None:
        user = User(email=email, first_name=first_name, last_name=last_name)
        user.set_unusable_password()
        user.save()
        created_user = True

    membership = Membership.objects.create(
        user=user, organization=organization, role=role
    )

    if created_user:
        _send_invite_email(user, organization, request)
        invite_sent = True

    return membership, created_user, invite_sent


def _send_invite_email(user, organization, request):
    uidb64 = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    set_password_url = request.build_absolute_uri(
        reverse("set_password", args=[uidb64, token])
    )

    context = {
        "organization": organization,
        "set_password_url": set_password_url,
        "user": user,
    }
    subject = f"You've been added to {organization.name} on Boxo.show"
    text_body = render_to_string("accounts/email/invite.txt", context)
    html_body = render_to_string("accounts/email/invite.html", context)

    email = EmailMultiAlternatives(subject=subject, body=text_body, to=[user.email])
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)
