"""Транспорты исходящих каналов: e-mail, Telegram, WhatsApp, MAX, SMS.

До появления этого модуля система только ставила доставку в очередь: записи
`NotificationDelivery`, `OutboundMessageDelivery` создавались, но никто их не
читал и наружу ничего не уходило. Здесь собран единственный слой, который
действительно обращается к внешним сервисам, и единственное место, где
известно, какие каналы реально настроены.

Правило слоя: канал либо настроен и отправляет, либо честно поднимает
`ChannelNotConfigured`. Молча «делать вид, что отправлено» нельзя — именно из
этого выросла проблема, когда оператор считал, что клиент получил документ.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field

from django.conf import settings

logger = logging.getLogger("travelhub.transports")

#: Каналы, которые может запросить правило уведомления или тред чата.
CHANNELS = ("desktop", "internal", "email", "telegram", "whatsapp", "max", "sms", "push")

#: Каналы, доставляемые внутри CRM: сам факт существования записи и есть доставка.
IN_APP_CHANNELS = ("desktop", "internal")

_HTTP_TIMEOUT = 20


class ChannelNotConfigured(Exception):
    """Канал не настроен администратором — отправлять нечем."""


class TransportError(Exception):
    """Временная ошибка внешнего сервиса: доставку имеет смысл повторить."""


class PermanentTransportError(TransportError):
    """Отправка невозможна по сути (нет адреса, адрес отвергнут) — не повторять."""


@dataclass(frozen=True)
class Attachment:
    filename: str
    content: bytes
    mime_type: str = ""

    def resolved_mime(self) -> str:
        return self.mime_type or mimetypes.guess_type(self.filename)[0] or "application/octet-stream"


@dataclass(frozen=True)
class TransportResult:
    external_id: str = ""
    detail: str = ""
    meta: dict = field(default_factory=dict)


# ——— Конфигурация ————————————————————————————————————————————————————————


def _cfg(name: str, default=""):
    return getattr(settings, name, default)


def channel_status() -> dict[str, dict]:
    """Что реально настроено. Используется API и интерфейсом, чтобы не обещать
    доставку по каналу, у которого нет ни одного реквизита."""
    return {
        "desktop": {
            "configured": True,
            "kind": "in_app",
            "requirement": "",
        },
        "internal": {
            "configured": True,
            "kind": "in_app",
            "requirement": "",
        },
        "email": {
            "configured": bool(_cfg("EMAIL_HOST") and _cfg("DEFAULT_FROM_EMAIL")),
            "kind": "external",
            "requirement": "SMTP-сервер (EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD) и адрес отправителя DEFAULT_FROM_EMAIL",
        },
        "telegram": {
            "configured": bool(_cfg("TELEGRAM_BOT_TOKEN")),
            "kind": "external",
            "requirement": "токен бота Telegram (TELEGRAM_BOT_TOKEN)",
        },
        "whatsapp": {
            "configured": bool(_cfg("WHATSAPP_API_URL") and _cfg("WHATSAPP_API_TOKEN")),
            "kind": "external",
            "requirement": "адрес и токен WhatsApp Business API (WHATSAPP_API_URL, WHATSAPP_API_TOKEN)",
        },
        "max": {
            "configured": bool(_cfg("MAX_API_URL") and _cfg("MAX_API_TOKEN")),
            "kind": "external",
            "requirement": "адрес и токен API мессенджера MAX (MAX_API_URL, MAX_API_TOKEN)",
        },
        "sms": {
            "configured": bool(_cfg("SMS_GATEWAY_URL") and _cfg("SMS_GATEWAY_TOKEN")),
            "kind": "external",
            "requirement": "SMS-шлюз (SMS_GATEWAY_URL, SMS_GATEWAY_TOKEN, SMS_GATEWAY_SENDER)",
        },
        "push": {
            "configured": False,
            "kind": "external",
            "requirement": "Web Push: пара ключей VAPID и хранилище подписок браузеров (не реализовано)",
        },
    }


def is_configured(channel: str) -> bool:
    return bool(channel_status().get(channel, {}).get("configured"))


def requirement_for(channel: str) -> str:
    status = channel_status().get(channel)
    if status is None:
        return f"неизвестный канал «{channel}»"
    return status["requirement"]


def not_configured_reason(channel: str) -> str:
    return f"Канал не настроен: требуется {requirement_for(channel)}"


# ——— HTTP-помощники ——————————————————————————————————————————————————————


def _post_json(url: str, payload: dict, *, token: str = "", headers: dict | None = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    return _perform(request)


def _post_multipart(url: str, fields: dict, files: dict) -> dict:
    boundary = f"----travelhub{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    for name, attachment in files.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{attachment.filename}"\r\n'
            f"Content-Type: {attachment.resolved_mime()}\r\n\r\n".encode()
        )
        chunks.append(attachment.content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    request = urllib.request.Request(url, data=b"".join(chunks), method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    return _perform(request)


def _perform(request: urllib.request.Request) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:  # noqa: S310
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        # 4xx, кроме 408/429, повторять бессмысленно: адрес или запрос неверны.
        if 400 <= exc.code < 500 and exc.code not in (408, 429):
            raise PermanentTransportError(f"HTTP {exc.code}: {detail}") from exc
        raise TransportError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TransportError(f"Сеть недоступна: {exc}") from exc
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"raw": raw[:300]}


# ——— Транспорты ——————————————————————————————————————————————————————————


def _send_email(recipient: str, subject: str, body: str, attachment: Attachment | None) -> TransportResult:
    from django.core.mail import EmailMessage

    if not is_configured("email"):
        raise ChannelNotConfigured(not_configured_reason("email"))
    if "@" not in recipient:
        raise PermanentTransportError(f"Некорректный адрес получателя: «{recipient}»")
    message = EmailMessage(
        subject=subject or "Сообщение Travel Hub CRM",
        body=body,
        from_email=_cfg("DEFAULT_FROM_EMAIL"),
        to=[recipient],
    )
    if attachment is not None:
        message.attach(attachment.filename, attachment.content, attachment.resolved_mime())
    try:
        sent = message.send(fail_silently=False)
    except Exception as exc:  # SMTP-ошибки разнородны, категорию задаёт сам SMTP
        raise TransportError(f"SMTP: {exc}") from exc
    if not sent:
        raise TransportError("SMTP не принял письмо")
    return TransportResult(detail=f"письмо отправлено на {recipient}")


def _send_telegram(recipient: str, subject: str, body: str, attachment: Attachment | None) -> TransportResult:
    if not is_configured("telegram"):
        raise ChannelNotConfigured(not_configured_reason("telegram"))
    if not recipient:
        raise PermanentTransportError("Не указан chat_id или @username получателя")
    base = f"{_cfg('TELEGRAM_API_URL', 'https://api.telegram.org').rstrip('/')}/bot{_cfg('TELEGRAM_BOT_TOKEN')}"
    text = f"{subject}\n\n{body}".strip() if subject else body
    if attachment is not None:
        data = _post_multipart(
            f"{base}/sendDocument",
            {"chat_id": recipient, "caption": text[:1024]},
            {"document": attachment},
        )
    else:
        data = _post_json(f"{base}/sendMessage", {"chat_id": recipient, "text": text[:4096]})
    if data.get("ok") is False:
        description = str(data.get("description", ""))[:200]
        raise PermanentTransportError(f"Telegram отклонил отправку: {description}")
    message_id = str((data.get("result") or {}).get("message_id", ""))
    return TransportResult(external_id=message_id, detail=f"Telegram → {recipient}")


def _send_http_messenger(
    channel: str, url: str, token: str, recipient: str, subject: str, body: str, attachment: Attachment | None
) -> TransportResult:
    if not is_configured(channel):
        raise ChannelNotConfigured(not_configured_reason(channel))
    if not recipient:
        raise PermanentTransportError("Не указан идентификатор получателя")
    payload = {
        "to": recipient,
        "subject": subject,
        "text": body,
    }
    if attachment is not None:
        import base64

        payload["attachment"] = {
            "filename": attachment.filename,
            "content_type": attachment.resolved_mime(),
            "content_base64": base64.b64encode(attachment.content).decode(),
        }
    data = _post_json(url, payload, token=token)
    external_id = str(data.get("id") or data.get("message_id") or "")
    return TransportResult(external_id=external_id, detail=f"{channel} → {recipient}")


def _send_sms(recipient: str, subject: str, body: str, attachment: Attachment | None) -> TransportResult:
    if not is_configured("sms"):
        raise ChannelNotConfigured(not_configured_reason("sms"))
    if not recipient:
        raise PermanentTransportError("Не указан номер телефона получателя")
    text = f"{subject}. {body}".strip(". ") if subject else body
    payload = {
        "to": recipient,
        "text": text[:800],
        "sender": _cfg("SMS_GATEWAY_SENDER"),
    }
    data = _post_json(_cfg("SMS_GATEWAY_URL"), payload, token=_cfg("SMS_GATEWAY_TOKEN"))
    external_id = str(data.get("id") or data.get("message_id") or "")
    detail = f"SMS → {recipient}"
    if attachment is not None:
        detail += " (вложение SMS-каналом не передаётся)"
    return TransportResult(external_id=external_id, detail=detail)


def send_message(
    channel: str,
    recipient: str,
    *,
    subject: str = "",
    body: str = "",
    attachment: Attachment | None = None,
) -> TransportResult:
    """Отправляет сообщение выбранным каналом.

    Поднимает `ChannelNotConfigured`, если канал не настроен, `PermanentTransportError`
    при заведомо неисправимой ошибке и `TransportError` при временной.
    """
    if channel in IN_APP_CHANNELS:
        return TransportResult(detail="доставлено внутри CRM")
    if channel == "email":
        return _send_email(recipient, subject, body, attachment)
    if channel == "telegram":
        return _send_telegram(recipient, subject, body, attachment)
    if channel == "whatsapp":
        return _send_http_messenger(
            "whatsapp", _cfg("WHATSAPP_API_URL"), _cfg("WHATSAPP_API_TOKEN"),
            recipient, subject, body, attachment,
        )
    if channel == "max":
        return _send_http_messenger(
            "max", _cfg("MAX_API_URL"), _cfg("MAX_API_TOKEN"), recipient, subject, body, attachment
        )
    if channel == "sms":
        return _send_sms(recipient, subject, body, attachment)
    if channel == "push":
        raise ChannelNotConfigured(not_configured_reason("push"))
    raise ChannelNotConfigured(f"Канал «{channel}» не поддерживается")
