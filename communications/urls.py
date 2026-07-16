from django.urls import path

from communications import views as v

urlpatterns = [
    path("chat/threads/", v.ThreadListCreateView.as_view(), name="chat-threads"),
    path("chat/threads/<uuid:thread_id>/messages/", v.ThreadMessagesView.as_view(),
         name="chat-messages"),
    path("chat/threads/<uuid:thread_id>/send/", v.ThreadSendView.as_view(),
         name="chat-send"),
    path("chat/threads/<uuid:thread_id>/read/", v.ThreadReadView.as_view(),
         name="chat-read"),
    path("chat/threads/<uuid:thread_id>/participants/", v.ThreadParticipantsView.as_view(),
         name="chat-participants"),
    path("chat/unread-count/", v.UnreadCountView.as_view(), name="chat-unread"),
    path("communications/webhooks/<str:provider>/", v.IncomingWebhookView.as_view(),
         name="communications-webhook"),
]
