from django.urls import path

from . import views

urlpatterns = [
    path("billing/dummy-ai-chat-ping/", views.DummyAiChatPingView.as_view(), name="dummy-ai-chat-ping"),
]
