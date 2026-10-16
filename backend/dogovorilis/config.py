from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore",
                                      env_ignore_empty=True)

    bot_token: str = ""
    actor_bot_tokens: str = ""
    test_chat_id: int | None = None
    allow_actor_bots: bool = False

    gigachat_auth_key: str = ""
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2"
    # версия промпта извлечения: v2 — прошла RAT; v3 — ловит «ща гляну», кандидат
    extract_prompt: str = "v2"
    gigachat_ca_file: Path = ROOT / "certs" / "russian_trusted_root_ca.cer"
    # личный тариф GigaChat: 1 одновременный запрос на аккаунт (второй получает 429)
    gigachat_concurrency: int = 1
    # деградация по длине очереди: 1 — откладываем слабые сигналы, 2 — ещё и быстрая модель
    degrade_strict_at: int = 10
    degrade_fast_at: int = 40
    degrade_model: str = "GigaChat-2"

    database_url: str = "postgresql+asyncpg://dogovorilis:dogovorilis@localhost:5433/dogovorilis"
    redis_url: str = "redis://localhost:6380/0"

    board_secret: str = "dev-secret"
    board_base_url: str = "http://localhost:5173"
    # короткое имя мини-аппа из @BotFather (/newapp): ссылка t.me/<bot>/<имя>?startapp=<чат>
    miniapp_short_name: str = ""
    api_base_url: str = "http://localhost:8010"

    # сколько последних сообщений чата отдаём модели как контекст и как долго их храним
    context_messages: int = 8
    context_ttl_hours: int = 24
    # через сколько секунд спросить срок у взявшего задачу без срока (0 — сразу)
    deadline_ask_delay_s: int = 0

    @property
    def actor_tokens(self) -> list[str]:
        return [t.strip() for t in self.actor_bot_tokens.split(",") if t.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
