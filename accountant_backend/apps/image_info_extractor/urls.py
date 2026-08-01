from django.urls import path

from . import views

urlpatterns = [
    path("chat/image/", views.UploadChatImageView.as_view(), name="upload-chat-image"),
]
