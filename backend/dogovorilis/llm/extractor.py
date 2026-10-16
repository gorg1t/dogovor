"""Извлечение договорённостей «кто, что, к какому сроку» из нового сообщения чата с помощью LLM."""

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .dates import resolve_deadline
from .gigachat import ChatResult, GigaChatClient
from .names import find_member

PROMPT_VERSION = "v2"

WEEKDAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAY_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

SYSTEM_PROMPT_V1 = """Ты — помощник студенческой команды в Telegram. Ты читаешь рабочий чат и замечаешь моменты, \
когда кто-то из участников берёт на себя задачу. Твоя работа — найти договорённости, которые ВОЗНИКЛИ \
в НОВОМ сообщении (оно помечено >>>). Предыдущие сообщения — только контекст.

Договорённость есть, если в новом сообщении:
1) участник сам обещает что-то сделать: «ща сделаю макет», «беру бота», «го я», «давай я», «на мне», \
«запилю к пн», «скину вечерком», «не успею к пт, сделаю к пн» (перенос — это новое обещание);
2) участнику прямо назначают задачу без вопроса: «Егор — на тебе презентация к пт», «Маша, сделай опрос до завтра»;
3) участник соглашается на просьбу из контекста: «Егор, сделаешь презу?» → новое сообщение Егора «ок» / «+» / «да, к пт будет». \
Тогда ответственный — тот, кто согласился, задача берётся из просьбы.

НЕ договорённость:
- вопрос или просьба без согласия («кто сделает презу?», «Егор, сможешь глянуть?»);
- уже сделанное («сделал», «скинул») и статус работы («делаю», «ковыряю»);
- условное или неуверенное («если надо, могу», «попробую, не обещаю»);
- отказ («не успею», «я пас», «снимите с меня»);
- пересказ чужих планов («Петя вроде делает», «препод сказал сделать к пт»);
- общее «надо бы кому-то…», «нам нужно…»;
- шутки и сарказм; «+» или «ок» как реакция на шутку или новость, а не на просьбу;
- решения команды («решили делать на React», «созвон в чт в 19») — это не задачи.

Для каждой договорённости верни:
- "assignee": имя ответственного ТОЧНО как в списке участников;
- "task": коротко, что сделать (3–8 слов, инфинитив: «сделать презентацию»);
- "deadline_text": фраза срока как в чате («к пт», «завтра», «на выхах», «к КТ») или null;
- "deadline": дата срока ГГГГ-ММ-ДД по календарю ниже или null;
- "kind": "promise" | "assignment" | "accepted_request";
- "source_ids": номера сообщений, из которых сложилась договорённость.

Правила сроков: день недели — ближайший такой день ПОСЛЕ даты сообщения; «на выходных» — ближайшее воскресенье; \
«сегодня», «ща», «вечером» — дата сообщения; «к КТ», «к защите» — дата чекпоинта; «на следующей неделе», «скоро» — null.

Ответь ТОЛЬКО JSON без пояснений: {"tasks": [...]}. Если договорённостей нет: {"tasks": []}."""

FEW_SHOT_V1 = [
    (
        "Участники: Аня (@anya_v), Костя (@kostya), Лёша (@lesha_p)\n"
        "Новое сообщение отправлено: 2026-03-03 (вторник) 18:10\nЧекпоинт команды: 2026-03-10\n"
        "Календарь: ср 04.03, чт 05.03, пт 06.03, сб 07.03, вс 08.03, пн 09.03, вт 10.03\n\n"
        "Контекст:\n[1] Аня 18:01: так, кто сделает слайды про конкурентов?\n"
        "[2] Лёша 18:05: ахах я вчера всю ночь их видел во сне\n\n"
        ">>> [3] Костя 18:10 (ответ на [1]): давай я, к пт скину",
        '{"tasks": [{"assignee": "Костя", "task": "сделать слайды про конкурентов", "deadline_text": "к пт", '
        '"deadline": "2026-03-06", "kind": "accepted_request", "source_ids": [1, 3]}]}',
    ),
    (
        "Участники: Аня (@anya_v), Костя (@kostya), Лёша (@lesha_p)\n"
        "Новое сообщение отправлено: 2026-03-03 (вторник) 18:12\nЧекпоинт команды: 2026-03-10\n"
        "Календарь: ср 04.03, чт 05.03, пт 06.03, сб 07.03, вс 08.03, пн 09.03, вт 10.03\n\n"
        "Контекст:\n[3] Костя 18:10: давай я, к пт скину\n[4] Лёша 18:11: я вчера починил деплой кстати\n\n"
        ">>> [5] Аня 18:12: + огонь, ещё бы кто-нибудь опрос доделал",
        '{"tasks": []}',
    ),
]


