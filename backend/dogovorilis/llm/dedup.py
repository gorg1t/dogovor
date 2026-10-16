"""Подавление повторных карточек: просьба и согласие, назначение и «ок» — это одна договорённость.

Если та же задача того же человека пришла с другим сроком — это перенос: карточку не глушим,
а предлагаем поменять срок.
"""

import re


# сравниваем задачи по предмету, а не по глаголу: «настроить CI» ≠ «настроить аналитику»,
# зато «подготовить презентацию» = «сделать презентацию»
_STOP = {"для", "на", "по", "из", "от", "до", "за", "про", "все", "это", "задачу", "задача",
         "нужно", "надо", "часть", "немного", "the", "a", "an", "to", "by", "for", "of", "on", "in", "and", "with",
         "my", "it", "will", "ll", "until", "before"}
_NOUNS_T = {"сеть", "путь", "память", "печать"}  # существительные на «-ть», не глаголы


def _is_verb(w: str) -> bool:
    if w in _NOUNS_T or w.endswith("сть"):  # «безопасность», «отчётность»
        return False
    return w.endswith(("ть", "ти", "чь", "ться"))


# одно и то же разными словами: «баг с логином» = «баг с авторизацией», «пролонгировать» = «продление»
_SYN = {"логин": "автор", "вход": "автор", "входа": "автор", "auth": "автор", "login": "автор", "signi": "автор",
        "проло": "продл", "front": "фронт", "бэкен": "бэк", "бекен": "бэк", "бек": "бэк", "backe": "бэк", "back": "бэк",
        "преза": "презе", "презу": "презе", "презы": "презе", "слайд": "презе", "ошибк": "баг", "бага": "баг",
        "багу": "баг", "баги": "баг", "bug": "баг", "datab": "база", "бд": "база", "базу": "база", "базы": "база",
        "доки": "докум", "docs": "докум", "deplo": "депло", "выкат": "депло", "тесты": "тест", "тести": "тест",
        "tests": "тест", "пулл": "пр", "рекве": "пр", "pull": "пр", "pr": "пр", "пайпл": "ci", "pipel": "ci",
        "domai": "домен", "landi": "ленди"}
# даты — не предмет задачи: «…домен до 20 октября» и «продление домена» — одно и то же
_DATEISH = {"январ", "февра", "март", "марта", "апрел", "мая", "май", "июня", "июнь", "июля", "июль", "авгус", "сентя",
            "октяб", "ноябр", "декаб", "понед", "вторн", "среды", "среду", "среда", "четве", "пятни", "суббо", "воскр",
            "пн", "вт", "ср", "чт", "пт", "сб", "вс", "завтр", "сегод", "послз", "недел", "месяц", "числа", "числу"}


# отглагольные «действия» в начале названия: «настройка …», «доработка …», «подготовка …»
_ACTIONS = {"настр", "разра", "дораб", "подго", "созда", "напис", "обнов", "испра", "прове", "почин", "оформ", "довед"}


def _object_list(s: str) -> list[str]:
    """Предмет задачи по порядку: первое слово — главное («тексты для лендинга» → тексты, лендинг)."""
    words = re.findall(r"[а-яёa-z0-9#+]+", s.lower().replace("ё", "е"))
    stems = [_SYN.get(w[:5], w[:5]) for w in words if len(w) >= 2 and w not in _STOP and not _is_verb(w)]
    return list(dict.fromkeys(x for x in stems if x not in _DATEISH and not x.isdigit()))


def _object_tokens(s: str) -> set[str]:
    return set(_object_list(s))


def find_duplicate(task: str, assignee: str, source_ids: list[int], target_id: int, recent: list[dict],
                   threshold: float = 0.5) -> dict | None:
    """recent — карточки чата за сутки: [{"task", "assignee_name", "message_id", "status"}], от старых к новым."""
    a = _object_tokens(task)
    earlier_sources = set(source_ids or []) - {target_id}
    for p in reversed(recent):
        if p.get("status") == "rejected" or p["assignee_name"] != assignee:
            continue
        if p.get("message_id") in earlier_sources:
            return p
        b = _object_tokens(p["task"])
        # похожи, или предмет одной целиком внутри другой: «ревью» и «ревью пулл-реквеста» того же человека
        if a and b and (len(a & b) / len(a | b) >= threshold or a <= b or b <= a):
            return p
    return None


