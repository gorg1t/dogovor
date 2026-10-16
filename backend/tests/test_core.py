"""Тесты детерминированных частей конвейера (без сети и модели): pytest backend/tests"""

from datetime import date, datetime

import pytest

from dogovorilis.bot.cards import apply_edit, card_text, open_card_keyboard, open_card_text
from dogovorilis.llm.dates import resolve_deadline
from dogovorilis.llm.dedup import classify, expand_recent, match_open_item
from dogovorilis.llm.extractor import evidence_in_message, is_hedged
from dogovorilis.llm.open_tasks import is_volunteer_call
from dogovorilis.llm.prefilter import prefilter

WED = datetime(2026, 10, 14, 15, 0)  # среда


@pytest.mark.parametrize("text, expected", [
    ("к пт", date(2026, 10, 16)),
    ("до пятницы", date(2026, 10, 16)),
    ("в среду", date(2026, 10, 21)),          # тот же день недели — следующая неделя
    ("к среде", date(2026, 10, 21)),
    ("к четвергу", date(2026, 10, 15)),
    ("к понедельнику", date(2026, 10, 19)),
    ("завтра", date(2026, 10, 15)),
    ("послезавтра", date(2026, 10, 16)),
    ("ща", date(2026, 10, 14)),
    ("вечерком", date(2026, 10, 14)),
    ("на выхах", date(2026, 10, 18)),
    ("через неделю", date(2026, 10, 21)),
    ("через 3 дня", date(2026, 10, 17)),
    ("до 20.10", date(2026, 10, 20)),
    ("к 25 октября", date(2026, 10, 25)),
    ("до дедлайна (21го)", date(2026, 10, 21)),
])
def test_deadline_rules(text, expected):
    d, prec = resolve_deadline(text, WED)
    assert prec == "day" and d == expected


def test_deadline_checkpoint_and_vague():
    assert resolve_deadline("к КТ", WED, date(2026, 10, 17)) == (date(2026, 10, 17), "day")
    assert resolve_deadline("к КТ", WED, None) == (None, "unparsed")
    assert resolve_deadline("на следующей неделе", WED) == (None, "vague")
    assert resolve_deadline(None, WED) == (None, "none")


def test_deadline_night_shift():
    night = datetime(2026, 10, 15, 0, 44)  # ночь на четверг
    assert resolve_deadline("к чт", night)[0] == date(2026, 10, 15)
    assert resolve_deadline("завтра", night)[0] == date(2026, 10, 15)
    assert resolve_deadline("ща", night)[0] == date(2026, 10, 15)
    assert resolve_deadline("к чт", datetime(2026, 10, 15, 14, 0))[0] == date(2026, 10, 22)


def test_deadline_k_utru():
    assert resolve_deadline("к утру", datetime(2026, 10, 14, 23, 0))[0] == date(2026, 10, 15)
    assert resolve_deadline("к утру", datetime(2026, 10, 14, 2, 0))[0] == date(2026, 10, 14)


@pytest.mark.parametrize("text, ctx, passed", [
    ("ща сделаю макет к пт", [], True),
    ("беру бота", [], True),
    ("+", ["Егор, сделаешь презу?"], True),          # согласие после просьбы
    ("+", ["ахах смешно"], False),                    # реакция на шутку
    ("лол я уснула на паре", [], False),
    ("заведу под это отдельный issue", [], True),
    ("да, бери, пожалуйста", [], True),
])
def test_prefilter(text, ctx, passed):
    assert prefilter(text, ctx).passed is passed


def test_evidence_must_come_from_new_message():
    assert evidence_in_message("давай я, к пт скину", "давай я, к пт скину!")
    assert evidence_in_message("+", "+ к пт будет")
    assert not evidence_in_message("поправить время на бэке", "Ладно, решим на созвоне в пн")
    assert not evidence_in_message(None, "что угодно")


@pytest.mark.parametrize("ev, hedged", [
    ("попробую поставить", True), ("наверное, напишу tip", True), ("скину, если найду", True),
    ("ща гляну PR", False), ("завтра скину", False),
])
def test_hedge(ev, hedged):
    assert is_hedged(ev) is hedged