SYSTEM_PROMPT_V2 = """Ты — помощник студенческой команды в Telegram. Ты замечаешь моменты, когда участник БЕРЁТ \
на себя задачу. Смотри ТОЛЬКО на новое сообщение (помечено >>>). Контекст нужен, чтобы понять, на что человек \
отвечает, — задачи из контекста НЕ возвращай, даже если они там есть.

Задача есть, только если в НОВОМ сообщении:
1) автор сам обещает сделать: «ща сделаю макет», «беру бота», «го я», «давай я», «на мне», «запилю к пн», \
«скину вечерком»; перенос «не успею к пт, сделаю к пн» — тоже обещание (с новым сроком);
2) автор соглашается на просьбу или вопрос «кто сделает?» из контекста: «ок», «+», «да, к пт будет», «давай я» — \
тогда ответственный — АВТОР нового сообщения, а задача берётся из просьбы;
3) автор прямо, без вопроса, поручает задачу другому: «Лёша — на тебе графики к пт», «Маша, сделай опрос до завтра».

Задачи НЕТ (ответ {"tasks": []}), если новое сообщение:
- вопрос или просьба, на которую ещё не согласились: «@kostya возьмёшь опрос?», «скинешь иконки?», «кто сделает?»;
- условное или неуверенное: «могу взять, если успею», «если надо, могу», «попробую», «постараюсь»;
- про уже сделанное или процесс: «пофиксил», «сделал», «скинул», «делаю», «ковыряю»;
- отказ или переадресация: «не успею», «я пас», «не возьму, попросите Аню» — у Ани задачи тоже нет;
- решение или встреча: «решили делать на React», «го созвон в 8», «созвон в пт в 18:00»;
- общее «нужно сделать X», «надо бы», «нам нужно» без конкретного человека;
- чужие планы: «Петя вроде делает», «препод сказал сделать к пт»;
- шутка, сарказм, реакция («+», «ок», «го» на шутку, новость или предложение созвониться);
- повтор уже взятой задачи без нового срока.

Для каждой задачи верни:
- "evidence": ТОЧНЫЙ фрагмент НОВОГО сообщения, где автор берёт задачу, соглашается или поручает;
- "assignee": имя ответственного ТОЧНО как в списке участников;
- "task": что сделать, 3–8 слов, с глагола: «сделать презентацию»;
- "deadline_text": фраза срока из переписки («к пт», «завтра», «на выхах», «к КТ») или null;
- "kind": "promise" | "accepted_request" | "assignment";
- "source_ids": номера сообщений, из которых сложилась задача.

Ответь ТОЛЬКО JSON: {"tasks": [...]}. Если сомневаешься — {"tasks": []}."""

_CTX = ("Участники: Аня (@anya_v), Костя (@kostya), Лёша (@lesha_p)\n"
        "Новое сообщение отправлено: 2026-03-03 (вторник) {t}\nЧекпоинт команды: 2026-03-10\n"
        "Календарь: ср 04.03, чт 05.03, пт 06.03, сб 07.03, вс 08.03, пн 09.03, вт 10.03\n\n"
        "Контекст:\n[1] Аня 18:01: так, кто сделает слайды про конкурентов?\n"
        "[2] Лёша 18:05: ахах я вчера всю ночь их видел во сне\n")
