"""Запуск всего «Договорились» одной командой (для демо): python tools/run_all.py

Поднимает docker compose (PostgreSQL, Redis), затем бота, воркер, API и доску.
Упавший процесс перезапускается через 3 секунды. Логи — в logs/*.log. Остановка — Ctrl+C.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
NPX = shutil.which("npx") or "npx"
LOGS = ROOT / "logs"

SERVICES = {
    "bot": ([str(PY), "-m", "dogovorilis.bot.app"], ROOT / "backend"),
    "worker": ([str(PY), "-m", "dogovorilis.worker.app"], ROOT / "backend"),
    "api": ([str(PY), "-m", "uvicorn", "dogovorilis.api.app:app", "--host", "127.0.0.1", "--port", "8010"], ROOT / "backend"),
    "web": ([NPX, "vite", "--host", "127.0.0.1", "--port", "5173", "--strictPort"], ROOT / "web"),
}


def start(name: str) -> subprocess.Popen:
    cmd, cwd = SERVICES[name]
    log = open(LOGS / f"{name}.log", "a", encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
           "WORKER_CONCURRENCY": os.environ.get("WORKER_CONCURRENCY", "2")}
    print(f"▶ {name}")
    return subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env=env, shell=(name == "web" and os.name == "nt"))


def main() -> None:
    LOGS.mkdir(exist_ok=True)
    subprocess.run(["docker", "compose", "--env-file", ".env", "up", "-d"], cwd=ROOT, check=True)
    procs = {name: start(name) for name in SERVICES}
    print("Доска: http://localhost:5173 (ссылку с токеном даёт /board в чате). Ctrl+C — остановить.")
    try:
        while True:
            time.sleep(3)
            for name, p in procs.items():
                if p.poll() is not None:
                    print(f"✖ {name} упал (код {p.returncode}), перезапускаю — см. logs/{name}.log")
                    procs[name] = start(name)
    except KeyboardInterrupt:
        print("Останавливаю…")
        for p in procs.values():
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            else:
                p.send_signal(signal.SIGTERM)


if __name__ == "__main__":
    sys.exit(main())
