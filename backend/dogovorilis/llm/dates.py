"""Детерминированный разбор сроков по правилам разметки RAT.

Модель возвращает фразу срока («к пт», «на выхах»), а дату считаем здесь:
арифметика дат — слабое место LLM, а правила разбора у нас фиксированы.
"""

import re
from datetime import date, datetime, timedelta

WEEKDAYS = [
    (0, r"пн|понедельник\w*|понед\w*"),
    (1, r"вт|вторник\w*"),
    (2, r"ср|сред\w*"),
    (3, r"чт|четверг\w*|четв\w*"),
    (4, r"пт|пятниц\w*|пятн\w*"),
    (5, r"сб|суббот\w*"),
    (6, r"вс|воскресень\w*|воскр\w*"),
]
MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7,
    "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
NUM_WORDS = {"один": 1, "одну": 1, "два": 2, "две": 2, "пару": 2, "три": 3, "четыре": 4, "пять": 5,
             "шесть": 6, "семь": 7, "неделю": 7, "недели": 7}

TODAY = re.compile(r"\b(сегодня|седня|сёдня|сення|ща|щас|щяс|сейчас|вечер\w*|до ночи|к ночи|ночью|"
                   r"через (час|пару часов|\d+ час\w*|полчаса|пол часа)|сразу|в течение дня|до обеда|к обеду)\b")
VAGUE = re.compile(r"\b(на следующей неделе|скоро|потом|как-нибудь|когда-нибудь|позже|на днях)\b")
CHECKPOINT = re.compile(r"\b(кт\d?|чекпоинт\w*|чек-поинт\w*|защит\w*|босс\w*|дедлайн\w*|контрольн\w* точк\w*)\b")


NIGHT_END_HOUR = 5


def reference_day(msg_dt: datetime) -> date:
    """«Завтра» и «к чт» в 00:44 четверга говорят ещё «из среды»: до 5 утра считаем от предыдущего дня."""
    return msg_dt.date() - timedelta(days=1) if msg_dt.hour < NIGHT_END_HOUR else msg_dt.date()


def _next_weekday(d: date, wd: int) -> date:
    """Ближайший такой день недели строго после d."""
    delta = (wd - d.weekday()) % 7
    return d + timedelta(days=delta or 7)


def _nearest_sunday(d: date) -> date:
    return d + timedelta(days=(6 - d.weekday()) % 7)


_EN_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _english(t: str, d: date, ref: date) -> tuple[date, str] | None:
    """«by Monday», «tomorrow», «eod», «end of week» — английские сроки (в командах пишут и так)."""
    if re.search(r"\b(today|tonight|eod|end of (the )?day|asap)\b", t):
        return d, "day"
    if re.search(r"\btomorrow\b", t):
        return ref + timedelta(days=1), "day"
    if re.search(r"\bend of (the )?week\b|\bweekend\b", t):
        return _nearest_sunday(ref), "day"
    m = re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", t)
    if m and re.search(r"\b(by|on|until|till|next|this)\b", t):
        return _next_weekday(ref, _EN_DAYS.index(m.group(1))), "day"
    return None


def deadline_time(text: str | None) -> str | None:
    """Время срока: «сегодня к 18:00» → 18:00, «до обеда» → 13:00, «до конца дня» → 18:00."""
    t = (text or "").lower()
    m = re.search(r"(?<![\d.])(\d{1,2}):(\d{2})(?!\d)", t)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    if re.search(r"обед", t):
        return "13:00"
    if re.search(r"конц\w* (рабочего )?дня|end of (the )?day|\beod\b", t):
        return "18:00"
    return None


URGENT = re.compile(r"(?<![а-я])(срочн\w*|asap|горит|как можно скорее|побыстрее|немедленно|прямо сейчас)(?![а-я])")

_TAIL = re.compile(r"\s+((?:к|до|на|в|by|until)\s+[^,.;]{2,40}|завтра|послезавтра|сегодня|срочно)\s*$", re.IGNORECASE)