FEW_SHOT_V2 = [
    (_CTX.format(t="18:10") + "\n>>> [3] Костя 18:10 (ответ на [1]): давай я, к пт скину",
     '{"tasks": [{"evidence": "давай я, к пт скину", "assignee": "Костя", "task": "сделать слайды про конкурентов", '
     '"deadline_text": "к пт", "kind": "accepted_request", "source_ids": [1, 3]}]}'),
    (_CTX.format(t="18:12") + "[3] Костя 18:10: давай я, к пт скину\n\n"
     ">>> [4] Аня 18:12: + огонь. @lesha_p возьмёшь опрос к КТ? и го созвон в чт в 19",
     '{"tasks": []}'),
    (_CTX.format(t="18:20") + "[3] Костя 18:10: давай я, к пт скину\n\n"
     ">>> [5] Лёша 18:20: деплой вчера починил. могу ещё графики взять, если успею с курсачом",
     '{"tasks": []}'),
    (_CTX.format(t="18:25") + "[3] Костя 18:10: давай я, к пт скину\n\n"
     ">>> [6] Аня 18:25: окей, тогда так: введение допишу к ср, а Лёша — на тебе графики к пт",
     '{"tasks": [{"evidence": "введение допишу к ср", "assignee": "Аня", "task": "дописать введение", '
     '"deadline_text": "к ср", "kind": "promise", "source_ids": [6]}, '
     '{"evidence": "Лёша — на тебе графики к пт", "assignee": "Лёша", "task": "сделать графики", '
     '"deadline_text": "к пт", "kind": "assignment", "source_ids": [6]}]}'),
]

SYSTEM_PROMPT_V3 = """Ты — помощник команды в Telegram. Ты замечаешь моменты, когда участник БЕРЁТ на себя дело. \
Смотри ТОЛЬКО на новое сообщение (помечено >>>). Контекст нужен, чтобы понять, на что человек отвечает, — \
дела из контекста НЕ возвращай, даже если они там есть.

Задача есть, если в НОВОМ сообщении:
1) автор твёрдо обещает сделать что-то сам — крупное или мелкое, со сроком или без: «ща сделаю макет», «беру бота», \
«го я», «давай я», «на мне», «запилю к пн», «скину вечерком», «ща гляну PR #12», «посмотрю», «сейчас скину», \
«в личку напишу», «завтра позвоню», «заведу issue», «потестю патчи», «дай-ка пробегусь по задачам»; \
перенос «не успею к пт, сделаю к пн» — тоже обещание (с новым сроком);
2) автор соглашается на просьбу или вопрос «кто сделает?» из контекста: «ок», «+», «да, к пт будет», «давай я», «бери» — \
ответственный — тот, кто берёт дело, задача берётся из просьбы;
3) автор прямо, без вопроса, поручает дело другому: «Лёша — на тебе графики к пт», «Маша, сделай опрос до завтра».

Задачи НЕТ, если новое сообщение:
- вопрос или просьба, на которую ещё не согласились: «@kostya возьмёшь опрос?», «скинешь иконки?», «кто сделает?»;
- неуверенное или условное: «попробую», «наверное, напишу», «может гляну», «постараюсь», «могу взять, если успею», «если надо, могу»;
- про уже сделанное или то, что делается прямо сейчас: «пофиксил», «сделал», «скинул», «делаю», «выкатываю прямо сейчас»;
- отказ или переадресация: «не успею», «я пас», «не возьму, попросите Аню» — у Ани задачи тоже нет;
- решение или встреча: «решили делать на React», «го созвон в 8», «созвон в пт в 18:00»;
- общее «нужно сделать X», «надо бы», «нам нужно» без конкретного человека;
- чужие планы: «Петя вроде делает», «препод сказал сделать к пт»;
- шутка, сарказм, реакция («+», «ок», «го» на шутку, новость или предложение созвониться);
- повтор уже взятого дела без нового срока.

Для каждой задачи верни:
- "evidence": ТОЧНЫЙ фрагмент НОВОГО сообщения, где автор берёт дело, соглашается или поручает;
- "assignee": имя ответственного ТОЧНО как в списке участников;
- "task": что сделать, 3–8 слов, с глагола: «сделать презентацию», «посмотреть PR #12»;
- "deadline_text": фраза срока из переписки («к пт», «завтра», «ща», «на выхах», «к КТ») или null;
- "kind": "promise" | "accepted_request" | "assignment";
- "source_ids": номера сообщений, из которых сложилась задача.

Ответь ТОЛЬКО JSON: {"tasks": [...]} или {"tasks": []}."""

