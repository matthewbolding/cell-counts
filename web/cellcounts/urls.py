from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.urls import include, path
from django.views.generic import RedirectView

from core import views as core_views

# Django admin signs in through allauth (and so through 2FA), never its own form.
admin.site.login = login_required(admin.site.login)
admin.site.site_header = "Cell Counts administration"

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="projects:list", permanent=False)),
    path("healthz", core_views.healthz, name="healthz"),
    path("accounts/", include("allauth.urls")),
    path("", include("accounts.urls")),
    path("", include("projects.urls")),
    path("", include("uploads.urls")),
    path("", include("processing.urls")),
    path("django-admin/", admin.site.urls),
]
