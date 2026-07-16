from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.invites import MemberExistsError, add_member
from accounts.models import Membership
from accounts.permissions import manager_required

from ..forms import InviteMemberForm


# --- team / roles (manager+, owner-only for the owner role) ---------------


def _assignable_roles(membership):
    """Role values `membership` is allowed to grant. Only owners can hand out
    (or move someone into/out of) the owner role."""
    roles = [Membership.Role.MANAGER, Membership.Role.BOX_OFFICE, Membership.Role.SCANNER]
    if membership.is_owner():
        roles = [Membership.Role.OWNER, *roles]
    return [str(r) for r in roles]


def _owner_count(organization):
    return Membership.objects.filter(
        organization=organization, role=Membership.Role.OWNER
    ).count()


def _render_team(request, form=None):
    organization = request.organization
    memberships = (
        Membership.objects.filter(organization=organization)
        .select_related("user")
        .order_by("role", "user__email")
    )
    assignable = _assignable_roles(request.membership)
    if form is None:
        form = InviteMemberForm(allowed_roles=assignable)
    return render(
        request,
        "dashboard/team.html",
        {
            "memberships": memberships,
            "form": form,
            "assignable_roles": assignable,
            "role_choices": Membership.Role.choices,
            "my_membership_id": request.membership.id,
        },
    )


@manager_required
def team(request):
    """List staff and their roles. Any manager+ can view; mutations go through
    the POST handlers below, which re-check the owner-role gate server-side."""
    return _render_team(request)


@manager_required
@require_POST
def team_add(request):
    organization = request.organization
    assignable = _assignable_roles(request.membership)
    form = InviteMemberForm(request.POST, allowed_roles=assignable)
    if not form.is_valid():
        return _render_team(request, form=form)

    role = form.cleaned_data["role"]
    if role not in assignable:
        # Belt-and-suspenders: the form already limits choices to `assignable`,
        # but re-check so a hand-crafted POST can't grant a role above the
        # actor's own authority (e.g. a manager minting an owner).
        messages.error(request, "You can't assign that role.")
        return redirect("dashboard_team")

    try:
        _membership, created_user, invite_sent = add_member(
            organization=organization,
            email=form.cleaned_data["email"],
            role=role,
            first_name=form.cleaned_data["first_name"],
            last_name=form.cleaned_data["last_name"],
            request=request,
        )
    except MemberExistsError:
        form.add_error("email", "That person is already on this team.")
        return _render_team(request, form=form)

    if invite_sent:
        messages.success(
            request,
            f"Invited {form.cleaned_data['email']} — they've been emailed a link to set a password.",
        )
    else:
        messages.success(
            request,
            f"Added {form.cleaned_data['email']} to the team. They sign in with their existing password.",
        )
    return redirect("dashboard_team")


@manager_required
@require_POST
def team_update_role(request, pk):
    organization = request.organization
    actor = request.membership
    target = get_object_or_404(Membership, pk=pk, organization=organization)
    new_role = request.POST.get("role")

    if new_role not in Membership.Role.values:
        messages.error(request, "Unknown role.")
        return redirect("dashboard_team")

    if target.id == actor.id:
        messages.error(request, "You can't change your own role.")
        return redirect("dashboard_team")

    # Owner-role changes (promoting to owner, or demoting an existing owner)
    # are owner-only.
    touches_owner = target.is_owner() or new_role == Membership.Role.OWNER
    if touches_owner and not actor.is_owner():
        messages.error(request, "Only an owner can grant or change the owner role.")
        return redirect("dashboard_team")

    # Never leave the organization with no owner.
    if target.is_owner() and new_role != Membership.Role.OWNER and _owner_count(organization) <= 1:
        messages.error(request, "This is the only owner — promote someone else first.")
        return redirect("dashboard_team")

    if target.role != new_role:
        target.role = new_role
        target.save(update_fields=["role"])
        messages.success(request, f"Updated {target.user.email} to {target.get_role_display()}.")
    return redirect("dashboard_team")


@manager_required
@require_POST
def team_remove(request, pk):
    organization = request.organization
    actor = request.membership
    target = get_object_or_404(Membership, pk=pk, organization=organization)

    if target.id == actor.id:
        messages.error(request, "You can't remove yourself.")
        return redirect("dashboard_team")

    if target.is_owner() and not actor.is_owner():
        messages.error(request, "Only an owner can remove an owner.")
        return redirect("dashboard_team")

    if target.is_owner() and _owner_count(organization) <= 1:
        messages.error(request, "This is the only owner — you can't remove them.")
        return redirect("dashboard_team")

    email = target.user.email
    # Remove the Membership only, not the User: they may belong to other
    # organizations (accounts.User is global, membership is per-org).
    target.delete()
    messages.success(request, f"Removed {email} from the team.")
    return redirect("dashboard_team")
