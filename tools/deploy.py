"""Деплой «Договорились» на сервер по SSH (Docker + Caddy с автоматическим HTTPS).

Первый раз (по паролю — скрипт поставит SSH-ключ и дальше будет ходить по нему):
    python tools/deploy.py --host 1.2.3.4 --password '<root-пароль>' [--domain board.example.ru] [--migrate-db]
Обновление кода:
    python tools/deploy.py --host 1.2.3.4
Проверка без изменений:
    python tools/deploy.py --host 1.2.3.4 --check

Домен по умолчанию — <ip>.sslip.io (бесплатный, указывает на IP сервера; Let's Encrypt выдаёт на него сертификат).
"""

import argparse
import io
import os
import secrets
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
KEY = Path.home() / ".ssh" / "dogovorilis_deploy_rsa"
REMOTE = "/opt/dogovorilis"


def log(msg: str) -> None:
    print(f"▶ {msg}", flush=True)


def ensure_key() -> paramiko.RSAKey:
    if not KEY.exists():
        KEY.parent.mkdir(exist_ok=True)
        key = paramiko.RSAKey.generate(3072)
        key.write_private_key_file(str(KEY))
        KEY.with_suffix(".pub").write_text(f"ssh-rsa {key.get_base64()} dogovorilis-deploy\n")
    return paramiko.RSAKey.from_private_key_file(str(KEY))


def connect(host: str, user: str, password: str | None) -> paramiko.SSHClient:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    key = ensure_key()
    try:
        c.connect(host, username=user, pkey=key, timeout=20, allow_agent=False, look_for_keys=False)
        return c
    except paramiko.AuthenticationException:
        if not password:
            sys.exit("SSH-ключ не принят, а пароль не передан: запустите с --password")
    c.connect(host, username=user, password=password, timeout=20, allow_agent=False, look_for_keys=False)
    pub = KEY.with_suffix(".pub").read_text().strip()
    run(c, f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qF {shlex.quote(pub)} ~/.ssh/authorized_keys 2>/dev/null "
           f"|| echo {shlex.quote(pub)} >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys")
    log("SSH-ключ установлен — дальше вход без пароля")
    return c


def run(c: paramiko.SSHClient, cmd: str, check: bool = True, quiet: bool = False, timeout: int = 1800) -> str:
    _, out, err = c.exec_command(cmd, timeout=timeout, get_pty=False)
    text = out.read().decode("utf-8", "replace")
    code = out.channel.recv_exit_status()
    if not quiet and text.strip():
        print("  " + text.strip().replace("\n", "\n  "))
    if check and code != 0:
        sys.exit(f"Команда упала ({code}): {cmd}\n{err.read().decode('utf-8', 'replace')}")
    return text


def read_env(path: Path) -> dict[str, str]:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.split("  #")[0].strip()
    return env


def prod_env(domain: str, miniapp: str, existing: dict[str, str]) -> str:
    local = read_env(ROOT / ".env")
    keep = ["BOT_TOKEN", "ACTOR_BOT_TOKENS", "TEST_CHAT_ID", "ALLOW_ACTOR_BOTS", "GIGACHAT_AUTH_KEY", "GIGACHAT_SCOPE",
            "GIGACHAT_MODEL", "EXTRACT_PROMPT", "GIGACHAT_CONCURRENCY", "DEGRADE_STRICT_AT", "DEGRADE_FAST_AT",
            "DEGRADE_MODEL", "DEADLINE_ASK_DELAY_S", "BOARD_SECRET", "POSTGRES_USER", "POSTGRES_DB"]
    env = {k: local[k] for k in keep if k in local}
    env.update({
        # пароль базы на сервере свой; при повторном деплое сохраняем прежний
        "POSTGRES_PASSWORD": existing.get("POSTGRES_PASSWORD") or secrets.token_urlsafe(24),
        "DOMAIN": domain,
        "BOARD_BASE_URL": f"https://{domain}",
        "API_BASE_URL": f"https://{domain}",
        "MINIAPP_SHORT_NAME": miniapp or existing.get("MINIAPP_SHORT_NAME", ""),
        "LOG_OUTGOING": "1",  # журнал ответов бота — только для тестовой группы, по нему проверяет автотест
    })
    return "# Сгенерировано tools/deploy.py — секреты, не коммитить\n" + "\n".join(f"{k}={v}" for k, v in env.items()) + "\n"