def classify(task: str, assignee: str, deadline: str | None, source_ids: list[int], target_id: int,
             recent: list[dict]) -> tuple[str, dict | None]:
    """new — новая карточка; duplicate — подавить; reschedule — та же задача с новым сроком."""
    dup = find_duplicate(task, assignee, source_ids, target_id, recent)
    if dup is None:
        return "new", None
    if deadline and deadline != dup.get("deadline"):
        return "reschedule", dup
    return "duplicate", dup


def is_duplicate(task: str, assignee: str, recent: list[dict], threshold: float = 0.5) -> bool:
    return find_duplicate(task, assignee, [], -1, recent, threshold) is not None


def expand_recent(recent: list[dict]) -> list[dict]:
    """Карточки «Кто возьмёт?» разворачиваем во взятые пункты, чтобы «беру фронт» после кнопки не стало дублем."""
    out = []
    for p in recent:
        if p.get("type") == "open":
            for it in p.get("items", []):
                if it.get("claimed_by"):
                    out.append({"task": it["task"], "assignee_name": it["claimed_by"], "message_id": p["message_id"],
                                "deadline": it.get("deadline"), "status": "confirmed", "task_id": it.get("task_id")})
        elif p.get("assignee_name") and p.get("type") not in ("done", "cancel", "handover"):  # служебные карточки
            out.append(p)
    return out


def match_open_item(task: str, open_cards: list[dict], threshold: float = 0.34) -> tuple[dict, int] | None:
    """Свободный пункт карточки «Кто возьмёт?», похожий на задачу (для «беру фронт» текстом)."""
    a = _object_tokens(task)
    best = None
    for p in open_cards:
        for i, it in enumerate(p.get("items", [])):
            if it.get("claimed_by"):
                continue
            b = _object_tokens(it["task"])
            score = len(a & b) / len(a | b) if a and b else 0
            if a and b and (a <= b or b <= a):  # «продление домена» ⊂ «пролонгировать домен»
                score = max(score, 0.9)
            if score >= threshold and (best is None or score > best[0]):
                best = (score, p, i)
    return (best[1], best[2]) if best else None


def same_task(a: str, b: str) -> bool:
    """Одна задача: то же главное слово («тексты» = «тексты для лендинга») или почти тот же предмет.
    Общее второстепенное слово — не повод: «сделать лендинг» ≠ «сделать дизайн лендинга»."""
    la, lb = _object_list(a), _object_list(b)
    # «настройка аналитики» = «настроить аналитику»: отглагольное «настройка» — действие, а не предмет
    acts = _ACTIONS | {w[:5] for w in re.findall(r"[а-яё]+", f"{a} {b}".lower().replace("ё", "е")) if _is_verb(w)}
    la = la[1:] if len(la) > 1 and la[0] in acts else la
    lb = lb[1:] if len(lb) > 1 and lb[0] in acts else lb
    if not la or not lb:
        return False
    ta, tb = set(la), set(lb)
    overlap = len(ta & tb) / len(ta | tb)
    return ta == tb or (la[0] == lb[0] and overlap >= 0.34) or overlap >= 0.67


def triage_open_items(items: list[str], recent: list[dict]) -> list[tuple[str, str, dict | None]]:
    """Для каждого найденного пункта «Кто возьмёт?»: ("new" | "taken" | "offered", пункт, что нашли).

    taken — такую задачу уже взяли (кнопкой, текстом или она ждёт «Да»); offered — уже висит свободной
    в другой карточке «Кто возьмёт?». Так вопрос «кто сделает?» не предлагает заново уже занятое.
    """
    taken = [p for p in expand_recent(recent) if p.get("status") != "rejected"]
    free = [(p, it) for p in recent if p.get("type") == "open" for it in p.get("items", []) if not it.get("claimed_by")]
    out = []
    for task in items:
        hit = next((p for p in reversed(taken) if same_task(task, p["task"])), None)
        if hit:
            out.append(("taken", task, hit))
            continue
        card = next((p for p, it in reversed(free) if same_task(task, it["task"])), None)
        out.append(("offered", task, card) if card else ("new", task, None))
    return out


def mentioned(task: str, text: str) -> bool:
    """Задача названа в самом сообщении (а не подтянута моделью из старой переписки)."""
    # корень короче, чем в same_task: «сделать презентацию» названа в «кто возьмёт презу?»
    return bool({w[:4] for w in _object_tokens(task)} & {w[:4] for w in _object_tokens(text)})