def split_deadline(title: str, msg_dt: datetime, checkpoint: date | None = None) -> tuple[str, date | None, str | None]:
    """«Пролонгировать домен до 20 октября» → («Пролонгировать домен», 20.10, «до 20 октября»): срок — не часть названия."""
    m = _TAIL.search(title or "")
    if not m:
        return title, None, None
    day, precision = resolve_deadline(m.group(1), msg_dt, checkpoint)
    if precision != "day" or not day:
        return title, None, None
    return title[:m.start()].strip(), day, m.group(1).strip()


def resolve_deadline(text: str | None, msg_dt: datetime, checkpoint: date | None = None) -> tuple[date | None, str]:
    """Возвращает (дата, точность), точность: day | vague | none | unparsed."""
    if not text:
        return None, "none"
    t = text.lower().replace("ё", "е").strip()
    d = msg_dt.date()
    ref = reference_day(msg_dt)  # для «завтра», дней недели и «через N»; «сегодня/ща» — по фактической дате

    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else d.year
        if year < 100:
            year += 2000
        try:
            res = date(year, month, day)
        except ValueError:
            return None, "unparsed"
        if not m.group(3) and res < d - timedelta(days=60):
            res = res.replace(year=res.year + 1)
        return res, "day"
    m = re.search(r"\b(\d{1,2})\s+(январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*", t)
    if m:
        key = m.group(2)[:-1] if m.group(2).startswith("ма") else m.group(2)
        try:
            return date(d.year, MONTHS[key], int(m.group(1))), "day"
        except ValueError:
            return None, "unparsed"

    m = re.search(r"\b(\d{1,2})\s*-?(го|ое|е)\b", t)  # «к 21му», «до 21го»
    if m and 1 <= int(m.group(1)) <= 31:
        day = int(m.group(1))
        try:
            res = date(d.year, d.month, day)
            if res < d:
                res = date(d.year + (d.month == 12), d.month % 12 + 1, day)
            return res, "day"
        except ValueError:
            pass

    m = re.search(r"\b(\d{1,2})\s+числ\w*", t)  # «до 25 числа» — этого месяца, а если уже прошло — следующего
    if m and 1 <= int(m.group(1)) <= 31:
        day = int(m.group(1))
        try:
            res = date(d.year, d.month, day)
            return (res if res >= d else date(d.year + (d.month == 12), d.month % 12 + 1, day)), "day"
        except ValueError:
            pass

    if CHECKPOINT.search(t):
        return (checkpoint, "day") if checkpoint else (None, "unparsed")
    if re.search(r"конц\w* месяц\w*|end of (the )?month", t):  # «к концу месяца» — последний день месяца
        nxt = date(ref.year + (ref.month == 12), ref.month % 12 + 1, 1)
        return nxt - timedelta(days=1), "day"
    if re.search(r"конц\w* следующ\w* недел\w*|end of next week", t):
        return _nearest_sunday(ref) + timedelta(days=7), "day"
    en = _english(t, d, ref)
    if en:
        return en
    if re.search(r"\bпослезавтр\w*", t):
        return ref + timedelta(days=2), "day"
    if re.search(r"\bзавтр\w*|\bк утру\b|\bдо утра\b", t):
        if re.search(r"к утру|до утра", t) and msg_dt.hour < 6 and "завтр" not in t:
            return d, "day"
        return ref + timedelta(days=1), "day"
    m = re.search(r"через\s+(\d+|" + "|".join(NUM_WORDS) + r")\s*(дн\w*|день|недел\w*)?", t)
    if m and m.group(2):
        n = int(m.group(1)) if m.group(1).isdigit() else NUM_WORDS[m.group(1)]
        if m.group(2).startswith("недел"):
            n = 7 if m.group(1) in ("неделю", "недели") else n * 7
        return ref + timedelta(days=n), "day"
    if re.search(r"через неделю", t):
        return ref + timedelta(days=7), "day"
    if re.search(r"выходн\w*|выхах|выхи|выхам|конц\w* недели|конец недели", t):
        return _nearest_sunday(ref), "day"
    for wd, pat in WEEKDAYS:
        if re.search(rf"\b({pat})\b", t):
            return _next_weekday(ref, wd), "day"
    if TODAY.search(t):
        return d, "day"
    if VAGUE.search(t):
        return None, "vague"
    return None, "unparsed"