def test_dedup_and_reschedule():
    recent = [{"task": "сделать холодильник", "assignee_name": "blueur", "message_id": 12,
               "deadline": "2026-10-16", "status": "confirmed", "task_id": 2}]
    # «ок» на ту же задачу без нового срока — повтор
    assert classify("сделать холодильник", "blueur", None, [12, 16], 16, recent)[0] == "duplicate"
    # «будет готово завтра» — перенос
    v, dup = classify("сделать холодильник", "blueur", "2026-10-15", [15], 15, recent)
    assert v == "reschedule" and dup["task_id"] == 2
    # другая задача или другой человек — новая карточка
    assert classify("сделать микроволновку", "blueur", None, [20], 20, recent)[0] == "new"
    assert classify("сделать холодильник", "Егор", None, [21], 21, recent)[0] == "new"
    # отклонённые карточки не мешают
    assert classify("сделать холодильник", "blueur", None, [22], 22, [{**recent[0], "status": "rejected"}])[0] == "new"


def test_open_items_in_dedup_and_matching():
    card = {"type": "open", "message_id": 30, "items": [
        {"task": "сделать фронт", "claimed_by": "blueur", "deadline": None, "task_id": 5},
        {"task": "сделать бэк", "claimed_by": None}]}
    flat = expand_recent([card])
    assert flat == [{"task": "сделать фронт", "assignee_name": "blueur", "message_id": 30, "deadline": None,
                     "status": "confirmed", "task_id": 5}]
    hit = match_open_item("сделать бэк на FastAPI", [card])
    assert hit and hit[1] == 1
    assert match_open_item("купить пиццу", [card]) is None


@pytest.mark.parametrize("text, call", [
    ("Кто сделает фронт?", True), ("кто что берёт?", True), ("таски:\n1. фронт\n2. бэк", True),
    ("нужно: фронт, бэк", True), ("кто идёт на обед?", False),
    # «нужно X» без исполнителя — тоже поиск исполнителя (что из этого работа, решает детектор)
    ("нужно сделать мем", True), ("нужен деплой на gh pages", True), ("надо подумать", False),
    ("кто делает фронт?", True), ("кто то делает фронт?", True), ("кто обучит модель?", True),
    ("кто знает пароль от wifi?", False), ("кто будет на созвоне?", False),
])
def test_volunteer_call(text, call):
    assert is_volunteer_call(text) is call


def _card(**kw):
    base = {"key": "-1:5:0", "assignee_name": "Егор", "task": "сделать бота", "deadline": "2026-10-16",
            "deadline_text": "к пт", "quote_text": "ща сделаю бота к пт", "quote_author_name": "Егор",
            "quote_ts": "2026-10-14T20:00:00+03:00"}
    return {**base, **kw}


def test_card_text_escapes_html():
    t = card_text(_card(task="<script>"))
    assert "&lt;script&gt;" in t and "до пт 16.10" in t


def test_apply_edit_semicolon_format():
    members = [{"name": "Егор", "username": "egor", "user_id": 1}, {"name": "Андрей", "username": "a", "user_id": 2}]
    p = apply_edit(_card(), "Андрей; сделать фронт; завтра", members, WED, None)
    assert (p["assignee_name"], p["assignee_user_id"], p["task"], p["deadline"]) == ("Андрей", 2, "сделать фронт", "2026-10-15")
    p = apply_edit(_card(), "; ; -", members, WED, None)
    assert p["deadline"] is None and p["assignee_name"] == "Егор"


def test_open_card_buttons_only_for_free_items():
    p = {"key": "-1:30:o", "quote_text": "кто что берёт?", "quote_author_name": "Егор",
         "quote_ts": "2026-10-14T20:00:00+03:00",
         "items": [{"task": "сделать фронт", "claimed_by": "blueur"}, {"task": "сделать бэк", "claimed_by": None}]}
    kb = open_card_keyboard(p)
    assert [b.callback_data for row in kb.inline_keyboard for b in row] == ["t|-1:30:o|1"]
    assert "✅ blueur" in open_card_text(p)
    p["items"][1]["claimed_by"] = "Егор"
    assert open_card_keyboard(p) is None and "разобраны" in open_card_text(p)


