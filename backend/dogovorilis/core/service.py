"""Операции над задачами и участниками, общие для бота, воркера и API."""

import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from .db import Attendee, Chat, Event, Member, Task, sessionmaker


async def upsert_chat(chat_id: int, title: str) -> None:
    async with sessionmaker()() as s, s.begin():
        await s.execute(insert(Chat).values(id=chat_id, title=title or "")
                        .on_conflict_do_update(index_elements=[Chat.id], set_={"title": title or ""}))


async def upsert_member(chat_id: int, user_id: int, name: str, username: str | None) -> None:
    async with sessionmaker()() as s, s.begin():
        await s.execute(insert(Member).values(chat_id=chat_id, user_id=user_id, name=name, username=username)
                        .on_conflict_do_update(index_elements=[Member.chat_id, Member.user_id],
                                               set_={"name": name, "username": username,
                                                     "updated_at": datetime.now(timezone.utc)}))


async def get_chat(chat_id: int) -> Chat | None:
    async with sessionmaker()() as s:
        return await s.get(Chat, chat_id)


async def set_checkpoint(chat_id: int, d: date | None) -> None:
    async with sessionmaker()() as s, s.begin():
        await s.execute(update(Chat).where(Chat.id == chat_id).values(checkpoint=d))


async def chats_of_user(user_id: int) -> list[Chat]:
    async with sessionmaker()() as s:
        q = select(Chat).join(Member, Member.chat_id == Chat.id).where(Member.user_id == user_id).order_by(Chat.title)
        return list((await s.execute(q)).scalars())


async def members(chat_id: int) -> list[Member]:
    async with sessionmaker()() as s:
        return list((await s.execute(select(Member).where(Member.chat_id == chat_id)
                                     .order_by(Member.name))).scalars())


def _event(text: str) -> dict:
    return {"ts": datetime.now(timezone.utc).isoformat(), "text": text}


def _append(task: Task, text: str) -> None:
    hist = json.loads(task.history or "[]")
    hist.append(_event(text))
    task.history = json.dumps(hist, ensure_ascii=False)


async def add_history(task_id: int, text: str) -> None:
    async with sessionmaker()() as s, s.begin():
        task = await s.get(Task, task_id)
        if task:
            _append(task, text)


async def confirm_proposal(p: dict, confirmed_by: str) -> tuple[Task, bool]:
    """Сохраняет подтверждённую карточку. Возвращает (задача, создана_сейчас). Повтор не создаёт дубль."""
    async with sessionmaker()() as s, s.begin():
        stmt = insert(Task).values(
            chat_id=p["chat_id"], proposal_key=p["key"], assignee_user_id=p.get("assignee_user_id"),
            assignee_name=p["assignee_name"], title=p["task"],
            deadline=date.fromisoformat(p["deadline"]) if p.get("deadline") else None,
            deadline_text=p.get("deadline_text"), kind=p.get("kind", "promise"),
            quote_text=p["quote_text"], quote_author_id=p.get("quote_author_id"),
            quote_author_name=p["quote_author_name"], quote_ts=datetime.fromisoformat(p["quote_ts"]),
            context_quotes=json.dumps(p.get("context_quotes") or [], ensure_ascii=False),
            context_after=json.dumps(p.get("context_after") or [], ensure_ascii=False),
            history=json.dumps([_event(f"{confirmed_by} взял(а) задачу кнопкой «🙋»" if p.get("kind") == "claimed"
                                       else f"{confirmed_by} подтвердил(а) задачу")], ensure_ascii=False),
            source_message_id=p["message_id"], confirmed_by=confirmed_by, edited=bool(p.get("edited")),
            requested_by_id=p.get("requested_by_id"), requested_by_name=p.get("requested_by_name"),
            deadline_time=p.get("deadline_time"), urgent=bool(p.get("urgent")),
        ).on_conflict_do_nothing(constraint="uq_tasks_proposal_key").returning(Task.id)
        new_id = (await s.execute(stmt)).scalar_one_or_none()
        task = (await s.execute(select(Task).where(Task.proposal_key == p["key"]))).scalar_one()
        return task, new_id is not None


async def get_task(task_id: int) -> Task | None:
    async with sessionmaker()() as s:
        return await s.get(Task, task_id)


async def list_tasks(chat_id: int, status: str | None = None) -> list[Task]:
    async with sessionmaker()() as s:
        q = select(Task).where(Task.chat_id == chat_id)
        # удалённые не показываем нигде, кроме явного запроса (их можно вернуть с доски)
        q = q.where(Task.status == status) if status else q.where(Task.status != "deleted")
        q = q.order_by(Task.deadline.is_(None), Task.deadline, Task.created_at)
        return list((await s.execute(q)).scalars())


