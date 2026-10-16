"""Карточка договорённости в чате: текст, кнопки, разбор исправления."""

from datetime import date, datetime, timedelta, timezone
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..llm.dates import resolve_deadline

MSK = timezone(timedelta(hours=3))
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def fmt_deadline(d: date | str | None, text: str | None = None, at: str | None = None) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    if not d:
        return f"срок: «{escape(text)}»" if text else "без срока"
    return f"до {WD[d.weekday()]} {d:%d.%m}" + (f", {at}" if at else "")


def deadline_line(p: dict) -> str:
    """Срок для карточки: время, «🔥 срочно» и предупреждение, если дата уже прошла («до 10 октября» 16-го)."""
    s = fmt_deadline(p.get("deadline"), p.get("deadline_text"), p.get("deadline_time"))
    if p.get("deadline") and date.fromisoformat(str(p["deadline"])) < datetime.now(MSK).date():
        s += " ⚠️ дата уже прошла"
    return ("🔥 срочно, " if p.get("urgent") else "") + s


def msg_link(chat_id: int, message_id: int) -> str | None:
    """Ссылка на сообщение в супергруппе: t.me/c/<id без -100>/<message_id> (открывается у участников чата)."""
    s = str(chat_id)
    return f"https://t.me/c/{s[4:]}/{message_id}" if s.startswith("-100") else None


def mention(name: str, user_id: int | None) -> str:
    return f'<a href="tg://user?id={user_id}">{escape(name)}</a>' if user_id else escape(name)


def cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def card_text(p: dict) -> str:
    """Кто, что, срок — одной строкой. Цитата не нужна: карточка отвечает на само сообщение."""
    mark = " ✏️" if p.get("edited") else ""
    lines = [f"🤝 <b>{escape(p['assignee_name'])}</b>: {escape(cap(p['task']))} — {deadline_line(p)}{mark}"]
    for q in (p.get("context_quotes") or [])[-1:]:  # просьба, на которую ответили «ок, сделаю»
        lines.append(f"↳ <i>«{escape(_short(q['text'], 90))}»</i> — {escape(q['author'])}")
    return "\n".join(lines)


def card_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да", callback_data=f"y|{key}"),
        InlineKeyboardButton(text="✏️ Исправить", callback_data=f"e|{key}"),
        InlineKeyboardButton(text="✖️ Не задача", callback_data=f"n|{key}"),
    ]])


def reschedule_text(p: dict) -> str:
    return (f"🔁 <b>{escape(p['assignee_name'])}</b>: {escape(cap(p['task']))} — {fmt_deadline(p.get('old_deadline'))} → "
            f"<b>{fmt_deadline(p.get('deadline'), p.get('deadline_text'))}</b>?")


OUTAGE = {
    "worker": "⏳ Перезапускаюсь — сообщения не потеряются, разберу их через минуту-другую.",
    "llm": "⏳ GigaChat сейчас не отвечает — сообщения не потеряются, разберу, как только он вернётся.",
    "slow": "⏳ Много сообщений — разбираю с задержкой, ничего не потеряется.",
}
RECOVERED = "✅ Снова работаю — все сообщения разобраны."


def admin_howto(bot_username: str) -> str:
    return ("<b>Сделайте меня администратором</b> — иначе я не вижу сообщения и не могу убирать свои карточки:\n"
            "1. Откройте профиль группы — нажмите на её название вверху.\n"
            "2. «Изменить» (✏️; на компьютере — «Управление группой») → «Администраторы» → «Добавить администратора».\n"
            f"3. Выберите @{bot_username}, оставьте право «Удаление сообщений» → «Сохранить».")


RSVP = {"yes": "✅", "maybe": "🤔", "no": "✖️"}


def fmt_when(day: date | None, starts_at: datetime | None, when_text: str | None = None) -> str:
    if starts_at:
        t = starts_at.astimezone(MSK)
        return f"{WD[t.weekday()]} {t:%d.%m}, {t:%H:%M}"
    if day:
        return f"{WD[day.weekday()]} {day:%d.%m}, время не назначено"
    return f"«{escape(when_text)}»" if when_text else "когда — пока не решили"


def event_text(ev, people: list) -> str:
    """📅 Созвон по фронту · пт 17.10, 19:00 / ✅ Егор, blueur · 🤔 Маша."""
    lines = [f"📅 <b>{escape(ev.title)}</b> · {fmt_when(ev.day, ev.starts_at, ev.when_text)}"]
    if ev.place:
        lines.append(f"📍 {escape(ev.place)}")
    groups = [f"{mark} " + ", ".join(escape(a.name) for a in people if a.status == st)
              for st, mark in RSVP.items() if any(a.status == st for a in people)]
    if groups:
        lines.append(" · ".join(groups))
    return "\n".join(lines)


def event_keyboard(event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Буду", callback_data=f"v|{event_id}|yes"),
         InlineKeyboardButton(text="🤔 Может", callback_data=f"v|{event_id}|maybe"),
         InlineKeyboardButton(text="✖️ Не смогу", callback_data=f"v|{event_id}|no")],
        [InlineKeyboardButton(text="Это не встреча", callback_data=f"v|{event_id}|x")],
    ])


def handover_text(p: dict) -> str:
    return f"🔁 <b>{escape(p['old_name'])}</b> → <b>{escape(p['new_name'])}</b>: {escape(cap(p['task']))}?"