def test_open_items_not_offered_again():
    from dogovorilis.llm.dedup import same_task, triage_open_items
    assert same_task("сделать фронт", "Сделать фронт")
    assert not same_task("сделать фронт", "сделать бэк")          # общий глагол не склеивает разные дела
    assert same_task("подготовить презентацию", "сделать презентацию")
    recent = [
        {"type": "open", "key": "c:125:o", "message_id": 125, "card_message_id": 126,
         "items": [{"task": "сделать фронт", "claimed_by": "blueur", "task_id": 8}]},
        {"type": "open", "key": "c:129:o", "message_id": 129, "card_message_id": 130,
         "items": [{"task": "подключить дб", "claimed_by": None}]},
    ]
    verdicts = {task: v for v, task, _ in triage_open_items(["сделать фронт", "подключить дб", "написать тесты"], recent)}
    assert verdicts == {"сделать фронт": "taken", "подключить дб": "offered", "написать тесты": "new"}



def test_mentioned_only_when_asked():
    from dogovorilis.llm.dedup import mentioned
    assert mentioned("сделать фронт", "кто возьмёт фронт и тесты?")
    assert not mentioned("сделать фронт", "кто возьмёт c++?")      # фронт подтянут из старой переписки — молчим



def test_split_enumeration():
    from dogovorilis.llm.open_tasks import split_enumeration
    assert split_enumeration("сделать фронт и вёрстку писем") == ["сделать фронт", "сделать вёрстку писем"]
    assert split_enumeration("сделать фронт, бэк и презентацию") == ["сделать фронт", "сделать бэк", "сделать презентацию"]
    assert split_enumeration("настроить CI и задеплоить") == ["настроить CI", "задеплоить"]
    assert split_enumeration("сделать макет главной") == ["сделать макет главной"]



def test_same_task_ignores_shared_verb():
    from dogovorilis.llm.dedup import match_open_item, same_task
    assert not same_task("настроить аналитику", "настроить CI")       # общий глагол ≠ та же задача (баг из автотеста)
    assert same_task("подготовить презентацию", "сделать презентацию")
    assert same_task("написать тексты для лендинга", "тексты для лендинга")
    assert same_task("настроить безопасность", "проверить безопасность")  # «-ость» — не глагол
    assert not same_task("настроить сеть", "настроить безопасность")
    card = {"items": [{"task": "написать тексты для лендинга", "claimed_by": None},
                      {"task": "настроить аналитику", "claimed_by": None}]}
    assert match_open_item("настроить CI", [card]) is None              # «беру CI» не должно забирать аналитику
    assert match_open_item("написать тексты", [card])[1] == 0


def test_open_tasks_accepts_plain_strings():
    """GigaChat иногда отвечает {"open_tasks": ["интеграция с jira"]} — это не «пусто» (баг из автотеста)."""
    import asyncio
    from types import SimpleNamespace

    from dogovorilis.llm.extractor import ChatMessage
    from dogovorilis.llm.open_tasks import detect_open_tasks

    class FakeLLM:
        async def chat(self, messages, model, max_tokens=400):
            return SimpleNamespace(content='{"open_tasks": ["интеграция с jira", {"task": "сделать бэк", "deadline_text": null}]}')

    target = ChatMessage(1, "Дима", datetime(2026, 10, 15, 22, 0), "кто возьмёт интеграцию с jira и бэк?", None)
    found = asyncio.run(detect_open_tasks(FakeLLM(), "m", [], [], target))
    assert [t.task for t in found] == ["интеграция с jira", "сделать бэк"]


def test_edit_hint_mentions_editor():
    from dogovorilis.bot.cards import edit_hint
    assert "@gorg1t" in edit_hint("Егор", 1, "gorg1t")
    assert 'tg://user?id=5' in edit_hint("Андрей", 5, None)  # без username — упоминание ссылкой


def test_bare_call_and_mentions():
    from dogovorilis.llm.dedup import mentioned
    from dogovorilis.llm.open_tasks import is_bare_call
    assert is_bare_call("кто возьмёт?") and is_bare_call("ну кто сделает это?") and is_bare_call("Кто может?")
    assert not is_bare_call("кто-нибудь видел мой зарядник?")   # задачи из старой переписки сюда не подтягиваем
    assert not is_bare_call("кто возьмёт интеграцию с jira?")
    assert mentioned("сделать презентацию", "фронт, бэк и презу к КТ — кто что берёт?")
    assert mentioned("настройка аналитики", "кто возьмёт настройку аналитики?")
    assert not mentioned("настроить аналитику", "кто-нибудь видел мой зарядник?")


