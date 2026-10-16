"""Встречи: «кто придёт на созвон завтра?», «давайте встретимся, обсудим», «решили: созвон в пт в 19:00».

Как и «Кто возьмёт?» (ADR-6) — отдельный вызов модели только для сообщений со словами про встречу; основной
промпт извлечения договорённостей не меняется. Дату и время считаем правилами: арифметика дат — не для модели.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .dates import resolve_deadline
from .extractor import ChatMessage, _parse_json, build_user_prompt
from .gigachat import GigaChatClient

EVENT_WORDS = re.compile(
    r"(?<![а-я])(созвон\w*|созвонимся|созвониться|встреч\w*|встретимся|встретиться|увидимся|митинг\w*|мит|"
    r"собрани\w*|собираемся|соберемся|собраться|планерк\w*|стендап\w*|синк\w*|ретро|демо|обсудим|зум\w*|zoom|meet|"
    r"телемост\w*|колл\w*|call|кто придет|кто будет на|приходите|подключайтесь)(?![а-я])")


def is_event_message(text: str) -> bool:
    return bool(EVENT_WORDS.search((text or "").lower().replace("ё", "е")))


_NOT_VERB = r"(?![а-я])(?!\s+[а-я]+(?:ть|ти|чь)(?![а-я]))"  # «буду делать фронт» — это обещание, а не «приду»
_YES = re.compile(r"(?<![а-я])(буду|приду|подключусь|присоединюсь|я в деле|участвую)" + _NOT_VERB)
_NO_STRONG = re.compile(r"(?<![а-я])(не смогу|не приду|без меня|пропущу|не подключусь)" + _NOT_VERB)
_NO_WEAK = re.compile(r"(?<![а-я])(не буду|не получится)" + _NOT_VERB)
_MAYBE = re.compile(r"(?<![а-я])(постараюсь|возможно буду|может быть буду|может приду|не уверен\w*|под вопросом)")


def rsvp_intent(text: str, recent_event: bool) -> str | None:
    """«я буду на созвоне» → yes, «я завтра не смогу, у меня врач» → no — ответ на встречу, а не задача.

    Без слов про встречу засчитываем только короткое сообщение и только если встречу недавно предлагали.
    """
    t = (text or "").lower().replace("ё", "е")
    about_event = is_event_message(t)
    if not about_event and (not recent_event or len(t) > 80):
        return None
    if _MAYBE.search(t):
        return "maybe"
    if _NO_STRONG.search(t) or about_event and _NO_WEAK.search(t):
        return "no"
    if _YES.search(t):
        return "yes"
    return None


SYSTEM = """Ты помогаешь команде в Telegram планировать встречи. Посмотри на НОВОЕ сообщение (помечено >>>).
Если в нём предлагают или назначают встречу, созвон, собрание или другое мероприятие в будущем
(«кто придёт на созвон завтра?», «давайте встретимся, обсудим», «решили: созвон в пт в 19:00», «го в зум в 18»),
верни её: "title" — 1–5 слов с заглавной буквы, о чём встреча («Созвон по фронту», «Встреча команды»);
"when" — фраза времени из сообщения или контекста («завтра в 19:00», «в пт») или null; "place" — ссылка или место,
если названы, иначе null.
Не встреча: прошедшее («созвон был норм»), отказ («я не смогу»), вопрос не про встречу, шутка.
Ответь ТОЛЬКО JSON: {"event": {...}} или {"event": null}."""

FEW_SHOT = [
    ("Участники: Аня (@anya_v), Костя (@kostya)\nНовое сообщение отправлено: 2026-03-03 (вторник) 18:00\n"
     "Чекпоинт команды: не задан\nКалендарь: ср 04.03, чт 05.03, пт 06.03\n\nКонтекст:\n"
     "[1] Костя 17:58: с фронтом непонятно, надо обсудить\n\n>>> [2] Аня 18:00: давайте завтра в 19 созвонимся, кто сможет?",
     '{"event": {"title": "Созвон по фронту", "when": "завтра в 19", "place": null}}'),
    ("Участники: Аня (@anya_v), Костя (@kostya)\nНовое сообщение отправлено: 2026-03-03 (вторник) 21:00\n"
     "Чекпоинт команды: не задан\nКалендарь: ср 04.03, чт 05.03, пт 06.03\n\nКонтекст: нет\n\n"
     ">>> [3] Костя 21:00: вчерашний созвон был полезный, спасибо",
     '{"event": null}'),
]

_HHMM = re.compile(r"(?<![\d.])(\d{1,2}):(\d{2})(?!\d)|(?<![а-я])в\s+(\d{1,2})(?:[.:](\d{2}))?"
                   r"(?:\s*(?:час\w*|ч)(?![а-я]))?(?:\s+(утра|дня|вечера|ночи))?(?![\d.])")


def parse_time(text: str | None) -> time | None:
    """«19:00», «в 19.30», «в 6 вечера», «в 6» (без уточнения 1–7 — это вечер: рабочие встречи не в 6 утра)."""
    m = _HHMM.search((text or "").lower())
    if not m:
        return None
    if m.group(1):
        h, mi, part = int(m.group(1)), int(m.group(2)), None
    else:
        h, mi, part = int(m.group(3)), int(m.group(4) or 0), m.group(5)
    if part in ("дня", "вечера") and h < 12 or part is None and 1 <= h <= 7:
        h += 12
    if part == "ночи" and h == 12:
        h = 0
    return time(h, mi) if 0 <= h <= 23 and 0 <= mi <= 59 else None


def parse_when(when: str | None, msg_dt: datetime, checkpoint: date | None = None) -> tuple[date | None, time | None]:
    """День и время встречи. Время без дня — ближайшее: сегодня, если ещё не прошло, иначе завтра."""
    if not when:
        return None, None
    t = parse_time(when)
    day, precision = resolve_deadline(re.sub(r"\d{1,2}:\d{2}", " ", when), msg_dt, checkpoint)
    day = day if precision == "day" else None
    if t and not day:
        day = msg_dt.date() if t > msg_dt.time() else msg_dt.date() + timedelta(days=1)
    return day, t


@dataclass
class EventInfo:
    title: str
    when: str | None
    place: str | None


async def detect_event(client: GigaChatClient, model: str, members: list[dict], context: list[ChatMessage],
                       target: ChatMessage, checkpoint: date | None = None) -> EventInfo | None:
    messages = [{"role": "system", "content": SYSTEM}]
    for u, a in FEW_SHOT:
        messages += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    messages.append({"role": "user", "content": build_user_prompt(members, context, target, checkpoint)})
    res = await client.chat(messages, model=model, max_tokens=200)
    try:
        ev = _parse_json(res.content).get("event")
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(ev, dict) or not str(ev.get("title") or "").strip():
        return None
    title = str(ev["title"]).strip()
    return EventInfo(title=title[:1].upper() + title[1:120], when=(ev.get("when") or None),
                     place=(str(ev["place"]).strip()[:255] if ev.get("place") else None))
