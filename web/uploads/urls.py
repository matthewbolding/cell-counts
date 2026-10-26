from django.urls import path

from uploads import api

app_name = "uploads"

urlpatterns = [
    path("api/projects/<uuid:project_id>/uploads/plan", api.plan, name="plan"),
    path("api/projects/<uuid:project_id>/uploads/init", api.init, name="init"),
    path("api/uploads/<uuid:upload_id>/chunks/<int:index>", api.chunk, name="chunk"),
    path("api/uploads/<uuid:upload_id>/complete", api.complete, name="complete"),
    path("api/uploads/<uuid:upload_id>", api.abort, name="abort"),
    path("api/images/<uuid:image_id>/import-cells", api.import_cells, name="import_cells"),
]