def test_open_card_notes_and_link():
    from dogovorilis.bot.cards import msg_link
    p = {"quote_ts": "2026-10-15T20:00:00+00:00", "quote_text": "кто возьмёт x и y?", "quote_author_name": "Дима",
         "items": [{"task": "сделать x", "claimed_by": None}],
         "notes": ['«Сделать y» уже предлагали — <a href="https://t.me/c/1/2">карточка</a>']}
    assert "ℹ️ «Сделать y» уже предлагали" in open_card_text(p)
    assert msg_link(-1004278948187, 7) == "https://t.me/c/4278948187/7" and msg_link(123, 7) is None


def test_dedup_ignores_shared_verb():
    """«сделать бт по мл» — не дубль «сделать фронт» того же человека (из-за этого «я сделаю» терялось)."""
    recent = [{"task": "сделать фронт", "assignee_name": "blueur", "message_id": 165, "deadline": None, "status": "confirmed"}]
    assert classify("сделать бт по мл", "blueur", None, [414, 415], 415, recent)[0] == "new"
    assert classify("доделать фронт", "blueur", None, [], 420, recent)[0] == "duplicate"


def test_volunteer_call_slang_and_group_request():
    for t in ("доделайте кто нить бт по мл", "сделайте кто-нить презу", "кто-либо может глянуть?",
              "доделайте бт по мл", "ребят, гляньте пр плиз", "Напишите тесты к апи"):
        assert is_volunteer_call(t), t
    for t in ("смотрите что нашёл", "давайте созвонимся", "на сайте баг", "извините, опоздаю",
              "леша и дима сможете сегодня встретится в 6?"):
        assert not is_volunteer_call(t), t


def test_impersonal_need_is_not_authors_promise():
    """«нужно сделать отчёт до конца недели» — не обещание автора (класс vague), а свободная задача."""
    from dogovorilis.llm.open_tasks import impersonal_need
    from dogovorilis.worker.app import _taken_note
    for t in ("нужно сделать отчет до конца недели", "надо подготовить презу к пт", "ребят, нужно починить бота",
              "нужен деплой на gh pages", "нужно скинуть макет"):
        assert impersonal_need(t), t
    for t in ("нужно сделать отчет, сделаю к пт", "нужно сделать отчет, я возьму", "надо доделать бэк, займусь вечером",
              "маша, нужно сделать презентацию к субботе", "нужно сделать отчет, могу взять", "я сделаю отчет"):
        assert not impersonal_need(t), t
    # справка называет ту задачу, с которой сопоставили, и её срок
    note = _taken_note("сделать отчет", {"task": "подготовить отчёт по продажам", "assignee_name": "Дима",
                                         "deadline": "2026-10-19"})
    assert note == "«Подготовить отчёт по продажам» уже взял(а) Дима, до пн 19.10"


def test_bare_accept_binds_to_latest_call():
    """«я сделаю» после «доделайте кто нить ревью» — про ревью, а не про более ранний «кто возьмёт jira?»."""
    from dogovorilis.llm.extractor import ExtractedTask
    from dogovorilis.llm.open_tasks import is_bare_accept
    from dogovorilis.worker.app import bind_to_latest_call
    assert is_bare_accept("я сделаю") and is_bare_accept("беру") and is_bare_accept("давай я к чт")
    assert not is_bare_accept("я сделаю ревью") and not is_bare_accept("беру фронт")
    recent = [{"type": "open", "message_id": 502, "items": [{"task": "интеграция с jira", "claimed_by": "Егор"}]},
              {"type": "open", "message_id": 508, "card_message_id": 509,
               "items": [{"task": "доделать ревью пулл-реквеста", "claimed_by": None}]}]
    t = ExtractedTask("Лёша", "сделать интеграцию с jira", None, None, None, "none", "accepted_request", [502, 510])
    msg = {"message_id": 510, "reply_to": None}
    assert bind_to_latest_call(t, msg, "я сделаю", recent).task == "доделать ревью пулл-реквеста"
    # модель сослалась на более свежую адресную просьбу — не трогаем
    t2 = ExtractedTask("Лёша", "сделать отчёт", None, None, None, "none", "accepted_request", [509, 510])
    assert bind_to_latest_call(t2, msg, "я сделаю", recent).task == "сделать отчёт"
    # ответ на конкретное другое сообщение — модели виднее
    assert bind_to_latest_call(t, {"message_id": 510, "reply_to": 502}, "я сделаю", recent).task == t.task
    # в сообщении названа задача — не трогаем
    assert bind_to_latest_call(t, msg, "я сделаю jira", recent).task == t.task


