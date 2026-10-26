from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from accounts.models import KnownDevice, NotificationPreferences, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["email"]
    list_display = ["email", "display_name", "is_active", "is_staff", "approved_at", "last_login"]
    list_filter = ["is_active", "is_staff"]
    search_fields = ["email", "display_name"]
    fieldsets = [
        (None, {"fields": ["email", "password", "display_name"]}),
        ("Access", {"fields": ["is_active", "approved_at", "approved_by", "storage_quota_bytes"]}),
        ("Permissions", {"fields": ["is_staff", "is_superuser", "groups", "user_permissions"]}),
        ("Dates", {"fields": ["last_login", "date_joined"]}),
    ]
    add_fieldsets = [(None, {"classes": ["wide"], "fields": ["email", "password1", "password2"]})]
    readonly_fields = ["last_login", "date_joined", "approved_by"]


admin.site.register(NotificationPreferences)
admin.site.register(KnownDevice, list_display=["user", "user_agent", "network", "first_seen", "last_seen"])