async def set_task_status(chat_id: int, task_id: int, status: str, who: str | None = None) -> Task | None:
    async with sessionmaker()() as s, s.begin():
        task = await s.get(Task, task_id)
        if not task or task.chat_id != chat_id:
            return None
        if task.status != status:
            what = {"done": "Отмечено выполненным", "deleted": "Удалена"}.get(
                status, "Восстановлена" if task.status == "deleted" else "Возвращено в работу")
            _append(task, what + (f" · {who}" if who else ""))
        task.status = status
        task.done_at = datetime.now(timezone.utc) if status == "done" else None
        return task


async def update_task(chat_id: int, task_id: int, who: str | None = None, **fields) -> Task | None:
    allowed = {"title", "deadline", "deadline_text", "deadline_time", "assignee_name", "assignee_user_id"}
    async with sessionmaker()() as s, s.begin():
        task = await s.get(Task, task_id)
        if not task or task.chat_id != chat_id:
            return None
        by = f" · {who}" if who else ""
        if "deadline" in fields and fields["deadline"] != task.deadline:
            old = _fmt_day(task.deadline) if task.deadline else "без срока"
            new = _fmt_day(fields["deadline"]) if fields["deadline"] else "без срока"
            _append(task, f"Срок: {old} → {new}{by}" if task.deadline else f"Срок назначен: {new}{by}")
        if fields.get("assignee_name") and fields["assignee_name"] != task.assignee_name:
            _append(task, f"Ответственный: {task.assignee_name} → {fields['assignee_name']}{by}")
        if fields.get("title") and fields["title"] != task.title:
            _append(task, f"Задача переименована{by}")
        for k, v in fields.items():
            if k in allowed:
                setattr(task, k, v)
        task.edited = True
        return task


_WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _fmt_day(d: date) -> str:
    return f"{_WD[d.weekday()]} {d:%d.%m}"


# --- права: задачу меняют исполнитель, тот, кто её предложил, и автор сообщения, из которого она взялась ---------
def task_managers(t: Task) -> set[int]:
    return {x for x in (t.assignee_user_id, t.requested_by_id, t.quote_author_id) if x}


def may_manage(t: Task, user_id: int | None) -> bool:
    """Никого из них бот не знает (исполнитель не в чате) — менять может любой участник."""
    managers = task_managers(t)
    return not managers or user_id in managers


def managers_label(t: Task) -> str:
    names = [t.assignee_name] + [n for n in (t.requested_by_name, t.quote_author_name) if n]
    return " или ".join(dict.fromkeys(names))


ANON = "Участник"


async def forget_member(chat_id: int, user_id: int, names: list[str]) -> int:
    """/forget: личное — имя и цитаты — заменяем на «Участник», а задачи остаются у команды (отчёт не ломается).

    names — как человека звали в чате (имя, ник, «меня зовут …»): так находим и задачи, назначенные по имени,
    у которых нет id. Возвращает число задач, где что-то обезличено.
    """
    lowered = {n.lower() for n in names if n}

    def is_him(name: str | None, uid: int | None = None) -> bool:
        return (uid is not None and uid == user_id) or (name or "").lower() in lowered

    def scrub(items: list[dict]) -> list[dict]:
        return [{**q, "author": ANON, "text": "[удалено по /forget]", "user_id": None}
                if is_him(q.get("author"), q.get("user_id")) else q for q in items]

    touched = 0
    async with sessionmaker()() as s, s.begin():
        for t in (await s.execute(select(Task).where(Task.chat_id == chat_id))).scalars():
            before = (t.assignee_name, t.quote_text, t.context_quotes, t.context_after, t.requested_by_name, t.history)
            if is_him(t.assignee_name, t.assignee_user_id):
                t.assignee_name, t.assignee_user_id = ANON, None
            if is_him(t.quote_author_name, t.quote_author_id):
                t.quote_text, t.quote_author_name, t.quote_author_id = "[удалено по /forget]", ANON, None
            if is_him(t.requested_by_name, t.requested_by_id):
                t.requested_by_name, t.requested_by_id = ANON, None
            if (t.confirmed_by or "").lower() in lowered:
                t.confirmed_by = ANON
            t.context_quotes = json.dumps(scrub(json.loads(t.context_quotes or "[]")), ensure_ascii=False)
            t.context_after = json.dumps(scrub(json.loads(t.context_after or "[]")), ensure_ascii=False)
            history = t.history or ""
            for n in names:
                if n:
                    history = history.replace(n, ANON)
            t.history = history
            touched += before != (t.assignee_name, t.quote_text, t.context_quotes, t.context_after,
                                  t.requested_by_name, t.history)
        await s.execute(delete(Attendee).where(Attendee.user_id == user_id,
                                               Attendee.event_id.in_(select(Event.id).where(Event.chat_id == chat_id))))
        await s.execute(update(Event).where(Event.chat_id == chat_id, Event.created_by_id == user_id)
                        .values(created_by_id=None, created_by_name=ANON, quote_text="[удалено по /forget]"))
        await s.execute(delete(Member).where(Member.chat_id == chat_id, Member.user_id == user_id))
    return touched


