from django.urls import path

from processing import api, views

app_name = "processing"

urlpatterns = [
    path("queue/", views.queue_page, name="queue_page"),
    path("api/queue/", api.queue_overview, name="api_queue"),
    path("api/queue/reorder", api.queue_reorder, name="api_queue_reorder"),
    path("api/queue/pause", api.queue_pause, name="api_queue_pause"),
    path("api/queue/cancel", api.queue_cancel, name="api_queue_cancel"),
    path("api/admin/queue/pause", api.admin_queue_pause, name="api_admin_queue_pause"),
    path("api/images/<uuid:image_id>/rescans", api.create_rescan, name="api_create_rescan"),
    path("api/rescans/<uuid:sweep_id>", api.rescan_detail, name="api_rescan"),
    path("api/projects/<uuid:project_id>/exports", api.create_export, name="api_create_export"),
    path("api/exports/<uuid:export_id>", api.export_detail, name="api_export"),
    path("exports/<uuid:export_id>/download", api.export_download, name="export_download"),
]