def handover_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Передать", callback_data=f"h|{key}|y"),
        InlineKeyboardButton(text="Нет", callback_data=f"h|{key}|n"),
    ]])


def cancel_text(p: dict) -> str:
    if p.get("event_id"):
        return f"🗑 Отменить встречу «{escape(p['task'])}»?"
    return f"🗑 «{escape(cap(p['task']))}» ({escape(p['assignee_name'])}) больше не нужна?"


def cancel_keyboard(key: str, event: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Отменить встречу" if event else "🗑 Удалить", callback_data=f"cx|{key}|y"),
        InlineKeyboardButton(text="Оставить", callback_data=f"cx|{key}|n"),
    ]])


def delete_text(t) -> str:
    return f"🗑 Удалить задачу #{t.id} «{escape(cap(t.title))}» ({escape(t.assignee_name)})?"


def delete_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del|{task_id}|y"),
        InlineKeyboardButton(text="Отмена", callback_data=f"del|{task_id}|n"),
    ]])


def done_text(p: dict) -> str:
    return f"✅ <b>{escape(p['assignee_name'])}</b>: {escape(cap(p['task']))} — готово?"


def done_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Готово", callback_data=f"f|{key}"),
        InlineKeyboardButton(text="Ещё нет", callback_data=f"x|{key}"),
    ]])


def reschedule_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Перенести", callback_data=f"r|{key}"),
        InlineKeyboardButton(text="Оставить", callback_data=f"k|{key}"),
    ]])


def edit_hint(name: str, user_id: int, username: str | None) -> str:
    # обращение к нажавшему обязательно: ForceReply(selective) открывает поле ответа только упомянутым
    who = f"@{escape(username)}" if username else mention(name, user_id)
    return f"✏️ {who}, что поправить?"


EDIT_PLACEHOLDER = "не Егор, а Андрей · срок — сб"


def open_card_text(p: dict) -> str:
    items = p["items"]

    def line(it: dict) -> str:
        dl = f" — {fmt_deadline(it.get('deadline'), it.get('deadline_text'))}" if it.get("deadline") or it.get("deadline_text") else ""
        who = f" · ✅ {escape(it['claimed_by'])}" if it.get("claimed_by") else ""
        return f"{escape(cap(it['task']))}{dl}{who}"

    if len(items) == 1:
        lines = [f"📋 Кто возьмёт: <b>{line(items[0])}</b>?"]
    else:
        lines = ["📋 <b>Кто возьмёт?</b>"] + [f"{i}. {line(it)}" for i, it in enumerate(items, 1)]
    lines += [f"ℹ️ {n}" for n in p.get("notes") or []]  # уже готовый HTML (экранирован при создании)
    if all(it.get("claimed_by") for it in items):
        lines.append("Все задачи разобраны 🎉")
    return "\n".join(lines)


def open_card_keyboard(p: dict) -> InlineKeyboardMarkup | None:
    rows = []
    for i, it in enumerate(p["items"]):
        if not it.get("claimed_by"):
            label = "🙋 Беру" if sum(not x.get("claimed_by") for x in p["items"]) == 1 else f"🙋 {i + 1}. {cap(it['task'])[:28]}"
            rows.append([InlineKeyboardButton(text=label, callback_data=f"t|{p['key']}|{i}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


DEADLINE_CHOICES = [("Сегодня", "сегодня"), ("Завтра", "завтра"), ("До пт", "к пт"), ("На выходных", "на выходных"),
                    ("Без срока", "-")]


def deadline_ask_text(name: str, task: str, user_id: int | None = None) -> str:
    return f"⏰ {mention(name, user_id)}, к какому сроку «{escape(cap(task))}»?"


def _choice_label(title: str, phrase: str, now: datetime) -> str:
    """«До пт» в пятницу — это следующая пятница: пишем дату прямо на кнопке («До пт 23.10»)."""
    if phrase in ("к пт", "на выходных"):
        d, _ = resolve_deadline(phrase, now)
        return f"До {WD[d.weekday()]} {d:%d.%m}"
    return title


def deadline_ask_keyboard(task_id: int, now: datetime | None = None) -> InlineKeyboardMarkup:
    now = now or datetime.now(MSK)
    labels = [_choice_label(t, ph, now) for t, ph in DEADLINE_CHOICES]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=labels[i], callback_data=f"d|{task_id}|{i}") for i in range(3)],
        [InlineKeyboardButton(text=labels[i], callback_data=f"d|{task_id}|{i}") for i in range(3, len(labels))],
    ])


def apply_edit(p: dict, text: str, members: list[dict], now: datetime, checkpoint: date | None) -> dict:
    parts = [x.strip() for x in text.split(";")]
    parts += [""] * (3 - len(parts))
    who, what, when = parts[:3]
    p = dict(p)
    if who:
        n = who.lstrip("@").lower()
        m = next((m for m in members if n in (m["name"].lower(), (m.get("username") or "").lower())
                  or m["name"].lower().startswith(n)), None)
        p["assignee_name"] = m["name"] if m else who
        p["assignee_user_id"] = m["user_id"] if m else None
    if what:
        p["task"] = what
    if when == "-":
        p["deadline"], p["deadline_text"] = None, None
    elif when:
        d, _ = resolve_deadline(when, now, checkpoint)
        p["deadline"], p["deadline_text"] = (d.isoformat() if d else None), when
    p["edited"] = True
    return p
