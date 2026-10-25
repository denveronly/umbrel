"""Шифрування паролів сторонніх сервісів (кабінет YASNO, токен Telegram) у базі.

Ключ — окремий файл data/vault.key, який НЕ потрапляє в бекапи (там лише шифротекст), тож бекап на NAS
чи скачаний ZIP не розкриває пароль. Шифр — потоковий на HMAC-SHA256 (лише стандартна бібліотека)
з перевіркою цілісності (encrypt-then-MAC).
"""
import base64
import hashlib
import hmac
import os
import secrets

from db import DATA_DIR

KEY_FILE = os.path.join(DATA_DIR, "vault.key")
PREFIX = "v1:"


def _key():
    try:
        with open(KEY_FILE, "rb") as f:
            k = f.read()
        if len(k) == 32:
            return k
    except OSError:
        pass
    k = secrets.token_bytes(32)
    os.makedirs(DATA_DIR, exist_ok=True)
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    return k


def _stream(key, nonce, n):
    out, i = b"", 0
    while len(out) < n:
        out += hmac.new(key, b"enc" + nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
        i += 1
    return out[:n]


def encrypt(text):
    if not text:
        return ""
    key, nonce = _key(), secrets.token_bytes(16)
    data = text.encode()
    ct = bytes(a ^ b for a, b in zip(data, _stream(key, nonce, len(data))))
    tag = hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).digest()[:16]
    return PREFIX + base64.b64encode(nonce + tag + ct).decode()


def decrypt(token):
    """None — якщо ключ змінився (напр. відновили бекап на іншому сервері): пароль треба ввести знову."""
    if not token or not token.startswith(PREFIX):
        return None
    try:
        raw = base64.b64decode(token[len(PREFIX):])
    except ValueError:
        return None
    nonce, tag, ct = raw[:16], raw[16:32], raw[32:]
    key = _key()
    if not hmac.compare_digest(tag, hmac.new(key, b"mac" + nonce + ct, hashlib.sha256).digest()[:16]):
        return None
    return bytes(a ^ b for a, b in zip(ct, _stream(key, nonce, len(ct)))).decode()
