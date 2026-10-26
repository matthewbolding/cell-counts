from django.urls import path, re_path

from projects import api, views

app_name = "projects"

urlpatterns = [
    path("projects/", views.project_list, name="list"),
    path("projects/<uuid:project_id>/", views.workspace, name="workspace"),
    path("projects/<uuid:project_id>/settings/", views.project_settings, name="settings"),
    path("projects/<uuid:project_id>/rename/", views.rename, name="rename"),
    path("projects/<uuid:project_id>/share/", views.share, name="share"),
    path("projects/<uuid:project_id>/share/<int:membership_id>/remove/", views.unshare, name="unshare"),
    path("projects/<uuid:project_id>/delete/", views.delete, name="delete"),

    path("api/projects/<uuid:project_id>/", api.project_detail, name="api_project"),
    path("api/projects/<uuid:project_id>/status", api.project_status, name="api_project_status"),
    path("api/projects/<uuid:project_id>/samples", api.project_samples, name="api_project_samples"),
    path("api/projects/<uuid:project_id>/ui-state", api.ui_state, name="api_ui_state"),
    path("api/images/<uuid:image_id>/cells", api.image_cells, name="api_image_cells"),
    path("api/images/<uuid:image_id>/cells/ops", api.cell_ops, name="api_cell_ops"),
    path("api/images/<uuid:image_id>/lock", api.image_lock, name="api_image_lock"),
    path("api/images/retry", api.retry_images, name="api_retry_images"),
    path("api/samples/<uuid:sample_id>/composite", api.sample_composite, name="api_composite"),

    re_path(r"^tiles/(?P<image_id>[0-9a-f-]{36})/(?P<rev>[0-9a-f]{12})/(?P<level>\d{1,2})/(?P<name>[0-9_]+\.png)$",
            api.tile, name="tile"),
]