async def report(chat_id: int, since: date, today: date) -> dict:
    tasks = await list_tasks(chat_id)
    done = [t for t in tasks if t.status == "done" and t.done_at and t.done_at.date() >= since]
    overdue = [t for t in tasks if t.status == "open" and t.deadline and t.deadline < today]
    open_ = [t for t in tasks if t.status == "open" and not (t.deadline and t.deadline < today)]
    return {"done": done, "overdue": overdue, "open": open_}


async def due_for_reminder(today: date) -> list[Task]:
    async with sessionmaker()() as s:
        q = select(Task).where(Task.status == "open", Task.reminded_at.is_(None),
                               Task.deadline == today + timedelta(days=1))
        return list((await s.execute(q)).scalars())


async def mark_reminded(task_id: int) -> None:
    async with sessionmaker()() as s, s.begin():
        task = await s.get(Task, task_id)
        if task:
            task.reminded_at = datetime.now(timezone.utc)
            _append(task, "Бот напомнил: срок завтра")


# --- встречи -----------------------------------------------------------------------------------------
async def create_event(chat_id: int, key: str, title: str, day: date | None, starts_at: datetime | None,
                       when_text: str | None, place: str | None, author_id: int | None, author: str, quote: str,
                       message_id: int) -> Event | None:
    """Встреча из сообщения; автор — сразу «буду». Повтор того же сообщения не создаёт вторую (None)."""
    async with sessionmaker()() as s, s.begin():
        new_id = (await s.execute(insert(Event).values(
            chat_id=chat_id, proposal_key=key, title=title, day=day, starts_at=starts_at, when_text=when_text,
            place=place, created_by_id=author_id, created_by_name=author, quote_text=quote, source_message_id=message_id,
        ).on_conflict_do_nothing(constraint="uq_events_proposal_key").returning(Event.id))).scalar_one_or_none()
        if new_id is None:
            return None
        if author_id:
            s.add(Attendee(event_id=new_id, user_id=author_id, name=author, status="yes"))
        return await s.get(Event, new_id)


async def get_event(event_id: int) -> Event | None:
    async with sessionmaker()() as s:
        return await s.get(Event, event_id)


async def update_event(event_id: int, **fields) -> Event | None:
    async with sessionmaker()() as s, s.begin():
        ev = await s.get(Event, event_id)
        if ev:
            for k, v in fields.items():
                setattr(ev, k, v)
        return ev


async def list_events(chat_id: int, since: date | None = None) -> list[Event]:
    """Встречи чата (кроме отменённых): с датой — от since, без даты — все."""
    async with sessionmaker()() as s:
        q = select(Event).where(Event.chat_id == chat_id, Event.status != "cancelled")
        if since:
            q = q.where((Event.day.is_(None)) | (Event.day >= since))
        return list((await s.execute(q.order_by(Event.day.is_(None), Event.day, Event.starts_at))).scalars())


async def attendees(event_ids: list[int]) -> dict[int, list[Attendee]]:
    if not event_ids:
        return {}
    async with sessionmaker()() as s:
        rows = (await s.execute(select(Attendee).where(Attendee.event_id.in_(event_ids))
                                .order_by(Attendee.updated_at))).scalars()
        out: dict[int, list[Attendee]] = {}
        for a in rows:
            out.setdefault(a.event_id, []).append(a)
        return out


async def rsvp(event_id: int, user_id: int, name: str, status: str) -> Event | None:
    """Ответ участника: yes | maybe | no. Первый ответ делает встречу запланированной."""
    async with sessionmaker()() as s, s.begin():
        ev = await s.get(Event, event_id)
        if not ev or ev.status == "cancelled":
            return None
        await s.execute(insert(Attendee).values(event_id=event_id, user_id=user_id, name=name, status=status)
                        .on_conflict_do_update(index_elements=["event_id", "user_id"],
                                               set_={"status": status, "name": name, "updated_at": func.now()}))
        ev.status = "planned"
        return ev


async def due_event_reminders(now: datetime, ahead: timedelta) -> list[Event]:
    async with sessionmaker()() as s:
        q = select(Event).where(Event.status != "cancelled", Event.reminded_at.is_(None), Event.starts_at.is_not(None),
                                Event.starts_at > now, Event.starts_at <= now + ahead)
        return list((await s.execute(q)).scalars())


async def event_by_card(chat_id: int, card_message_id: int) -> Event | None:
    async with sessionmaker()() as s:
        q = select(Event).where(Event.chat_id == chat_id, Event.card_message_id == card_message_id)
        return (await s.execute(q)).scalar_one_or_none()