FEW_SHOT_V3 = FEW_SHOT_V2[:3] + [
    (_CTX.format(t="18:22") + "[3] Костя 18:10: давай я, к пт скину\n"
     "[4] Аня 18:15: я там PR #12 с парсером открыла, если что\n\n"
     ">>> [5] Лёша 18:22: ща гляну твой PR. графики наверное тоже возьму, но не обещаю",
     '{"tasks": [{"evidence": "ща гляну твой PR", "assignee": "Лёша", "task": "посмотреть PR #12", '
     '"deadline_text": "ща", "kind": "promise", "source_ids": [4, 5]}]}'),
    FEW_SHOT_V2[3],
]

PROMPTS = {
    "v1": (SYSTEM_PROMPT_V1, FEW_SHOT_V1),
    "v2": (SYSTEM_PROMPT_V2, FEW_SHOT_V2),
    "v3": (SYSTEM_PROMPT_V3, FEW_SHOT_V3),
}


HEDGE = re.compile(r"(?<![а-яё])(попробую|попробуем|наверное|может быть|может|мб|постараюсь|если|бы|"
                   r"не обещаю|как-нибудь|хотел|хотела|хочу)(?![а-яё])")


def is_hedged(evidence: str) -> bool:
    """Неуверенное или условное обещание по правилам разметки — не задача."""
    return bool(HEDGE.search(evidence.lower().replace("ё", "е")))


def _norm_tokens(s: str) -> list[str]:
    return re.findall(r"[а-яёa-z0-9+]+", s.lower().replace("ё", "е"))


def evidence_in_message(evidence: str | None, text: str) -> bool:
    """Цитата модели действительно взята из нового сообщения (а не из контекста)."""
    if not evidence:
        return False
    ev, tx = " ".join(_norm_tokens(evidence)), " ".join(_norm_tokens(text))
    if not ev:
        return evidence.strip() in text
    if ev in tx:
        return True
    ev_t, tx_t = set(ev.split()), set(tx.split())
    return len(ev_t & tx_t) / len(ev_t) >= 0.6


@dataclass
class ChatMessage:
    id: int
    author: str
    ts: datetime
    text: str
    reply_to: int | None = None


@dataclass
class ExtractedTask:
    assignee: str | None
    task: str
    deadline_text: str | None
    deadline_model: date | None
    deadline: date | None  # итоговая дата: по правилам, иначе от модели
    deadline_precision: str
    kind: str
    source_ids: list[int]


@dataclass
class ExtractionResult:
    tasks: list[ExtractedTask] = field(default_factory=list)
    raw: str = ""
    error: str | None = None
    dropped: list[dict] = field(default_factory=list)
    llm: ChatResult | None = None


def _fmt_msg(m: ChatMessage, marker: str = "") -> str:
    reply = f" (ответ на [{m.reply_to}])" if m.reply_to else ""
    return f"{marker}[{m.id}] {m.author} {m.ts:%H:%M}{reply}: {m.text}"


def _person(p: dict) -> str:
    """«blueur (@slykiddd, Андрей)»: ник и как человек себя назвал — чтобы «андрей, сделай» нашло blueur."""
    extra = [x for x in (f"@{p['username']}" if p.get("username") else None, p.get("alias")) if x]
    return f"{p['name']} ({', '.join(extra)})" if extra else p["name"]


