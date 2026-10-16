"""Запустить сквозной автотест на сервере: python tools/e2e_remote.py --host <IP>"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import deploy  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--host", required=True)
ap.add_argument("--script", default=str(ROOT / "backend" / "tests" / "e2e_tg.py"), help="скрипт для запуска в контейнере бота")
a = ap.parse_args()
c = deploy.connect(a.host, "root", None)
sftp = c.open_sftp()
sftp.put(a.script, "/tmp/e2e_tg.py")
sftp.close()
_, out, _ = c.exec_command("cd /opt/dogovorilis && docker compose -f docker-compose.prod.yml --env-file .env "
                           "exec -T bot python - < /tmp/e2e_tg.py 2>&1", timeout=1800, get_pty=False)
for line in iter(out.readline, ""):
    print(line.rstrip(), flush=True)
c.close()
