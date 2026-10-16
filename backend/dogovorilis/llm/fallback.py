"""Страховка, когда модель промолчала: «акт сверки сдам до 14 октября», «ща гляну пр, к вечеру апрув кину»,
«I'll fix the CI pipeline by Monday» — явное обещание правилом: глагол 1-го лица будущего времени + предмет + срок.

Только для сообщений, где модель ничего не нашла; карточку всё равно подтверждает человек («Да»).
Основной экстрактор (проверенный в RAT) не меняется.
"""

import re
from datetime import date, datetime

from .dates import resolve_deadline

VERBS = {
    "сдам": "сдать", "сделаю": "сделать", "доделаю": "доделать", "допишу": "дописать", "напишу": "написать",
    "отправлю": "отправить", "скину": "скинуть", "кину": "кинуть", "закину": "закинуть", "подготовлю": "подготовить",
    "соберу": "собрать", "гляну": "глянуть", "посмотрю": "посмотреть", "проверю": "проверить", "поправлю": "поправить",
    "исправлю": "исправить", "починю": "починить", "оформлю": "оформить", "залью": "залить", "выложу": "выложить",
    "допилю": "допилить", "запилю": "запилить", "закончу": "закончить", "пришлю": "прислать", "настрою": "настроить",
    "обновлю": "обновить", "добавлю": "добавить", "запущу": "запустить", "задеплою": "задеплоить",
    "сверстаю": "сверстать", "нарисую": "нарисовать", "протестирую": "протестировать", "распечатаю": "распечатать",
    "куплю": "купить", "позвоню": "позвонить", "подниму": "поднять", "разверну": "развернуть",
    "согласую": "согласовать", "посчитаю": "посчитать", "переведу": "перевести", "подпишу": "подписать",
    "закажу": "заказать", "смержу": "смержить", "апрувну": "апрувнуть", "отревьюю": "отревьюить", "оплачу": "оплатить",
}
_HEDGE = re.compile(r"(?<![а-я])(если|может|наверно\w*|попробую|постараюсь|не|бы|вряд ли|хотел\w*)(?![а-я])")
_WHEN = re.compile(r"(?<![а-я\d])((?:к|до|на|в)\s+(?:вечеру|утру|обеду|ночи|концу\s+\w+|выходны\w+|"
                   r"\d{1,2}(?:[./]\d{1,2})?(?:\s+[а-я]+)?|пн|вт|ср|чт|пт|сб|вс|понедельник\w*|вторник\w*|сред[уы]|"
                   r"четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)|послезавтра|завтра|сегодня|ща|щас|сейчас|вечером|"
                   r"утром)(?![а-я])")
_FILLER = {"я", "ну", "так", "ок", "ладно", "тогда", "уже", "щас", "ща", "короче", "кстати", "это", "а", "и"}
_EN = re.compile(r"\bi(?:'ll| will| am going to|'m going to)\s+(.+?)(?:\s+(?:by|until|before|on)\s+(.+?))?[.!]*$",
                 re.IGNORECASE)


def rule_promises(text: str, msg_dt: datetime, checkpoint: date | None = None) -> list[tuple[str, str | None]]:
    """[(задача, фраза срока)] — что автор явно пообещал сам."""
    out: list[tuple[str, str | None]] = []
    seen = ""  # текст до текущей части: «если успею, сделаю фронт» — оговорка в соседней части фразы
    for clause in re.split(r"[,;.!\n]+", text or ""):
        c = clause.strip()
        if not c or "?" in c:
            seen += " " + c
            continue
        en = _EN.search(c)
        if en:
            out.append((en.group(1).strip(), f"by {en.group(2)}" if en.group(2) else None))
            continue
        low = c.lower().replace("ё", "е")
        words = low.split()
        idx = next((i for i, w in enumerate(words) if w.strip("!.") in VERBS), None)
        hedged = idx is not None and _HEDGE.search((seen + " " + " ".join(words[:idx])).lower().replace("ё", "е"))
        seen += " " + c
        if idx is None or hedged:
            continue
        when = _WHEN.search(low)

        orig = c.split()  # предмет — как написали (с «ё» и регистром), фильтруем по нормализованному

        def objects(lo: int, hi: int) -> list[str]:
            cut = set(re.findall(r"[а-яa-z0-9]+", " ".join(m.group(0) for m in _WHEN.finditer(" ".join(words[lo:hi])))))
            return [o for o, w in zip(orig[lo:hi], words[lo:hi])
                    if w not in _FILLER and w not in cut and re.search(r"[a-zа-я0-9]", w)]

        obj = objects(idx + 1, len(words))
        if not obj:  # «Акт сверки сдам до 14 октября» — предмет до глагола (заглавная там — от начала фразы)
            obj = objects(0, idx)
            if obj and not obj[0].isupper():
                obj[0] = obj[0][:1].lower() + obj[0][1:]
        if not obj:
            continue
        phrase = when.group(1) if when else None
        if phrase and resolve_deadline(phrase, msg_dt, checkpoint)[1] not in ("day",):
            phrase = None
        out.append((f"{VERBS[words[idx].strip('!.')]} {' '.join(obj)}", phrase))
    return out
