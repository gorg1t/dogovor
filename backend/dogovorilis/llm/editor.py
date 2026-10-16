"""Исправление карточки обычным текстом: «не Егор, а Андрей», «срок — суббота», «задача: сверстать главную»."""

import json
from datetime import date, datetime, timedelta

from .dates import resolve_deadline
from .extractor import WEEKDAY_FULL, WEEKDAY_SHORT, _match_member, _parse_json
from .gigachat import GigaChatClient

SYSTEM = """Ты правишь карточку задачи командного чата по просьбе пользователя.
Меняй только то, о чём просят; остальное оставь как было. Ответственный — строго имя из списка участников.
Срок верни фразой, как сказал пользователь («к сб», «завтра», «20.10»), или null, если просят убрать срок.
Ответь ТОЛЬКО JSON: {"assignee": "...", "task": "...", "deadline_text": "..." | null}"""


async def edit_card(client: GigaChatClient, model: str, card: dict, members: list[dict], correction: str,
                    now: datetime, checkpoint: date | None) -> dict:
    """Возвращает обновлённую карточку (копию). Исключение — если модель не ответила разборчиво."""
    people = ", ".join(m["name"] for m in members)
    cal = ", ".join(f"{WEEKDAY_SHORT[x.weekday()]} {x:%d.%m}" for x in (now.date() + timedelta(days=i) for i in range(1, 15)))
    current = {"assignee": card.get("assignee_name"), "task": card.get("task"), "deadline_text": card.get("deadline_text"),
               "deadline": card.get("deadline")}
    user = (f"Участники: {people}\nСегодня: {now.date().isoformat()} ({WEEKDAY_FULL[now.weekday()]})\n"
            f"Календарь: {cal}\nЧекпоинт команды: {checkpoint.isoformat() if checkpoint else 'не задан'}\n\n"
            f"Карточка сейчас: {json.dumps(current, ensure_ascii=False)}\n"
            f"Просьба пользователя: «{correction}»")
    res = await client.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                            model=model, max_tokens=200)
    data = _parse_json(res.content)
    p = dict(card)
    if data.get("assignee"):
        name = _match_member(data["assignee"], members, card.get("assignee_name"))
        p["assignee_name"] = name
        p["assignee_user_id"] = next((m["user_id"] for m in members if m["name"] == name), None)
    if data.get("task"):
        p["task"] = str(data["task"]).strip()
    if "deadline_text" in data:
        dl_text = data["deadline_text"] or None
        if dl_text != card.get("deadline_text"):
            d, prec = resolve_deadline(dl_text, now, checkpoint)
            p["deadline_text"] = dl_text
            p["deadline"] = d.isoformat() if d and prec == "day" else None
    p["edited"] = True
    return p


FORCE_SYSTEM = """Пользователь сам отметил сообщение командного чата как договорённость (команда /task) — задача в нём есть.
Сформулируй её. Ответственный — имя из списка участников: автор сообщения, если он берёт дело на себя,
или тот, кому дело прямо поручают. Ответь ТОЛЬКО JSON:
{"assignee": "...", "task": "3–8 слов, с глагола", "deadline_text": "фраза срока из сообщения" | null}"""


async def force_card(client: GigaChatClient, model: str, text: str, author: str, members: list[dict],
                     now: datetime, checkpoint: date | None) -> tuple[str, str, str | None, str | None]:
    """(ответственный, задача, срок ISO, фраза срока) для сообщения, которое человек отметил как договорённость."""
    people = ", ".join(m["name"] for m in members)
    user = f"Участники: {people}\nАвтор сообщения: {author}\nСообщение: «{text}»"
    res = await client.chat([{"role": "system", "content": FORCE_SYSTEM}, {"role": "user", "content": user}],
                            model=model, max_tokens=150)
    data = _parse_json(res.content)
    assignee = _match_member(data.get("assignee"), members, author)
    if assignee not in {m["name"] for m in members} | {author}:
        assignee = author
    dl_text = data.get("deadline_text") or None
    d, prec = resolve_deadline(dl_text, now, checkpoint)
    return assignee, str(data.get("task") or text).strip(), (d.isoformat() if d and prec == "day" else None), dl_text
