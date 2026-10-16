"""Redis: очередь входящих сообщений (Streams), контекст переписки и карточки до подтверждения.

Всё здесь живёт с TTL: текст чата не хранится дольше суток, неподтверждённая карточка — двое суток.
"""

import json
import time

from redis.asyncio import Redis
from redis.exceptions import ResponseError

STREAM = "incoming"
LOW_STREAM = "incoming_low"  # отложенные при перегрузке сообщения со слабым сигналом
GROUP = "workers"
CONTEXT_KEEP = 50
PROPOSAL_TTL_S = 48 * 3600


class Store:
    def __init__(self, redis: Redis, context_ttl_s: int = 24 * 3600):
        self.r = redis
        self.context_ttl_s = context_ttl_s

    @classmethod
    def from_url(cls, url: str, context_ttl_s: int = 24 * 3600) -> "Store":
        return cls(Redis.from_url(url, decode_responses=True), context_ttl_s)

    async def close(self) -> None:
        await self.r.aclose()

    # --- очередь -----------------------------------------------------------------------------
    async def ensure_group(self) -> None:
        for stream in (STREAM, LOW_STREAM):
            try:
                await self.r.xgroup_create(stream, GROUP, id="0", mkstream=True)
            except ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise

    async def enqueue(self, msg: dict) -> str | None:
        """Кладёт сообщение в очередь ровно один раз (Telegram может прислать апдейт повторно)."""
        if not await self.r.set(f"seen:{msg['chat_id']}:{msg['message_id']}", 1, nx=True, ex=48 * 3600):
            return None
        await self.push_context(msg)
        # записи старше суток обрезаем: текст переписки не хранится дольше, чем обещано
        return await self.r.xadd(STREAM, {"data": json.dumps(msg, ensure_ascii=False)},
                                 minid=str(int((time.time() - self.context_ttl_s) * 1000)), approximate=True)

    async def read(self, consumer: str, count: int = 10, block_ms: int = 2000,
                   stream: str = STREAM) -> list[tuple[str, dict]]:
        res = await self.r.xreadgroup(GROUP, consumer, {stream: ">"}, count=count, block=block_ms)
        return [(eid, json.loads(f["data"])) for _, entries in res or [] for eid, f in entries]

    async def claim_stale(self, consumer: str, min_idle_ms: int, count: int = 20,
                          stream: str = STREAM) -> list[tuple[str, dict]]:
        """Забирает сообщения, которые взял и не подтвердил упавший или ждущий LLM воркер."""
        _, entries, _ = await self.r.xautoclaim(stream, GROUP, consumer, min_idle_time=min_idle_ms,
                                                start_id="0-0", count=count)
        return [(eid, json.loads(f["data"])) for eid, f in entries if f]

    async def ack(self, entry_id: str, stream: str = STREAM) -> None:
        await self.r.xack(stream, GROUP, entry_id)
        await self.r.xdel(stream, entry_id)

    async def defer(self, msg: dict) -> None:
        await self.r.xadd(LOW_STREAM, {"data": json.dumps({**msg, "deferred": True}, ensure_ascii=False)})

    async def queue_stats(self) -> dict:
        try:
            pending = await self.r.xpending(STREAM, GROUP)
        except ResponseError:
            pending = {"pending": 0}
        return {"length": await self.r.xlen(STREAM), "pending": pending["pending"],
                "deferred": await self.r.xlen(LOW_STREAM), "degrade_level": int(await self.r.get("degrade:level") or 0)}

    # --- контекст переписки ------------------------------------------------------------------
    async def push_context(self, msg: dict) -> None:
        key = f"ctx:{msg['chat_id']}"
        await self.r.rpush(key, json.dumps(msg, ensure_ascii=False))
        await self.r.ltrim(key, -CONTEXT_KEEP, -1)
        await self.r.expire(key, self.context_ttl_s)

    async def get_context(self, chat_id: int, before_message_id: int, n: int) -> list[dict]:
        items = [json.loads(x) for x in await self.r.lrange(f"ctx:{chat_id}", 0, -1)]
        now = time.time()
        items = [m for m in items if m["message_id"] < before_message_id
                 and now - m.get("unix", now) <= self.context_ttl_s]
        return items[-n:]

    async def thread_after(self, chat_id: int, message_id: int, within_s: int = 300, limit: int = 2) -> list[dict]:
        """Пара сообщений сразу после обещания (как «ок, тогда я на CI») — для экрана задачи."""
        items = [json.loads(x) for x in await self.r.lrange(f"ctx:{chat_id}", 0, -1)]
        base = next((m for m in items if m["message_id"] == message_id), None)
        if not base:
            return []
        after = [m for m in items if m["message_id"] > message_id and m.get("unix", 0) - base.get("unix", 0) <= within_s
                 and not (m.get("text") or "").startswith("/")]
        return [{"author": m["name"], "text": m["text"], "ts": m["ts"]} for m in after[:limit]]

    # --- карточки до подтверждения -----------------------------------------------------------
    async def save_proposal(self, p: dict) -> bool:
        """False, если карточка с таким ключом уже есть (повторная обработка того же сообщения)."""
        ok = await self.r.set(f"proposal:{p['key']}", json.dumps(p, ensure_ascii=False), nx=True, ex=PROPOSAL_TTL_S)
        if ok:
            await self.r.zadd(f"proposals:{p['chat_id']}", {p["key"]: time.time()})
            await self.r.expire(f"proposals:{p['chat_id']}", PROPOSAL_TTL_S)
        return bool(ok)

    async def get_proposal(self, key: str) -> dict | None:
        raw = await self.r.get(f"proposal:{key}")
        return json.loads(raw) if raw else None

    async def update_proposal(self, p: dict) -> None:
        await self.r.set(f"proposal:{p['key']}", json.dumps(p, ensure_ascii=False), ex=PROPOSAL_TTL_S)

    async def recent_proposals(self, chat_id: int, within_s: int = 24 * 3600) -> list[dict]:
        keys = await self.r.zrangebyscore(f"proposals:{chat_id}", time.time() - within_s, "+inf")
        out = []
        for k in keys:
            p = await self.get_proposal(k)
            if p:
                out.append(p)
        return out

    async def claim_once(self, key: str, ttl_s: int = PROPOSAL_TTL_S) -> bool:
        """Атомарный флаг «сделано один раз» (отправка карточки, обработка нажатия)."""
        return bool(await self.r.set(f"once:{key}", 1, nx=True, ex=ttl_s))

    # --- /forget -----------------------------------------------------------------------------
    async def forget_user(self, chat_id: int, user_id: int) -> int:
        key = f"ctx:{chat_id}"
        items = await self.r.lrange(key, 0, -1)
        keep = [x for x in items if json.loads(x).get("user_id") != user_id]
        removed = len(items) - len(keep)
        pipe = self.r.pipeline()
        pipe.delete(key)
        if keep:
            pipe.rpush(key, *keep)
            pipe.expire(key, self.context_ttl_s)
        await pipe.execute()
        for p in await self.recent_proposals(chat_id, PROPOSAL_TTL_S):
            if user_id in (p.get("assignee_user_id"), p.get("quote_author_id")):
                await self.r.delete(f"proposal:{p['key']}")
                removed += 1
        # текст тех же сообщений в очереди (живёт до суток) и запомненное «меня зовут …»
        mine = [eid for eid, f in await self.r.xrange(STREAM)
                if (json.loads(f["data"]).get("chat_id"), json.loads(f["data"]).get("user_id")) == (chat_id, user_id)]
        if mine:
            await self.r.xdel(STREAM, *mine)
        await self.r.hdel(f"aliases:{chat_id}", str(user_id))
        return removed

    # --- вопрос о сроке ------------------------------------------------------------------------
    async def schedule_deadline_ask(self, chat_id: int, task_id: int, delay_s: int) -> None:
        await self.r.zadd("ask_deadline", {f"{chat_id}:{task_id}": time.time() + delay_s})

    async def due_deadline_asks(self) -> list[tuple[int, int]]:
        return await self._pop_due("ask_deadline")

    async def _pop_due(self, key: str) -> list[tuple[int, int]]:
        """Наступившие записи «chat_id:id» из отложенной очереди (zset по времени)."""
        out = []
        for member in await self.r.zrangebyscore(key, 0, time.time()):
            if await self.r.zrem(key, member):  # забирает только один процесс
                chat_id, item = member.rsplit(":", 1)
                out.append((int(chat_id), int(item)))
        return out

    # --- чистота чата: служебные сообщения бота живут недолго -------------------------------------
    async def delete_later(self, chat_id: int, message_id: int, delay_s: float) -> None:
        await self.r.zadd("autodelete", {f"{chat_id}:{message_id}": time.time() + delay_s})

    async def due_deletes(self) -> list[tuple[int, int]]:
        return await self._pop_due("autodelete")

    async def remember_bot_message(self, chat_id: int, message_id: int) -> None:
        """id сообщений бота за двое суток (Telegram не даёт боту историю) — чтобы автотест мог убрать за собой."""
        key = f"botmsgs:{chat_id}"
        await self.r.zadd(key, {str(message_id): time.time()})
        await self.r.zremrangebyscore(key, 0, time.time() - 48 * 3600)

    # --- как участники себя называют: «меня зовут Андрей» → blueur — это Андрей ----------------------
    async def set_alias(self, chat_id: int, user_id: int, alias: str) -> None:
        await self.r.hset(f"aliases:{chat_id}", str(user_id), alias)

    async def aliases(self, chat_id: int) -> dict[int, str]:
        return {int(k): v for k, v in (await self.r.hgetall(f"aliases:{chat_id}")).items()}

    # --- живость: насколько отстаёт разбор сообщений ------------------------------------------------
    async def backlog(self) -> tuple[float, set[int]]:
        """Возраст самого старого неразобранного сообщения основной очереди (с) и чаты, где оно копится.

        id записи в Redis Stream — это время постановки в очередь (мс): берём самое раннее среди
        «взятых, но не подтверждённых» (воркер завис или модель не отвечает) и «ещё не выданных» (воркер лежит).
        """
        try:
            group = next((g for g in await self.r.xinfo_groups(STREAM) if g["name"] == GROUP), None)
            pending = await self.r.xpending(STREAM, GROUP)
        except ResponseError:
            return 0.0, set()
        ids = [pending["min"]] if pending.get("pending") else []
        undelivered = await self.r.xrange(STREAM, min=f"({group['last-delivered-id']}" if group else "-", count=1)
        ids += [undelivered[0][0]] if undelivered else []
        if not ids:
            return 0.0, set()
        oldest = min(ids, key=lambda i: tuple(map(int, i.split("-"))))
        chats = {json.loads(f["data"])["chat_id"] for _, f in await self.r.xrange(STREAM, min=oldest, count=200)}
        return time.time() - int(oldest.split("-")[0]) / 1000, chats

    async def bot_messages(self, chat_id: int, since_message_id: int) -> list[int]:
        return sorted(i for i in map(int, await self.r.zrange(f"botmsgs:{chat_id}", 0, -1)) if i >= since_message_id)

    # --- флаги для проверок отказоустойчивости -----------------------------------------------
    async def llm_down(self) -> bool:
        return bool(await self.r.exists("chaos:llm_down"))
