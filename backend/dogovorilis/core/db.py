"""PostgreSQL: только подтверждённые человеком задачи и минимум метаданных чата.

Неподтверждённые карточки и контекст переписки живут в Redis с TTL и в базу не попадают.
"""

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..config import get_settings


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram chat id
    title: Mapped[str] = mapped_column(String(255), default="")
    checkpoint: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Member(Base):
    __tablename__ = "members"
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("proposal_key", name="uq_tasks_proposal_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    # ключ идемпотентности: chat:message:index — повторная обработка не создаёт дубль (риск А2)
    proposal_key: Mapped[str] = mapped_column(String(64))
    assignee_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    assignee_name: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(Text)
    deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    deadline_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | done | deleted (удалена, можно вернуть)
    kind: Mapped[str] = mapped_column(String(32), default="promise")
    quote_text: Mapped[str] = mapped_column(Text)
    quote_author_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    quote_author_name: Mapped[str] = mapped_column(String(255))
    quote_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # сообщения до цитаты, из которых сложилась задача (просьба → «беру»): JSON [{author, text, ts}]
    context_quotes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # пара сообщений сразу после обещания (до 5 минут) и история событий задачи: JSON-списки
    context_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    history: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    confirmed_by: Mapped[str] = mapped_column(String(255))
    edited: Mapped[bool] = mapped_column(default=False)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # кто предложил задачу («кто возьмёт фронт?», «Маша, сделай…») — он тоже может назначить срок
    requested_by_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    requested_by_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deadline_time: Mapped[str | None] = mapped_column(String(5), nullable=True)  # «сегодня к 18:00» → 18:00
    urgent: Mapped[bool] = mapped_column(default=False)  # «срочно»


class Event(Base):
    """Встреча из чата («кто придёт на созвон завтра?»): отдельно от задач, на доске — в разделе «Встречи»."""
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("proposal_key", name="uq_events_proposal_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.id", ondelete="CASCADE"), index=True)
    proposal_key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    day: Mapped[date | None] = mapped_column(Date, nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # если названо время
    when_text: Mapped[str | None] = mapped_column(String(128), nullable=True)
    place: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # proposed — предложена, никто ещё не ответил; planned — кто-то ответил; cancelled — «не встреча» / удалена
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    created_by_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by_name: Mapped[str] = mapped_column(String(255))
    quote_text: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    card_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Attendee(Base):
    __tablename__ = "event_attendees"
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(8))  # yes | maybe | no
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _sessionmaker is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _sessionmaker


async def init_db() -> None:
    sessionmaker()
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # лёгкая миграция для баз, созданных до появления колонки
        for col in ("context_quotes", "context_after", "history"):
            await conn.execute(text(f"ALTER TABLE tasks ADD COLUMN IF NOT EXISTS {col} TEXT"))
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS requested_by_id BIGINT"))
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS requested_by_name VARCHAR(255)"))
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deadline_time VARCHAR(5)"))
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS urgent BOOLEAN NOT NULL DEFAULT FALSE"))
