"""Живость процессов: пульс в Redis и сторож зависания.

Упавший процесс docker перезапускает сам (restart: unless-stopped), а зависший — нет: процесс жив,
но ничего не делает. Сторож в отдельном потоке видит, что цикл событий перестал тикать, и завершает
процесс — docker поднимает его заново. Пульс в Redis читают монитор задержки и /status.
"""

import asyncio
import logging
import os
import threading
import time

log = logging.getLogger("health")

TICK_S = 5
STALL_S = 120  # цикл событий не тикал столько — считаем процесс зависшим


class Watchdog:
    def __init__(self, store, name: str):
        self.store, self.name = store, name
        self.tick = time.monotonic()

    async def _beat(self) -> None:
        while True:
            self.tick = time.monotonic()
            try:
                # живёт 4 такта: остановленный процесс пропадает за 20 с — раньше, чем монитор поднимет тревогу
                await self.store.r.set(f"hb:{self.name}", int(time.time()), ex=4 * TICK_S)
            except Exception:  # noqa: BLE001 — Redis недоступен: процесс жив, пульс пропускаем
                log.warning("heartbeat %s: redis unavailable", self.name)
            await asyncio.sleep(TICK_S)

    def _guard(self) -> None:
        while True:
            time.sleep(10)
            if time.monotonic() - self.tick > STALL_S:
                log.critical("%s: event loop stalled for %.0fs — exiting so docker restarts it",
                             self.name, time.monotonic() - self.tick)
                os._exit(1)

    def start(self) -> asyncio.Task:
        threading.Thread(target=self._guard, name=f"watchdog-{self.name}", daemon=True).start()
        return asyncio.create_task(self._beat())
