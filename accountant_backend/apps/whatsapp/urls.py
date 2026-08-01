from django.urls import path

from . import views

urlpatterns = [
    path("whatsapp/connect/", views.WhatsAppConnectView.as_view(), name="whatsapp-connect"),
    path("whatsapp/status/", views.WhatsAppStatusView.as_view(), name="whatsapp-status"),
    path("whatsapp/qr/", views.WhatsAppQrView.as_view(), name="whatsapp-qr"),
    path("whatsapp/disconnect/", views.WhatsAppDisconnectView.as_view(), name="whatsapp-disconnect"),
    path("whatsapp/unlink/", views.WhatsAppUnlinkView.as_view(), name="whatsapp-unlink"),
    path("whatsapp/send/", views.WhatsAppSendTextView.as_view(), name="whatsapp-send-text"),
]
