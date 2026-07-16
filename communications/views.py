"""Chat API (ТЗ §17)."""
import hashlib
import hmac

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers, status as http
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import has_permission, require
from common.audit import audit
from common.errors import ApiError
from common.outbox import emit_event
from common.pagination import DefaultPagination
from communications.models import (
    ChatThread, Message, OutboundMessageDelivery, ThreadParticipant, WebhookEvent,
)


class ThreadSerializer(serializers.ModelSerializer):
    unread_count = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = ChatThread
        fields = ["id", "type", "order", "service", "title", "external_channel",
                  "status", "unread_count", "last_message", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_unread_count(self, obj) -> int:
        request = self.context.get("request")
        if request is None:
            return 0
        participant = obj.participants.filter(user=request.user, left_at__isnull=True).first()
        if participant is None:
            return 0
        qs = obj.messages.filter(deleted_at__isnull=True)
        if participant.last_read_message_id:
            qs = qs.filter(id__gt=participant.last_read_message_id)
        return qs.exclude(author_user=request.user).count()

    def get_last_message(self, obj):
        message = obj.messages.filter(deleted_at__isnull=True).order_by("-created_at").first()
        if message is None:
            return None
        return {"id": str(message.id), "type": message.type,
                "body": message.body[:200], "created_at": message.created_at}


class MessageSerializer(serializers.ModelSerializer):
    author_name = serializers.CharField(source="author_user.get_full_name",
                                        read_only=True, default="")

    class Meta:
        model = Message
        fields = ["id", "type", "body", "author_user", "author_name", "author_external",
                  "reply_to", "attachment", "service_card", "delivery_state",
                  "edited_at", "deleted_at", "created_at"]


def _threads_qs(request):
    qs = ChatThread.objects.filter(tenant_id=request.user.tenant_id,
                                   archived_at__isnull=True)
    allowed_types = []
    if has_permission(request.user, "communications.view_internal"):
        allowed_types += ["internal", "supplier"]
    if has_permission(request.user, "communications.view_client"):
        allowed_types.append("client")
    return qs.filter(type__in=allowed_types)


def _get_thread(request, thread_id) -> ChatThread:
    thread = _threads_qs(request).filter(pk=thread_id).first()
    if thread is None:
        raise ApiError(code="NOT_FOUND", message="Тред не найден", status_code=404)
    return thread


class ThreadListCreateView(GenericAPIView):
    permission_classes = [require("communications.view_internal",
                                  "communications.view_client")]
    pagination_class = DefaultPagination
    serializer_class = ThreadSerializer

    def get(self, request):
        qs = _threads_qs(request)
        params = request.query_params
        if thread_type := params.get("type"):
            qs = qs.filter(type=thread_type)
        if order_id := params.get("order"):
            qs = qs.filter(order_id=order_id)
        if q := params.get("q", "").strip():
            qs = qs.filter(Q(title__icontains=q))
        page = self.paginate_queryset(qs.order_by("-updated_at"))
        return self.get_paginated_response(
            ThreadSerializer(page, many=True, context={"request": request}).data)

    def post(self, request):
        serializer = ThreadSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        thread_type = serializer.validated_data.get("type")
        needed = ("communications.view_client" if thread_type == "client"
                  else "communications.view_internal")
        if not has_permission(request.user, needed):
            raise ApiError(code="PERMISSION_DENIED", message=f"Нет права {needed}",
                           status_code=403)
        with transaction.atomic():
            thread = serializer.save(tenant_id=request.user.tenant_id,
                                     created_by=request.user)
            ThreadParticipant.objects.create(tenant_id=request.user.tenant_id,
                                             thread=thread, user=request.user,
                                             role="owner", created_by=request.user)
        return Response(ThreadSerializer(thread, context={"request": request}).data,
                        status=http.HTTP_201_CREATED)


class ThreadMessagesView(APIView):
    permission_classes = [require("communications.view_internal",
                                  "communications.view_client")]

    def get(self, request, thread_id):
        """Cursor-пагинация ?before=<message_id> для чатов с миллионами сообщений."""
        thread = _get_thread(request, thread_id)
        qs = thread.messages.select_related("author_user").order_by("-id")
        if before := request.query_params.get("before"):
            before_message = thread.messages.filter(pk=before).first()
            if before_message:
                qs = qs.filter(id__lt=before_message.id)
        limit = min(int(request.query_params.get("limit", 50)), 200)
        messages = list(qs[:limit])
        return Response({
            "results": MessageSerializer(reversed(messages), many=True).data,
            "has_more": len(messages) == limit,
        })


class ThreadSendView(APIView):
    permission_classes = [require("communications.send")]

    def post(self, request, thread_id):
        thread = _get_thread(request, thread_id)
        body = str(request.data.get("body", "")).strip()
        message_type = str(request.data.get("type", "text"))
        internal_note = bool(request.data.get("internal_note"))
        if not body and message_type == "text":
            raise ApiError(code="VALIDATION_ERROR", message="body обязателен",
                           status_code=400)
        with transaction.atomic():
            message = Message.objects.create(
                tenant_id=thread.tenant_id, thread=thread, author_user=request.user,
                type=message_type, body=body,
                reply_to_id=request.data.get("reply_to"),
                service_card_id=request.data.get("service_card"),
                created_by=request.user,
            )
            # исходящее во внешний канал — транзакционно через outbox (ТЗ §17);
            # внутреннее сообщение никогда не отправляется клиенту
            if thread.type == "client" and thread.external_channel and not internal_note:
                OutboundMessageDelivery.objects.create(
                    message=message, channel=thread.external_channel,
                    recipient=thread.external_account,
                )
            thread.updated_by = request.user
            thread.save(update_fields=["updated_at", "updated_by"])
            emit_event("chat.message.created", message,
                       payload={"thread_id": str(thread.id),
                                "thread_type": thread.type})
            # упоминания создают notifications (через outbox-процессор notifications)
            for mention in _extract_mentions(body):
                emit_event("chat.mention", message,
                           payload={"thread_id": str(thread.id), "mention": mention})
        return Response(MessageSerializer(message).data, status=http.HTTP_201_CREATED)


def _extract_mentions(body: str) -> list[str]:
    import re

    return re.findall(r"@([\w.@+-]+)", body)


class ThreadReadView(APIView):
    permission_classes = [require("communications.view_internal",
                                  "communications.view_client")]

    def post(self, request, thread_id):
        thread = _get_thread(request, thread_id)
        message_id = request.data.get("message")
        message = thread.messages.filter(pk=message_id).first() if message_id else \
            thread.messages.order_by("-id").first()
        participant, _ = ThreadParticipant.objects.get_or_create(
            thread=thread, user=request.user, left_at__isnull=True,
            defaults={"tenant_id": thread.tenant_id, "created_by": request.user},
        )
        participant.last_read_message = message
        participant.last_read_at = timezone.now()
        participant.save(update_fields=["last_read_message", "last_read_at"])
        emit_event("chat.message.read", thread,
                   payload={"user_id": str(request.user.id)},
                   audience_user=request.user)
        return Response({"last_read_message": str(message.id) if message else None})


class ThreadParticipantsView(APIView):
    permission_classes = [require("communications.view_internal",
                                  "communications.view_client")]

    def get(self, request, thread_id):
        thread = _get_thread(request, thread_id)
        return Response([
            {"id": str(p.id), "user": str(p.user_id) if p.user_id else None,
             "person": str(p.person_id) if p.person_id else None,
             "external_identity": p.external_identity, "role": p.role,
             "joined_at": p.joined_at, "left_at": p.left_at}
            for p in thread.participants.all()
        ])

    def post(self, request, thread_id):
        from accounts.models import User

        thread = _get_thread(request, thread_id)
        user = User.objects.filter(pk=request.data.get("user"),
                                   tenant_id=request.user.tenant_id).first()
        if user is None:
            raise ApiError(code="VALIDATION_ERROR", message="Пользователь не найден",
                           status_code=400)
        participant, created = ThreadParticipant.objects.get_or_create(
            thread=thread, user=user, left_at__isnull=True,
            defaults={"tenant_id": thread.tenant_id, "created_by": request.user},
        )
        return Response({"id": str(participant.id)},
                        status=http.HTTP_201_CREATED if created else http.HTTP_200_OK)


class UnreadCountView(APIView):
    permission_classes = [require("communications.view_internal",
                                  "communications.view_client")]

    def get(self, request):
        total = 0
        threads = _threads_qs(request).filter(participants__user=request.user,
                                              participants__left_at__isnull=True)
        for thread in threads:
            participant = thread.participants.filter(user=request.user).first()
            qs = thread.messages.filter(deleted_at__isnull=True).exclude(
                author_user=request.user)
            if participant and participant.last_read_message_id:
                qs = qs.filter(id__gt=participant.last_read_message_id)
            total += qs.count()
        return Response({"unread": total})


class IncomingWebhookView(APIView):
    """Входящие webhook внешних каналов: подпись, replay, дедупликация (ТЗ §17)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def post(self, request, provider):
        secret = getattr(settings, "WEBHOOK_SECRETS", {}).get(provider, "")
        signature = request.headers.get("X-Webhook-Signature", "")
        payload_bytes = request.body or b"{}"
        signature_valid = False
        if secret:
            expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
            signature_valid = hmac.compare_digest(expected, signature)
            if not signature_valid:
                raise ApiError(code="INVALID_SIGNATURE", message="Подпись не совпадает",
                               status_code=401)
        external_event_id = str(request.data.get("event_id", ""))
        if not external_event_id:
            raise ApiError(code="VALIDATION_ERROR", message="event_id обязателен",
                           status_code=400)
        event, created = WebhookEvent.objects.get_or_create(
            provider=provider, external_event_id=external_event_id,
            defaults={"payload": request.data, "signature_valid": signature_valid},
        )
        if not created:
            return Response({"status": "duplicate"})  # 2xx без повторного side effect
        # обработка: входящее сообщение в соответствующий тред по mapping identity
        message_data = request.data.get("message") or {}
        thread = ChatThread.objects.filter(
            external_channel=provider,
            external_account=str(request.data.get("account", "")),
        ).first()
        if thread is not None and message_data.get("text"):
            message = Message.objects.create(
                tenant_id=thread.tenant_id, thread=thread,
                author_external=str(request.data.get("sender", "")),
                type="text", body=str(message_data["text"]),
                external_message_id=str(message_data.get("id", "")),
                delivery_state="delivered",
            )
            emit_event("chat.message.created", message,
                       payload={"thread_id": str(thread.id), "thread_type": thread.type},
                       tenant_id=thread.tenant_id)
        event.processed_at = timezone.now()
        event.save(update_fields=["processed_at"])
        return Response({"status": "ok"})
