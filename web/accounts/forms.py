from __future__ import annotations

from allauth.account import forms as allauth_forms
from django import forms

from accounts.models import NotificationPreferences, User
from core.turnstile import TurnstileFormMixin


class LoginForm(TurnstileFormMixin, allauth_forms.LoginForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password"].help_text = ""  # the page has its own "Forgot your password?" link
        if "remember" in self.fields:
            self.fields["remember"].label = "Remember me on this computer"
        self.add_turnstile_field()


class SignupForm(TurnstileFormMixin, allauth_forms.SignupForm):
    display_name = forms.CharField(
        max_length=80, required=False, label="Name",
        help_text="How collaborators see you. Optional.")

    field_order = ["display_name", "email", "password1", "password2"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_turnstile_field()

    def save(self, request):
        user = super().save(request)
        user.display_name = self.cleaned_data.get("display_name", "").strip()
        user.save(update_fields=["display_name"])
        return user


class ResetPasswordForm(TurnstileFormMixin, allauth_forms.ResetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_turnstile_field()


class NotificationPreferencesForm(forms.ModelForm):
    class Meta:
        model = NotificationPreferences
        fields = ["batch_finished", "job_failures", "daily_summary"]
        labels = {
            "batch_finished": "Batch finished",
            "job_failures": "Processing failures",
            "daily_summary": "Daily summary",
        }


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["display_name"]
        labels = {"display_name": "Name"}


GB = 1024**3


class InviteForm(forms.Form):
    email = forms.EmailField(label="Email")
    display_name = forms.CharField(max_length=80, required=False, label="Name")
    quota_gb = forms.IntegerField(min_value=1, max_value=100_000, label="Storage quota (GB)")

    def clean_email(self) -> str:
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email


class QuotaForm(forms.Form):
    quota_gb = forms.IntegerField(min_value=1, max_value=100_000, label="Storage quota (GB)")