def build_user_prompt(members: list[dict], context: list[ChatMessage], target: ChatMessage,
                      checkpoint: date | None) -> str:
    people = ", ".join(_person(p) for p in members)
    d = target.ts.date()
    cal = ", ".join(f"{WEEKDAY_SHORT[x.weekday()]} {x:%d.%m}" for x in (d + timedelta(days=i) for i in range(1, 15)))
    lines = [
        f"Участники: {people}",
        f"Новое сообщение отправлено: {d.isoformat()} ({WEEKDAY_FULL[d.weekday()]}) {target.ts:%H:%M}",
        f"Чекпоинт команды: {checkpoint.isoformat() if checkpoint else 'не задан'}",
        f"Календарь: {cal}",
        "",
        "Контекст:" if context else "Контекст: нет",
        *[_fmt_msg(m) for m in context],
        "",
        _fmt_msg(target, ">>> "),
    ]
    return "\n".join(lines)


def _parse_json(content: str) -> dict:
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError("no JSON object in response")
    return json.loads(m.group(0))


def _match_member(name: str | None, members: list[dict], author: str) -> str | None:
    if not name:
        return None
    n = name.strip().lstrip("@").lower().replace("ё", "е")
    if n in ("я", "автор", "author", "i", "me", "myself"):
        return author
    # «Лёша» → «Алексей Петров» (@lesha_k): уменьшительные, падежи, ник латиницей, «меня зовут …»
    m = find_member(name, members)
    return m["name"] if m else name


def _to_date(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


async def extract(client: GigaChatClient, model: str, members: list[dict], context: list[ChatMessage],
                  target: ChatMessage, checkpoint: date | None = None,
                  prompt_version: str = PROMPT_VERSION) -> ExtractionResult:
    system, few_shot = PROMPTS[prompt_version]
    messages = [{"role": "system", "content": system}]
    for user, assistant in few_shot:
        messages += [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]
    messages.append({"role": "user", "content": build_user_prompt(members, context, target, checkpoint)})

    res = ExtractionResult()
    res.llm = await client.chat(messages, model=model)
    res.raw = res.llm.content
    try:
        data = _parse_json(res.raw)
    except (ValueError, json.JSONDecodeError) as e:
        res.error = f"parse: {e}"
        return res
    for t in data.get("tasks") or []:
        if not isinstance(t, dict) or not t.get("task"):
            continue
        assignee = _match_member(t.get("assignee"), members, target.author)
        names = {m["name"] for m in members} | {target.author}
        if prompt_version != "v1" and assignee not in names:
            # «я», «мне», искажённое имя: обещание и согласие — это всегда автор; иначе ответственный неизвестен
            if (t.get("kind") or "promise") in ("promise", "accepted_request"):
                assignee = target.author
            else:
                res.dropped.append({**t, "drop": "unknown_assignee"})
                continue
        if prompt_version != "v1":
            # проверки v2: цитата из нового сообщения; поручение другому — не вопрос
            if not evidence_in_message(t.get("evidence"), target.text):
                res.dropped.append({**t, "drop": "evidence_not_in_message"})
                continue
            if prompt_version >= "v3" and is_hedged(t.get("evidence") or ""):
                res.dropped.append({**t, "drop": "hedged"})
                continue
            if assignee != target.author and "?" in target.text:
                res.dropped.append({**t, "drop": "question_to_other"})
                continue
        dl_text = t.get("deadline_text") or None
        rule_date, precision = resolve_deadline(dl_text, target.ts, checkpoint)
        model_date = _to_date(t.get("deadline"))
        res.tasks.append(ExtractedTask(
            assignee=assignee,
            task=str(t["task"]).strip(),
            deadline_text=dl_text,
            deadline_model=model_date,
            deadline=rule_date if precision == "day" else (model_date if precision == "unparsed" else None),
            deadline_precision=precision,
            kind=t.get("kind") or "promise",
            source_ids=[int(x) for x in t.get("source_ids") or [] if str(x).isdigit()],
        ))
    return res
