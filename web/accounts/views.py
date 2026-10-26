from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts import services
from accounts.forms import GB, InviteForm, NotificationPreferencesForm, ProfileForm, QuotaForm
from accounts.models import NotificationPreferences, User
from accounts.tokens import is_pending_invite


def pending(request):
    """Shown after verifying an email, and to anyone who signs in before an
    admin has approved them. Deliberately says nothing account-specific: it's
    reachable without being signed in."""
    return render(request, "accounts/pending.html")


@login_required
def settings_view(request):
    prefs = NotificationPreferences.for_user(request.user)
    if request.method == "POST" and "save_profile" in request.POST:
        profile_form = ProfileForm(request.POST, instance=request.user)
        notify_form = NotificationPreferencesForm(instance=prefs)
        if profile_form.is_valid():
            profile_form.save()
            messages.success(request, "Profile saved.")
            return redirect("accounts:settings")
    elif request.method == "POST" and "save_notifications" in request.POST:
        profile_form = ProfileForm(instance=request.user)
        notify_form = NotificationPreferencesForm(request.POST, instance=prefs)
        if notify_form.is_valid():
            notify_form.save()
            messages.success(request, "Notification preferences saved.")
            return redirect("accounts:settings")
    else:
        profile_form = ProfileForm(instance=request.user)
        notify_form = NotificationPreferencesForm(instance=prefs)

    used = request.user.storage_used_bytes()
    quota = request.user.storage_quota_bytes
    return render(request, "accounts/settings.html", {
        "profile_form": profile_form,
        "notify_form": notify_form,
        "storage_used": used,
        "storage_quota": quota,
        "storage_pct": min(100, round(used / quota * 100, 1)) if quota else 0,
        "devices": request.user.known_devices.order_by("-last_seen"),
    })


# --------------------------------------------------------------------------- #
# Admin user portal (/manage/users/). AccountGateMiddleware already restricts
# /manage/ to staff; the explicit check below is belt-and-braces.
# --------------------------------------------------------------------------- #
def _staff_only(request) -> None:
    if not request.user.is_staff:
        raise PermissionDenied


@login_required
def manage_users(request):
    _staff_only(request)
    rows = [
        {
            "user": u,
            "state": u.account_state(),
            "has_mfa": u.has_mfa(),
            "used": u.storage_used_bytes(),
            "pending_invite": is_pending_invite(u),
        }
        for u in User.objects.order_by("approved_at", "email")
    ]
    pending_rows = [r for r in rows if r["state"] in ("pending_approval", "pending_verification")]
    other_rows = [r for r in rows if r not in pending_rows]
    return render(request, "accounts/manage_users.html", {
        "pending_rows": pending_rows,
        "rows": other_rows,
        "invite_form": InviteForm(initial={"quota_gb": User._meta.get_field("storage_quota_bytes")
                                           .get_default() // GB}),
    })


@login_required
@require_POST
def manage_invite(request):
    _staff_only(request)
    form = InviteForm(request.POST)
    if form.is_valid():
        user = services.invite(
            form.cleaned_data["email"], form.cleaned_data["display_name"].strip(),
            form.cleaned_data["quota_gb"] * GB, by=request.user)
        messages.success(request, f"Invitation sent to {user.email}.")
    else:
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
    return redirect("accounts:manage_users")


@login_required
@require_POST
def manage_user_action(request, user_id: int, action: str):
    _staff_only(request)
    target = get_object_or_404(User, pk=user_id)
    if target == request.user and action in ("disable", "reset_mfa"):
        messages.error(request, "You can't do that to your own account from here.")
        return redirect("accounts:manage_users")

    if action == "approve" and not target.is_approved:
        services.approve(target, by=request.user)
        messages.success(request, f"Approved {target.email}. They've been emailed.")
    elif action == "reject" and not target.is_approved:
        email = target.email
        services.reject(target)
        messages.success(request, f"Removed the request from {email}.")
    elif action == "disable":
        services.set_active(target, False)
        messages.success(request, f"Disabled {target.email} and signed them out everywhere.")
    elif action == "enable":
        services.set_active(target, True)
        messages.success(request, f"Re-enabled {target.email}.")
    elif action == "reset_mfa":
        services.reset_mfa(target, by=request.user)
        messages.success(request, f"Removed {target.email}'s 2FA. They'll set it up again at next sign-in.")
    elif action == "resend_invite":
        services.resend_invite(target, by=request.user)
        messages.success(request, f"Sent a new invitation link to {target.email}.")
    elif action == "quota":
        form = QuotaForm(request.POST)
        if form.is_valid():
            target.storage_quota_bytes = form.cleaned_data["quota_gb"] * GB
            target.save(update_fields=["storage_quota_bytes"])
            messages.success(request, f"{target.email} now has {form.cleaned_data['quota_gb']} GB.")
        else:
            messages.error(request, "Enter a whole number of GB.")
    else:
        messages.error(request, "That action isn't available for this account.")
    return redirect("accounts:manage_users")
