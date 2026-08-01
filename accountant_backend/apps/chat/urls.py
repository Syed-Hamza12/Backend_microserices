from django.urls import path

from . import views

urlpatterns = [
    path("chat/message/", views.SendChatMessageView.as_view(), name="send-chat-message"),
    # Urdu script -> Roman Urdu for dictated input (the ur-PK recogniser emits
    # no other script). Called by the app in Roman Urdu mode only.
    path("chat/transliterate/", views.TransliterateView.as_view(), name="chat-transliterate"),
    # Multi-device sync state: tells a device when history was wiped elsewhere.
    path("chat/sync-state/", views.ChatSyncStateView.as_view(), name="chat-sync-state"),
    # Deletes all chat history for this business, on every device.
    path("chat/history/", views.ClearChatHistoryView.as_view(), name="clear-chat-history"),
    # History, for the mobile offline cache.
    path("chat/conversations/", views.ConversationListView.as_view(), name="conversation-list"),
    path(
        "chat/conversations/<int:conversation_id>/messages/",
        views.ConversationMessagesView.as_view(),
        name="conversation-messages",
    ),
    # Persists Edit Draft's changes. Without this the confirm below records the
    # AI's original figures no matter what the owner edited.
    path("chat/draft/<int:message_id>/", views.UpdateDraftBillView.as_view(), name="update-draft-bill"),
    path("chat/draft/<int:message_id>/confirm/", views.ConfirmDraftBillView.as_view(), name="confirm-draft-bill"),
    path(
        "chat/draft/<int:message_id>/confirm-document/",
        views.ConfirmDraftDocumentView.as_view(),
        name="confirm-draft-document",
    ),
    path(
        "chat/draft/<int:message_id>/confirm-action/",
        views.ConfirmDraftActionView.as_view(),
        name="confirm-draft-action",
    ),
]
