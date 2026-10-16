"""Взять свободную задачу из «Кто возьмёт?» — кнопкой в чате, ответом на карточку или с доски (общая логика)."""

from dataclasses import dataclass
from datetime import date

from ..llm.dedup import same_task
from . import service
from .store import Store


@dataclass
class Claim:
    card: dict | None
    item: dict | None
    task: object | None = None      # Task, если взяли сейчас
    taken_by: str | None = None     # кто взял раньше (задача уже занята)


async def claim_open_item(store: Store, key: str, idx: int, user_id: int, name: str, username: str | None,
                          deadline: date | None = None, deadline_text: str | None = None) -> Claim:
    """Создаёт задачу на взявшего; если задачу уже взяли (здесь или в другой карточке) — Claim.taken_by."""
    card = await store.get_proposal(key)
    if not card or idx >= len(card.get("items", [])):
        return Claim(None, None)
    it = card["items"][idx]
    if it.get("claimed_by"):
        return Claim(card, it, taken_by=it["claimed_by"])
    # та же задача уже взята в другой карточке — второй не создаём, а отмечаем и здесь
    dup = next((x for p in await store.recent_proposals(card["chat_id"]) if p.get("type") == "open" and p["key"] != key
                for x in p["items"] if x.get("claimed_by") and same_task(it["task"], x["task"])), None)
    if dup:
        it.update(claimed_by=dup["claimed_by"], claimed_user_id=dup.get("claimed_user_id"), task_id=dup.get("task_id"))
        await store.update_proposal(card)
        return Claim(card, it, taken_by=dup["claimed_by"])
    await service.upsert_member(card["chat_id"], user_id, name, username)
    proposal = {"key": f"{key}:{idx}", "chat_id": card["chat_id"], "message_id": card["message_id"],
                "assignee_name": name, "assignee_user_id": user_id, "task": it["task"],
                "deadline": (deadline.isoformat() if deadline else it.get("deadline")),
                "deadline_text": deadline_text or it.get("deadline_text"), "kind": "claimed",
                "quote_text": card["quote_text"], "quote_author_id": card.get("quote_author_id"),
                "quote_author_name": card["quote_author_name"], "quote_ts": card["quote_ts"],
                "requested_by_id": card.get("quote_author_id"), "requested_by_name": card["quote_author_name"]}
    task, created = await service.confirm_proposal(proposal, name)
    if not created:  # двое нажали «Беру» одновременно: задача одна (уникальный ключ) — она у того, кто успел первым
        it.update(claimed_by=task.assignee_name, claimed_user_id=task.assignee_user_id, task_id=task.id)
        await store.update_proposal(card)
        return Claim(card, it, taken_by=task.assignee_name)
    it.update(claimed_by=name, claimed_user_id=user_id, task_id=task.id)
    await store.update_proposal(card)
    await store.r.hincrby("stats", "claimed", 1)
    return Claim(card, it, task=task)
