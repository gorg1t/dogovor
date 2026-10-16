"""«Я доделал бэк» → предложить отметить задачу выполненной (карточка «Готово?», решает человек).

Правила, без вызова модели: глагол завершения в прошедшем времени + предмет открытой задачи в той же части фразы.
Если предмет не назван («всё, сделал») и у автора ровно одна открытая задача — предлагаем её.
"""

import re

from .dedup import _object_tokens, mentioned

DONE_VERB = re.compile(
    r"(?<![а-я])(с?делал[аи]?|до[а-я]{2,}л[аи]?|закончил[аи]?|завершил[аи]?|готов[оа]?|выкатил[аи]?|залил[аи]?|"
    r"запушил[аи]?|[сз]?мержил[аи]?|починил[аи]?|пофиксил[аи]?|поправил[аи]?|сдал[аи]?|отправил[аи]?|написал[аи]?|"
    r"сверстал[аи]?|настроил[аи]?|собрал[аи]?|задеплоил[аи]?|развернул[аи]?|обучил[аи]?|посчитал[аи]?|"
    r"подготовил[аи]?|оформил[аи]?|выложил[аи]?|done)(?![а-я])")
_NOT_YET = re.compile(r"(?<![а-я])(не|почти|пока|ещ[её]|если|когда|чуть)\s+(\w+\s+)?$")
_FUTURE = re.compile(r"(?<![а-я])(сделаю|доделаю|закончу|буду|допишу|успею|завтра|потом)(?![а-я])")
_FILLER = {"я", "все", "всё", "уже", "вот", "ну", "короче", "итак", "наконец", "кстати", "ребят", "ребята", "мы", "это"}


def _done_clauses(text: str) -> list[str]:
    """Части фразы, где сказано «сделал» без отрицания, вопроса и будущего."""
    t = (text or "").lower().replace("ё", "е")
    out = []
    for clause in re.split(r"[,.;!\n]+|\s(?:а|но|и)\s", t):
        if "?" in clause or _FUTURE.search(clause):
            continue
        m = DONE_VERB.search(clause)
        if m and not _NOT_YET.search(clause[:m.start()]):
            out.append(clause)
    return out


CANCEL = re.compile(r"(?<![а-я])(отмен\w*|отбой|не нуж(?:ен|на|но|ны)|не нужн\w*|не надо|неактуальн\w*|снимаем|снимаю|забейте|"
                    r"не делаем|не делайте|больше не делаем)(?![а-я])")
HANDOVER = re.compile(r"(?<![а-я])(забер\w*|забирай|возьми|подхвати\w*|передаю|передам|перекин\w*|подмени\w*|"
                      r"вместо меня|заболел\w*|не смогу|не успеваю|не успею)(?![а-я])")


def cancelled(text: str, titles: list[str]) -> list[int]:
    """«фронт больше не нужен», «созвон отменяется» → номера названий, о которых это сказано."""
    t = (text or "").lower().replace("ё", "е")
    if "?" in t or not CANCEL.search(t):
        return []
    rest = CANCEL.sub(" ", t)
    return [i for i, title in enumerate(titles) if mentioned(title, rest)]


def completed_tasks(text: str, tasks: list, author_id: int | None) -> list:
    """Открытые задачи чата, о завершении которых говорит сообщение (своим — приоритет)."""
    hits: list = []
    open_tasks = [t for t in tasks if t.status == "open"]
    mine = [t for t in open_tasks if author_id and t.assignee_user_id == author_id]
    for clause in _done_clauses(text):
        named = [t for t in open_tasks if mentioned(t.title, DONE_VERB.sub(" ", clause))]
        if named:
            own = [t for t in named if t in mine]
            hits += own or named
        elif not (_object_tokens(DONE_VERB.sub(" ", clause)) - _FILLER) and len(mine) == 1:
            hits.append(mine[0])  # «всё, сделал» — единственная задача автора
    return list({t.id: t for t in hits}.values())
