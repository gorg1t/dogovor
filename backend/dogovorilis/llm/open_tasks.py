"""Открытые задачи: «Кто сделает фронт?», «Кто что берёт: фронт, бэк, преза?» → карточка с кнопками «Беру».

Отдельный вызов модели, только для сообщений с признаками «ищем исполнителя» — основной промпт извлечения
договорённостей (проверенный в RAT) не меняется.
"""

import json
import re
from dataclasses import dataclass
from datetime import date

from .dates import resolve_deadline, split_deadline
from .extractor import ChatMessage, _fmt_msg, _parse_json, build_user_prompt
from .gigachat import GigaChatClient
from .prefilter import PROMISE

VOLUNTEER_CALL = re.compile(
    r"(?<![а-яё])(кто\s+(сделает|сделаешь|возьм\w*|займ\w*|может|готов|сможет|хочет|бер[её]т|за\s|из\s+вас|"
    r"что\s+бер[её]т|чем\s+займ\w*)|кто[-\s]?(нибудь|нить|либо)|кто-то\s+(может|возьм\w*|сделает)|"
    r"нуж(ен|на|ны)\s+(кто|люди|человек|помощь)|распредел\w*|разбер[её]м\s+задачи|"
    r"свободн\w*\s+задач\w*|кому\s+(дать|отдать|отдадим))", re.IGNORECASE)
TASK_LIST = re.compile(r"(?<![а-яё])(нужно|надо|осталось|задачи|таски|туду|todo|список)(?![а-яё])[^\n]{0,40}"
                       r"(:|\n\s*(\d+[.)]|[-•—]))", re.IGNORECASE)


# просьба ко всем: «доделайте бт по мл», «ребят, гляньте пр» — повелительное мн. ч. в начале, без адресата
GROUP_REQUEST = re.compile(r"^(?:(?:ребят\w*|народ|так|ну|плиз|пж|пожалуйста|всем привет)[\s,!.]+)*"
                           r"(?!(?:извините|простите|держите|смотрите|представьте|знаете|видите|давайте)\b)"
                           r"[а-я]+(?:айте|яйте|ейте|уйте|ьте|ите)\b")


# «кто делает фронт?», «кто обучит модель?», «кто-то напишет тесты?» — любой глагол 3-го лица после «кто»
WHO_VERB = re.compile(r"(?<![а-я])кто(?:[-\s](?:то|нибудь|нить|либо))?\s+(?:у\s+нас\s+|же\s+|вообще\s+)?"
                      r"(?!(?:знает|помнит|понимает|шарит|идет|придет|будет|есть|спит|сидит|видел)\b)"
                      r"[а-я]+(?:ет|ит|ут|ют|ят|ат)\b")
# «нужно сделать мем», «нужен деплой на gh pages» — потребность без исполнителя
NEED = re.compile(r"^(?:(?:ребят\w*|народ|так|ну|кстати)[\s,!.]+)*(?:нужно|надо|нужен|нужна|нужны)\s+"
                  r"(?!(?:ли|подумать|обсудить|решить|понять)\b)")


def is_volunteer_call(text: str) -> bool:
    t = (text or "").lower().replace("ё", "е").strip()
    return bool(VOLUNTEER_CALL.search(t) or TASK_LIST.search(t) or GROUP_REQUEST.search(t)
                or WHO_VERB.search(t) or NEED.search(t))


_ME = re.compile(r"(?<![а-яa-z])(я|мне|меня|мной|сам|сама)(?![а-яa-z])")


def impersonal_need(text: str) -> bool:
    """«нужно сделать отчёт до пятницы» — потребность без исполнителя. По разметке (класс vague) это не обещание
    автора, а свободная задача; «нужно сделать отчёт, сделаю к пт» — обещание от первого лица всё же есть."""
    t = (text or "").lower().replace("ё", "е").strip()
    if not NEED.search(t) or _ME.search(t):
        return False
    # инфинитив («сделать», «подготовить») — часть «нужно …»; любое другое слово-обещание («сделаю», «могу») — нет
    return all(m.group(0).endswith(("ть", "ти", "чь")) for m in PROMISE.finditer(t))


_FILLER = {"кто", "что", "это", "эту", "его", "ее", "их", "тогда", "так", "ну", "ребят", "ребята", "народ", "плиз",
           "пж", "пожалуйста", "срочно", "задачу", "задачи", "таск", "таску", "таски", "сам", "на", "себя"}


_ACCEPT = {"я", "сделаю", "беру", "возьму", "возьмусь", "займусь", "давай", "давайте", "ок", "окей", "го", "могу",
           "мне", "на", "себя", "тогда", "ну", "ща", "щас", "сейчас", "это", "его", "ее", "к", "до", "плюс", "готов",
           "готова", "сегодня", "завтра", "послезавтра", "пн", "вт", "ср", "чт", "пт", "сб", "вс", "выходных", "вечеру",
           "гляну", "посмотрю", "вечером", "утром", "днем", "попозже", "позже"}


def is_bare_accept(text: str) -> bool:
    """«я сделаю», «беру», «давай я к чт» — согласие без названия задачи."""
    words = re.findall(r"[а-яa-z0-9+]+", (text or "").lower().replace("ё", "е"))
    return bool(words) and all(w in _ACCEPT or w.isdigit() for w in words)


def latest_call_item(open_cards: list[dict], before_message_id: int) -> tuple[dict, dict] | None:
    """Последняя карточка «Кто возьмёт?» до сообщения, если в ней ровно один свободный пункт."""
    cards = [p for p in open_cards if p.get("type") == "open" and p["message_id"] < before_message_id]
    if not cards:
        return None
    card = max(cards, key=lambda p: p["message_id"])
    free = [it for it in card["items"] if not it.get("claimed_by")]
    return (card, free[0]) if len(free) == 1 else None


