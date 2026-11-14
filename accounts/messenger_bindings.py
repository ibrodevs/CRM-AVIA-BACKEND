import hashlib
import json
import re
import secrets
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import MessengerBinding, User
from common.audit import audit
from common.errors import ApiError
from common.transports import ChannelNotConfigured, TransportError, channel_status, send_message

CHANNELS = ['telegram', 'max', 'whatsapp']


def fail(message, status=400):
    raise ApiError(code='MESSENGER_BINDING', message=message, status_code=status)


def digest(user_id, channel, code):
    return salted_hmac('messenger-binding', f'{user_id}:{channel}:{code}').hexdigest()


def binding_data(user):
    rows = {row.channel: row for row in user.messenger_bindings.all()}
    status = channel_status()
    now = timezone.now()
    return [{'channel': channel, 'configured': status[channel]['configured'], 'mode': 'bot' if channel == 'telegram' and getattr(settings, 'WEBHOOK_SECRETS', {}).get('telegram') else 'code', 'verified': bool(rows.get(channel) and rows[channel].verified_at), 'pending': bool(rows.get(channel) and rows[channel].code_hash and rows[channel].expires_at and rows[channel].expires_at > now), 'address': rows[channel].address if rows.get(channel) and rows[channel].verified_at else getattr(user, channel, ''), 'verified_at': rows[channel].verified_at if rows.get(channel) else None} for channel in CHANNELS]


def telegram_bot_name():
    token = settings.TELEGRAM_BOT_TOKEN
    key = 'telegram-binding-bot-' + hashlib.sha256(token.encode()).hexdigest()
    name = cache.get(key)
    if not name:
        try:
            url = settings.TELEGRAM_API_URL.rstrip('/') + '/bot' + token + '/getMe'
            with urllib.request.urlopen(url, timeout=10) as response:
                name = json.load(response).get('result', {}).get('username', '')
        except Exception:
            fail('Не удалось получить данные Telegram-бота. Проверьте интеграцию организации.', 502)
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', name):
            fail('Telegram не подтвердил имя бота', 502)
        cache.set(key, name, 300)
    return name


@transaction.atomic
def complete_telegram_binding(text, sender):
    """Consume a state only after the gateway webhook signature is verified."""
    match = re.fullmatch(r'/start(?:@[A-Za-z0-9_]+)?\s+psc_(\d{1,18})_([-A-Za-z0-9_]{24,40})', str(text).strip())
    address = str(sender)
    if not match or not address.isdigit() or not 1 <= len(address) <= 20:
        return False
    user_id = MessengerBinding.objects.filter(pk=match[1], channel='telegram').values_list('user_id', flat=True).first()
    if not user_id:
        return False
    user = User.objects.select_for_update().get(pk=user_id)
    row = MessengerBinding.objects.select_for_update().filter(pk=match[1], user=user).first()
    now = timezone.now()
    if not row or row.pending_address != '__telegram_start__' or not row.expires_at or row.expires_at <= now or not constant_time_compare(row.code_hash, digest(user.pk, 'telegram', 'state:' + match[2])):
        return False
    row.address = address
    row.pending_address = ''
    row.verified_at = now
    row.code_hash = ''
    row.expires_at = None
    row.save()
    user.telegram = address
    user.save(update_fields=['telegram'])
    audit('accounts.messenger_verified', actor=user, tenant_id=user.tenant_id, resource=user, after={'channel': 'telegram', 'method': 'bot'})
    return True


class MessengerBindingsView(APIView):
    def get(self, request):
        return Response({'results': binding_data(request.user)})


class MessengerBindingView(APIView):
    @transaction.atomic
    def post(self, request, channel, action):
        if channel not in CHANNELS:
            fail('Неизвестный мессенджер', 404)
        actor = User.objects.select_for_update().get(pk=request.user.pk)
        row, _ = MessengerBinding.objects.get_or_create(user=actor, channel=channel)
        now = timezone.now()
        if action == 'unlink':
            setattr(actor, channel, '')
            actor.save(update_fields=[channel])
            row.delete()
            audit('accounts.messenger_unlinked', request=request, resource=actor, after={'channel': channel})
            return Response({'results': binding_data(actor)})
        if action == 'start':
            if not channel_status()[channel]['configured']:
                fail('Администратор ещё не подключил канал организации. Привязка станет доступна после настройки интеграции.', 409)
            if row.sent_at and now - row.sent_at < timedelta(seconds=60):
                fail('Новый код можно запросить через минуту', 429)
            if channel == 'telegram' and getattr(settings, 'WEBHOOK_SECRETS', {}).get('telegram'):
                name = telegram_bot_name()
                state = secrets.token_urlsafe(24)
                row.pending_address = '__telegram_start__'
                row.code_hash = digest(actor.pk, channel, 'state:' + state)
                row.expires_at = now + timedelta(minutes=10)
                row.sent_at = now
                row.attempts = 0
                row.save()
                return Response({'url': f'https://t.me/{name}?start=psc_{row.pk}_{state}', 'expires_at': row.expires_at})
            address = serializers.CharField(max_length=64, allow_blank=False).run_validation(request.data.get('address', ''))
            if channel == 'whatsapp':
                if not address.startswith('+') or not address[1:].isdigit() or not 7 <= len(address[1:]) <= 15:
                    fail('Укажите номер WhatsApp в международном формате с кодом страны')
            elif channel == 'telegram' and (not address.isdigit() or not 1 <= len(address) <= 20):
                fail('Для Telegram нужен ID личного чата с ботом организации. Сначала откройте бота и отправьте /start. Имя @username не позволяет боту отправить код.')
            code = str(secrets.randbelow(1000000)).zfill(6)
            row.pending_address = address
            row.code_hash = digest(actor.pk, channel, code)
            row.expires_at = now + timedelta(minutes=10)
            row.attempts = 0
            row.sent_at = now
            try:
                send_message(channel, address, subject='Подтверждение аккаунта ПСЦ', body=f'Код привязки к вашему профилю ПСЦ: {code}. Действует 10 минут. Никому не сообщайте код.')
            except (ChannelNotConfigured, TransportError):
                fail('Код не доставлен. Проверьте реквизиты и настройку канала; для Telegram сначала начните диалог с ботом.', 502)
            row.save()
            audit('accounts.messenger_challenge', request=request, resource=actor, after={'channel': channel})
            return Response({'sent': True, 'expires_at': row.expires_at})
        if action == 'confirm':
            if row.pending_address == '__telegram_start__':
                fail('Подтвердите привязку через кнопку Start в Telegram-боте')
            if not row.expires_at or row.expires_at <= now or not row.code_hash or row.attempts >= 5:
                fail('Код истёк или исчерпаны попытки. Запросите новый код.')
            row.attempts += 1
            code = str(request.data.get('code', '')).strip()
            if not constant_time_compare(row.code_hash, digest(actor.pk, channel, code)):
                row.save(update_fields=['attempts'])
                # Return, rather than raise, so failed-attempt accounting commits.
                return Response({'error': {'code': 'INVALID_CODE', 'message': 'Неверный код подтверждения'}}, status=400)
            row.address = row.pending_address
            row.pending_address = ''
            row.verified_at = now
            row.code_hash = ''
            row.expires_at = None
            row.save()
            setattr(actor, channel, row.address)
            actor.save(update_fields=[channel])
            audit('accounts.messenger_verified', request=request, resource=actor, after={'channel': channel})
            return Response({'results': binding_data(actor)})
        fail('Неизвестное действие', 404)
