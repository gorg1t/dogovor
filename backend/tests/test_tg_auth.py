"""Проверка входа в мини-апп: подпись initData и параметр чата."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from dogovorilis.api.tg_auth import InitDataError, chat_param, parse_chat_param, validate_init_data

TOKEN = "123456:TEST-token"


def make_init_data(fields: dict, token: str = TOKEN) -> str:
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def test_valid_init_data():
    raw = make_init_data({"auth_date": str(int(time.time())), "start_param": "c1004278948187_abc",
                          "user": json.dumps({"id": 42, "first_name": "Егор"}, ensure_ascii=False)})
    data = validate_init_data(raw, TOKEN)
    assert data["user"]["id"] == 42 and data["start_param"] == "c1004278948187_abc"


def test_forged_or_stale_init_data():
    raw = make_init_data({"auth_date": str(int(time.time())), "user": '{"id": 42}'})
    with pytest.raises(InitDataError):
        validate_init_data(raw.replace("42", "43"), TOKEN)          # подменили пользователя
    with pytest.raises(InitDataError):
        validate_init_data(raw, "999:other-token")                  # подписано другим ботом
    old = make_init_data({"auth_date": str(int(time.time()) - 3 * 86400), "user": '{"id": 42}'})
    with pytest.raises(InitDataError):
        validate_init_data(old, TOKEN)                              # старше суток


def test_chat_param_roundtrip_and_tamper():
    p = chat_param(-1004278948187, "secret")
    assert len(p) <= 64 and all(ch.isalnum() or ch in "_-" for ch in p)
    assert parse_chat_param(p, "secret") == -1004278948187
    assert parse_chat_param(p.replace("1004", "1005"), "secret") is None
    assert parse_chat_param(p, "other-secret") is None