def is_bare_call(text: str) -> bool:
    """«кто возьмёт?», «ну кто сделает это?» — задача не названа, речь о деле из предыдущих сообщений."""
    t = WHO_VERB.sub(" ", VOLUNTEER_CALL.sub(" ", (text or "").lower().replace("ё", "е")))
    return not [w for w in re.findall(r"[а-яa-z0-9#+]+", t) if w not in _FILLER and len(w) > 1]


SYSTEM = """Ты помогаешь команде в Telegram распределять задачи. Посмотри на НОВОЕ сообщение (помечено >>>).
Если автор ищет, кто возьмёт задачи или кто их делает («кто сделает фронт?», «кто делает бэк?», «нужно сделать мем»,
«кто что берёт: фронт, бэк, преза?», список задач на распределение), верни эти задачи. Даже если задачу, о которой
спрашивают, уже кто-то взял, — верни её: бот сам проверит, взята ли она.

Если новое сообщение — короткий вопрос («кто сделает?», «кто возьмёт?»), задача — то, о чём говорили прямо перед ним
(последнее сообщение с делом), а не всё подряд из контекста.

Не возвращай:
- задачи из контекста, о которых в новом сообщении не спрашивают;
- задачи, которые автор берёт сам («бэк я сделаю») или поручает конкретному человеку («Костя, сделай фронт»);
- то, что уже сделано, шутки, вопросы не про работу («кто на обед?», «кто видел мой зарядник?»).

Перечисление через запятую или «и» — это отдельные задачи: «кто возьмёт фронт и вёрстку писем?» → «сделать фронт»
и «сделать вёрстку писем».

Для каждой задачи: "task" — 3–8 слов, с глагола («сделать фронт»); "deadline_text" — фраза срока из сообщения
или null. Ответь ТОЛЬКО JSON: {"open_tasks": [...]} или {"open_tasks": []}."""

FEW_SHOT = [
    ("Участники: Аня (@anya_v), Костя (@kostya), Лёша (@lesha_p)\n"
     "Новое сообщение отправлено: 2026-03-03 (вторник) 18:00\nЧекпоинт команды: 2026-03-10\n"
     "Календарь: ср 04.03, чт 05.03, пт 06.03, сб 07.03, вс 08.03, пн 09.03, вт 10.03\n\nКонтекст: нет\n\n"
     ">>> [1] Аня 18:00: так, на неделю: фронт, бэк и презу к КТ. отчёт я сама напишу. кто что берёт?",
     '{"open_tasks": [{"task": "сделать фронт", "deadline_text": null}, '
     '{"task": "сделать бэк", "deadline_text": null}, {"task": "сделать презентацию", "deadline_text": "к КТ"}]}'),
    ("Участники: Аня (@anya_v), Костя (@kostya), Лёша (@lesha_p)\n"
     "Новое сообщение отправлено: 2026-03-03 (вторник) 13:00\nЧекпоинт команды: 2026-03-10\n"
     "Календарь: ср 04.03, чт 05.03, пт 06.03, сб 07.03, вс 08.03, пн 09.03, вт 10.03\n\nКонтекст: нет\n\n"
     ">>> [2] Лёша 13:00: кто-нибудь идёт на обед?",
     '{"open_tasks": []}'),
]


def split_enumeration(task: str) -> list[str]:
    """«сделать фронт и вёрстку писем» → ["сделать фронт", "сделать вёрстку писем"] (модель иногда склеивает)."""
    parts = [x.strip() for x in re.split(r",\s*|\s+и\s+", task) if x.strip()]
    if len(parts) < 2 or any(len(x) < 2 for x in parts):
        return [task]
    verb = parts[0].split()[0] if len(parts[0].split()) > 1 else ""
    out = [parts[0]] + [x if not verb or x.split()[0].endswith(("ть", "ти", "чь")) else f"{verb} {x}" for x in parts[1:]]
    return out


@dataclass
class OpenTask:
    task: str
    deadline_text: str | None
    deadline: date | None


async def detect_open_tasks(client: GigaChatClient, model: str, members: list[dict], context: list[ChatMessage],
                            target: ChatMessage, checkpoint: date | None = None) -> list[OpenTask]:
    messages = [{"role": "system", "content": SYSTEM}]
    for u, a in FEW_SHOT:
        messages += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
    messages.append({"role": "user", "content": build_user_prompt(members, context, target, checkpoint)})
    res = await client.chat(messages, model=model, max_tokens=400)
    try:
        data = _parse_json(res.content)
    except (ValueError, json.JSONDecodeError):
        return []
    out = []
    for t in data.get("open_tasks") or []:
        if isinstance(t, str):  # модель иногда отвечает списком строк: ["интеграция с jira"]
            t = {"task": t}
        if not isinstance(t, dict) or not t.get("task"):
            continue
        dl_text = t.get("deadline_text") or None
        d, prec = resolve_deadline(dl_text, target.ts, checkpoint)
        for task in split_enumeration(str(t["task"]).strip()):
            title, day, phrase = split_deadline(task, target.ts, checkpoint)  # «…домен до 20 октября»
            task = title if day else task
            if all(task.lower() != o.task.lower() for o in out):
                out.append(OpenTask(task=task, deadline_text=dl_text or phrase,
                                    deadline=(d if prec == "day" else None) or day))
    return out[:8]