def _task(id_, title, assignee="blueur", uid=7, status="open"):
    from types import SimpleNamespace
    return SimpleNamespace(id=id_, title=title, assignee_name=assignee, assignee_user_id=uid, status=status)


def test_completion_detection():
    from dogovorilis.llm.completion import completed_tasks
    tasks = [_task(1, "сделать бэк", "Егор", 1), _task(2, "сделать фронт", "blueur", 7), _task(3, "сделать верстку", "Егор", 1)]
    assert [t.id for t in completed_tasks("я доделал бэк", tasks, 1)] == [1]
    assert [t.id for t in completed_tasks("все я сделал фронт", tasks, 7)] == [2]
    assert [t.id for t in completed_tasks("фронт готов", tasks, 7)] == [2]
    assert completed_tasks("я не доделал бэк", tasks, 1) == []
    assert completed_tasks("ты сделал бэк?", tasks, 7) == []
    assert completed_tasks("сделаю бэк завтра", tasks, 1) == []
    # одна часть фразы — про сделанное, другая — обещание: «фронт» не закрываем
    assert [t.id for t in completed_tasks("доделал бэк, а фронт сделаю завтра", tasks, 1)] == [1]
    # «всё, сделал» без предмета — единственная задача автора
    assert [t.id for t in completed_tasks("всё, сделал", tasks, 7)] == [2]
    assert completed_tasks("всё, сделал", tasks, 1) == []  # у Егора две задачи — не угадываем


def test_unknown_assignee_becomes_open_item():
    """Исполнителя не найти среди участников (Максим не в чате, «кто-то из вас») — «Кто возьмёт?», а не карточка-призрак."""
    from dogovorilis.llm.extractor import ChatMessage
    from dogovorilis.worker.app import unassigned_items
    target = ChatMessage(29, "blueur", datetime(2026, 10, 16, 0, 5), "сделать лендинг - максим", None)
    dropped = [{"assignee": "максим", "task": "сделать лендинг", "evidence": "сделать лендинг - максим",
                "kind": "assigned", "drop": "unknown_assignee", "source_ids": [29]},
               {"assignee": "Петя", "task": "y", "evidence": "нет такой цитаты", "drop": "unknown_assignee"}]
    assert [i.task for i in unassigned_items(dropped, target)] == ["сделать лендинг"]


def test_names_diminutives_and_cases():
    from dogovorilis.llm.names import find_member
    m = [{"name": "Алексей Петров", "username": "lesha_k", "user_id": 1}, {"name": "Дмитрий", "user_id": 2},
         {"name": "Лена", "user_id": 6}, {"name": "Леонид", "user_id": 7}, {"name": "blueur", "username": "slykiddd",
                                                                            "user_id": 4, "alias": "Андрей"}]
    name = lambda n: (find_member(n, m) or {}).get("name")  # noqa: E731
    assert name("Лёша") == name("Лёше") == name("Лёшу") == name("lesha") == name("Алексею") == "Алексей Петров"
    assert name("Дима") == name("Диме") == "Дмитрий"
    assert name("Лёня") == "Леонид" and name("Лена") == "Лена"
    assert name("андрюха") == "blueur"  # «меня зовут Андрей» + уменьшительное
    assert name("Максим") is None and name("я") is None


