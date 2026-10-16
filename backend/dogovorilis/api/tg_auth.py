"""Вход в мини-апп Telegram: проверка initData и короткий параметр чата для ссылки t.me/<bot>/<app>?startapp=…

initData подписан токеном бота (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
secret = HMAC_SHA256(key="WebAppData", msg=bot_token); hash = HMAC_SHA256(key=secret, msg=data_check_string).
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

MAX_AGE_S = 24 * 3600


class InitDataError(ValueError):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age_s: int = MAX_AGE_S) -> dict:
    """Возвращает поля initData (user — уже словарь). Бросает InitDataError, если подпись или срок не сходятся."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("нет подписи")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("неверная подпись")
    if max_age_s and time.time() - int(pairs.get("auth_date", 0)) > max_age_s:
        raise InitDataError("данные устарели — откройте доску заново")
    if "user" in pairs:
        pairs["user"] = json.loads(pairs["user"])
    return pairs


def chat_param(chat_id: int, secret: str) -> str:
    """Короткий (≤ 64 символа, [A-Za-z0-9_-]) подписанный параметр чата для startapp."""
    body = f"c{abs(chat_id)}" if chat_id < 0 else f"p{chat_id}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:12]
    return f"{body}_{sig}"


def parse_chat_param(param: str, secret: str) -> int | None:
    try:
        body, sig = param.rsplit("_", 1)
    except ValueError:
        return None
    good = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:12]
    if not hmac.compare_digest(good, sig) or not body[1:].isdigit():
        return None
    return -int(body[1:]) if body[0] == "c" else int(body[1:])