def bundle() -> bytes:
    """Код бэкенда, сертификаты, собранная доска и файлы деплоя — в один tar.gz."""
    log("собираю доску (vite build)")
    subprocess.run("npx vite build", cwd=ROOT / "web", shell=True, check=True, capture_output=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(src: Path, arc: str) -> None:
            tar.add(src, arcname=arc, filter=lambda ti: None if "__pycache__" in ti.name else ti)
        add(ROOT / "backend" / "dogovorilis", "backend/dogovorilis")
        add(ROOT / "backend" / "requirements-prod.txt", "backend/requirements-prod.txt")
        add(ROOT / "certs", "certs")
        add(ROOT / "web" / "dist", "web")
        for f in ("Dockerfile", "Caddyfile", "docker-compose.prod.yml"):
            add(ROOT / "deploy" / f, f)
    return buf.getvalue()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", default="root")
    ap.add_argument("--password")
    ap.add_argument("--domain", help="по умолчанию <ip>.sslip.io")
    ap.add_argument("--miniapp", default="", help="короткое имя мини-аппа из @BotFather /newapp")
    ap.add_argument("--migrate-db", action="store_true", help="перенести локальную базу (задачи, чаты) на сервер")
    ap.add_argument("--check", action="store_true", help="только проверить состояние")
    a = ap.parse_args()
    domain = a.domain or f"{a.host.replace('.', '-')}.sslip.io"

    c = connect(a.host, a.user, a.password)
    log("сервер")
    run(c, "cat /etc/os-release | grep PRETTY_NAME; nproc; free -m | head -2; df -h / | tail -1")
    if a.check:
        run(c, f"cd {REMOTE} && docker compose -f docker-compose.prod.yml ps", check=False)
        run(c, f"curl -s -m 10 https://{domain}/api/health; echo", check=False)
        return

    if "swap" not in run(c, "swapon --show", quiet=True, check=False).lower() and \
            int(run(c, "df --output=avail -BM / | tail -1", quiet=True).strip().rstrip("M")) > 1500:
        log("добавляю swap 512 МБ (2 ГБ памяти на сборку образа впритык)")
        run(c, "fallocate -l 512M /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile "
               "&& grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab", check=False)

    if "Docker version" not in run(c, "docker --version", check=False, quiet=True):
        log("ставлю Docker")
        run(c, "curl -fsSL https://get.docker.com | sh", timeout=900)
    run(c, "docker --version; docker compose version")

    log("проверяю доступ к GigaChat и Telegram с сервера")
    run(c, "curl -s -o /dev/null -m 15 -w 'gigachat oauth: %{http_code}\\n' https://ngw.devices.sberbank.ru:9443/api/v2/oauth; "
           "curl -s -o /dev/null -m 15 -w 'telegram api: %{http_code}\\n' https://api.telegram.org/", check=False)

    log("загружаю код")
    data = bundle()
    run(c, f"mkdir -p {REMOTE}")
    sftp = c.open_sftp()
    with sftp.open(f"{REMOTE}/bundle.tar.gz", "wb") as fh:
        fh.write(data)
    existing = {}
    try:
        with sftp.open(f"{REMOTE}/.env") as fh:
            existing = dict(l.split("=", 1) for l in fh.read().decode().splitlines() if "=" in l and not l.startswith("#"))
    except IOError:
        pass
    with sftp.open(f"{REMOTE}/.env", "w") as fh:
        fh.write(prod_env(domain, a.miniapp, existing))
    sftp.chmod(f"{REMOTE}/.env", 0o600)
    # папку web не пересоздаём: Caddy держит её смонтированной, новая папка ему не видна (была 404)
    run(c, f"cd {REMOTE} && rm -rf backend certs && mkdir -p web && find web -mindepth 1 -delete "
           f"&& tar xzf bundle.tar.gz && rm bundle.tar.gz")
    compose = f"cd {REMOTE} && docker compose -f docker-compose.prod.yml --env-file .env"

    if a.migrate_db:
        log("переношу базу")
        dump = subprocess.run(["docker", "exec", "webpril-postgres-1", "pg_dump", "-U", "dogovorilis", "-d", "dogovorilis",
                               "--clean", "--if-exists", "--no-owner"], capture_output=True, check=True).stdout
        with sftp.open(f"{REMOTE}/dump.sql", "wb") as fh:
            fh.write(dump)
        run(c, f"{compose} up -d postgres redis")
        for _ in range(30):
            if "healthy" in run(c, f"{compose} ps postgres", quiet=True, check=False):
                break
            time.sleep(2)
        run(c, f"{compose} exec -T postgres sh -c 'psql -q -U $POSTGRES_USER -d $POSTGRES_DB' < {REMOTE}/dump.sql "
               f"&& rm {REMOTE}/dump.sql", quiet=True)
    sftp.close()

    log("собираю и запускаю контейнеры")
    run(c, f"{compose} up -d --build", timeout=1800)
    run(c, "docker image prune -f >/dev/null; docker builder prune -f >/dev/null; df -h / | tail -1")
    time.sleep(8)
    run(c, f"{compose} ps --format '{{{{.Name}}}}: {{{{.Status}}}}'")
    log(f"проверяю https://{domain}")
    for _ in range(20):
        out = run(c, f"curl -s -m 10 https://{domain}/api/health", check=False, quiet=True)
        if '"postgres":"ok"' in out:
            print("  " + out)
            break
        time.sleep(5)
    else:
        print("  HTTPS ещё не поднялся (сертификат выпускается до минуты) — проверьте: python tools/deploy.py --host … --check")
        run(c, f"{compose} logs --tail 20 caddy", check=False)
    run(c, f"{compose} logs --tail 5 bot worker", check=False)
    print(f"\nГотово: https://{domain}\nНе забудьте остановить локальные бот и воркер — один токен бота может слушать только один процесс.")


if __name__ == "__main__":
    main()