def test_alias_matches_member():
    from dogovorilis.llm.extractor import _match_member, build_user_prompt
    members = [{"name": "blueur", "username": "slykiddd", "user_id": 7, "alias": "Андрей"},
               {"name": "Егор", "username": "gorg1t", "user_id": 1}]
    assert _match_member("андрей", members, "Егор") == "blueur"
    assert _match_member("Андрею", members, "Егор") == "blueur"
    target = __import__("dogovorilis.llm.extractor", fromlist=["ChatMessage"]).ChatMessage(1, "Егор", datetime(2026, 10, 16), "x", None)
    assert "blueur (@slykiddd, Андрей), Егор (@gorg1t)" in build_user_prompt(members, [], target, None)


def test_done_proposals_are_not_taken():
    recent = [{"type": "done", "task": "сделать бэк", "assignee_name": "Егор", "message_id": 14, "status": "pending"}]
    assert expand_recent(recent) == []


def test_reschedule_when_task_named_shorter():
    """«не успеваю ревью, сделаю к сб» — перенос задачи «доделать ревью пулл-реквеста», а не новая задача."""
    recent = [{"task": "доделать ревью пулл-реквеста", "assignee_name": "Лёша", "message_id": 60, "deadline": None,
               "status": "confirmed", "task_id": 9}]
    assert classify("сделать ревью", "Лёша", "2026-10-17", [], 70, recent)[0] == "reschedule"
    assert classify("сделать ревью", "Маша", "2026-10-17", [], 70, recent)[0] == "new"  # другой человек


def test_dash_assignments():
    from dogovorilis.llm.extractor import ChatMessage, ExtractedTask
    from dogovorilis.llm.prefilter import prefilter
    from dogovorilis.worker.app import dash_assignments
    members = [{"name": "blueur", "username": "slykiddd", "user_id": 7, "alias": "Андрей"},
               {"name": "Егор", "username": "gorg1t", "user_id": 1}]
    ts = datetime(2026, 10, 16, 0, 5)

    def run(text, tasks=()):
        found, free = dash_assignments(list(tasks), members, ChatMessage(5, "Егор", ts, text, None), None)
        return [(t.assignee, t.task) for t in found], [o.task for o in free]

    assert prefilter("развернуть сайт - андрей").passed  # раньше не доходило до модели
    assert run("развернуть сайт - андрей") == ([("blueur", "развернуть сайт")], [])     # прозвище → участник
    assert run("сделать презу к пд - максим") == ([], ["сделать презу к пд"])           # не участник → «Кто возьмёт?»
    assert run("деплой на gh pages - Егор") == ([("Егор", "деплой на gh pages")], [])    # известный участник
    assert run("фронт - норм") == ([], []) and run("сделать отчёт - срочно") == ([], [])  # не поручения
    llm = ExtractedTask("Егор", "сделать презу к пд", None, None, None, "none", "promise", [5])
    assert run("сделать презу к пд - сема", [llm]) == ([], ["сделать презу к пд"])       # не автору, а свободная
    found, free = run("сделать презу к пд - андрей\nпосчитать сработки по gnn",
                      [ExtractedTask("Егор", "посчитать сработки по gnn", None, None, None, "none", "promise", [5])])
    assert sorted(found) == [("blueur", "сделать презу к пд"), ("Егор", "посчитать сработки по gnn")] and free == []


def test_event_time_parsing():
    from datetime import time
    from dogovorilis.llm.events import is_event_message, parse_time, parse_when
    assert parse_time("завтра в 19:00") == time(19, 0) and parse_time("в 19.30") == time(19, 30)
    assert parse_time("в 6 вечера") == time(18, 0) and parse_time("в 6") == time(18, 0)  # рабочая встреча — вечер
    assert parse_time("в 10 утра") == time(10, 0) and parse_time("в пт") is None
    thu = datetime(2026, 10, 15, 14, 0)
    assert parse_when("завтра в 19:00", thu) == (date(2026, 10, 16), time(19, 0))
    assert parse_when("в пт", thu) == (date(2026, 10, 16), None)
    assert parse_when("в 11:00", thu) == (date(2026, 10, 16), time(11, 0))  # сегодня уже прошло — завтра
    assert parse_when("19.10 в 15:00", thu) == (date(2026, 10, 19), time(15, 0))  # дата через точку — не время
    for t in ("кто придёт на созвон завтра?", "давайте встретимся, обсудим?", "решили: созвон в пт в 19:00",
              "го в зум в 18"):
        assert is_event_message(t), t
    assert not is_event_message("сделаю фронт к пт")


