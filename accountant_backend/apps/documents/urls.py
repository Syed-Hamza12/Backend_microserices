from django.urls import path

from . import views

urlpatterns = [
    # Which formats each document type supports, so the app offers only valid choices.
    path("documents/formats/", views.DocumentFormatsView.as_view(), name="document-formats"),
    # Whole-ledger Excel export for the Settings > Data Export screen.
    path("documents/export/excel/", views.ExportExcelView.as_view(), name="export-excel"),
    # Renders and returns the file itself, for preview/share. Nothing is stored.
    path("documents/render/", views.RenderDocumentView.as_view(), name="render-document"),
    # Queues a render-and-send over WhatsApp; returns a job id to poll.
    path("documents/send/", views.SendDocumentView.as_view(), name="send-document"),
    path("documents/deliveries/", views.DocumentDeliveryListView.as_view(), name="document-deliveries"),
    path(
        "documents/deliveries/<int:delivery_id>/",
        views.DocumentDeliveryDetailView.as_view(),
        name="document-delivery-detail",
    ),
]
