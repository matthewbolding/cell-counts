from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("accounts/pending/", views.pending, name="pending"),
    path("settings/", views.settings_view, name="settings"),
    path("manage/users/", views.manage_users, name="manage_users"),
    path("manage/users/invite/", views.manage_invite, name="manage_invite"),
    path("manage/users/<int:user_id>/<str:action>/", views.manage_user_action, name="manage_user_action"),
]