def test_rsvp_from_text():
    from dogovorilis.bot.app import rsvp_from_text
    assert rsvp_from_text("+") == "yes" and rsvp_from_text("буду") == "yes" and rsvp_from_text("Приду!") == "yes"
    assert rsvp_from_text("не смогу, сорри") == "no" and rsvp_from_text("постараюсь") == "maybe"
    assert rsvp_from_text("а кто ведёт?") is None


def test_task_permissions():
    """Менять задачу могут исполнитель, предложивший и автор сообщения; если никого из них бот не знает — любой."""
    from types import SimpleNamespace
    from dogovorilis.core.service import managers_label, may_manage
    t = SimpleNamespace(assignee_user_id=1, assignee_name="Егор", requested_by_id=7, requested_by_name="blueur",
                        quote_author_id=1, quote_author_name="Егор")
    assert may_manage(t, 1) and may_manage(t, 7) and not may_manage(t, 9)
    assert managers_label(t) == "Егор или blueur"
    nobody = SimpleNamespace(assignee_user_id=None, assignee_name="Максим", requested_by_id=None, requested_by_name=None,
                             quote_author_id=None, quote_author_name="Максим")
    assert may_manage(nobody, 9)


def test_requested_by_and_unassigned():
    from dogovorilis.llm.extractor import ChatMessage, ExtractedTask
    from dogovorilis.worker.app import requested_by, unassigned_items
    msg = {"name": "Маша", "user_id": 2}
    accepted = ExtractedTask("Маша", "сделать FAQ", None, None, None, "none", "accepted_request", [1, 2])
    assert requested_by(accepted, msg, [{"author": "Дима", "user_id": 5, "text": "сможешь FAQ?"}]) == (5, "Дима")
    assigned = ExtractedTask("Егор", "сделать фронт", None, None, None, "none", "assignment", [3])
    assert requested_by(assigned, msg, []) == (2, "Маша")
    promise = ExtractedTask("Маша", "сделать дизайн", None, None, None, "none", "promise", [3])
    assert requested_by(promise, msg, []) == (None, None)
    target = ChatMessage(3, "Маша", datetime(2026, 10, 15, 12), "надо бы задеплоить на gh pages до пт", None)
    dropped = [{"assignee": None, "task": "задеплоить на gh pages", "evidence": "задеплоить на gh pages",
                "deadline_text": "до пт", "drop": "unknown_assignee"},
               {"assignee": "Максим", "task": "x", "evidence": "нет такого", "drop": "unknown_assignee"}]
    items = unassigned_items(dropped, target)  # «Максим» без цитаты из сообщения — не берём
    assert [(i.task, str(i.deadline)) for i in items] == [("задеплоить на gh pages", "2026-10-16")]


def test_event_card_text():
    from types import SimpleNamespace
    from dogovorilis.bot.cards import event_text
    ev = SimpleNamespace(title="Созвон по фронту", day=date(2026, 10, 17), when_text="завтра в 19",
                         starts_at=datetime(2026, 10, 17, 19, 0, tzinfo=__import__("dogovorilis.bot.cards", fromlist=["MSK"]).MSK),
                         place=None)
    people = [SimpleNamespace(name="Егор", status="yes"), SimpleNamespace(name="blueur", status="yes"),
              SimpleNamespace(name="Маша", status="maybe")]
    assert event_text(ev, people) == "📅 <b>Созвон по фронту</b> · сб 17.10, 19:00\n✅ Егор, blueur · 🤔 Маша"



def test_new_deadline_formats():
    from dogovorilis.llm.dates import deadline_time, resolve_deadline, split_deadline
    fri = datetime(2026, 10, 16, 14, 0)
    assert resolve_deadline("к концу месяца", fri)[0] == date(2026, 10, 31)
    assert resolve_deadline("до 25 числа", fri)[0] == date(2026, 10, 25)
    assert resolve_deadline("до 10 числа", fri)[0] == date(2026, 11, 10)  # 10-е уже прошло — следующего месяца
    assert resolve_deadline("до конца следующей недели", fri)[0] == date(2026, 10, 25)
    assert resolve_deadline("by Monday", fri)[0] == date(2026, 10, 19)
    assert resolve_deadline("до обеда", fri)[0] == date(2026, 10, 16)
    assert deadline_time("сегодня к 18:00") == "18:00" and deadline_time("до обеда") == "13:00"
    assert split_deadline("Пролонгировать домен до 20 октября", fri) == ("Пролонгировать домен", date(2026, 10, 20), "до 20 октября")
    assert split_deadline("настроить CI", fri) == ("настроить CI", None, None)


