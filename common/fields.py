"""Поля моделей: шифрование чувствительных данных на уровне приложения.

Паспортные номера, банковские реквизиты и provider secrets хранятся в БД
только в зашифрованном виде (Fernet/AES128-CBC+HMAC). Ключ — вне БД и git
(FIELD_ENCRYPTION_KEY). В обычных API-ответах значения маскируются.
"""
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

_PREFIX = "enc$1$"  # версия схемы шифрования для будущей ротации ключа


def _fernet() -> Fernet:
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        raise RuntimeError("FIELD_ENCRYPTION_KEY не задан — шифрование полей невозможно")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_value(value: str) -> str:
    return _PREFIX + _fernet().encrypt(value.encode()).decode()


def decrypt_value(stored: str) -> str:
    if not stored.startswith(_PREFIX):
        # Данные до включения шифрования (не должно встречаться в production).
        return stored
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Не удалось расшифровать поле: неверный ключ или повреждённые данные") from exc


class EncryptedTextField(models.TextField):
    """TextField, прозрачно шифрующий значение при записи в БД."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value in (None, ""):
            return value
        if isinstance(value, str) and value.startswith(_PREFIX):
            return value
        return encrypt_value(str(value))

    def from_db_value(self, value, expression, connection):  # noqa: ARG002
        if value in (None, ""):
            return value
        return decrypt_value(value)


def mask_tail(value: str | None, visible: int = 4, mask_char: str = "*") -> str:
    """Маскирует значение, оставляя последние `visible` символов: ****1234."""
    if not value:
        return ""
    if len(value) <= visible:
        return mask_char * len(value)
    return mask_char * (len(value) - visible) + value[-visible:]