def test_deadline_buttons_show_dates():
    from dogovorilis.bot.cards import deadline_ask_keyboard
    kb = deadline_ask_keyboard(5, datetime(2026, 10, 16, 14, 0))  # пятница: «До пт» — это 23.10
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "До пт 23.10" in labels and "До вс 18.10" in labels


def test_card_past_and_urgent():
    p = _card(deadline="2026-10-10", deadline_text="до 10 октября", urgent=True, deadline_time="18:00")
    t = card_text(p)
    assert "🔥 срочно" in t and "18:00" in t and "уже прошла" in t


def test_free_task_synonyms():
    card = {"items": [{"task": "Пролонгировать домен до 20 октября", "claimed_by": None},
                      {"task": "баг с авторизацией", "claimed_by": None}]}
    assert match_open_item("продление домена", [card])[1] == 0
    assert match_open_item("поправить баг с логином", [card])[1] == 1


def test_fallback_promises():
    from dogovorilis.llm.fallback import rule_promises
    fri = datetime(2026, 10, 16, 14, 0)
    assert rule_promises("акт сверки сдам до 14 октября", fri) == [("сдать акт сверки", "до 14 октября")]
    assert rule_promises("ща гляну пр, к вечеру апрув кину", fri) == [("глянуть пр", "ща"), ("кинуть апрув", "к вечеру")]
    assert rule_promises("I'll fix the CI pipeline by Monday", fri) == [("fix the CI pipeline", "by Monday")]
    assert rule_promises("так / по релизу / я завтра / соберу / changelog", fri) == [("собрать changelog", "завтра")]
    assert rule_promises("если успею, сделаю фронт", fri) == [] and rule_promises("ты сделаешь фронт?", fri) == []


def test_event_rsvp_intent():
    from dogovorilis.llm.events import rsvp_intent
    assert rsvp_intent("я буду на созвоне", False) == "yes"
    assert rsvp_intent("я завтра не смогу, у меня врач", True) == "no"
    assert rsvp_intent("я завтра не смогу, у меня врач", False) is None  # встречу недавно не предлагали
    assert rsvp_intent("буду делать фронт", True) is None and rsvp_intent("не успею к пт", True) is None


def test_cancel_and_handover_words():
    from dogovorilis.llm.completion import HANDOVER, cancelled
    assert cancelled("фронт больше не нужен", ["сделать фронт", "сделать бэк"]) == [0]
    assert cancelled("а фронт нужен?", ["сделать фронт"]) == []
    assert HANDOVER.search("я заболел, лёш, забери демо")


def test_board_change_text():
    from types import SimpleNamespace
    from dogovorilis.api.app import board_change_text
    a = SimpleNamespace(title="Сделать фронт", status="open", deadline=None, assignee_name="Егор")
    b = SimpleNamespace(title="Сделать фронт", status="open", deadline=date(2026, 10, 23), assignee_name="Егор")
    assert board_change_text(a, b, "Егор") == "✏️ Егор на доске: «Сделать фронт» — срок до пт 23.10"
    done = SimpleNamespace(**{**b.__dict__, "status": "done"})
    assert board_change_text(b, done, "Егор").startswith("✅ Егор на доске: «Сделать фронт» — готово")


def test_same_task_by_head_word():
    """Общее второстепенное слово — не та же задача; то же главное слово — та же."""
    from dogovorilis.llm.dedup import same_task
    assert not same_task("сделать лендинг", "сделать дизайн лендинга")
    assert same_task("сделать тексты", "написать тексты для лендинга")
    assert same_task("баг с логином", "баг с авторизацией")


def test_same_task_action_nouns():
    from dogovorilis.llm.dedup import same_task
    assert same_task("настроить аналитику", "настройка аналитики")  # «настройка» — действие, предмет — аналитика
    assert not same_task("настройка CI", "настройка аналитики")
