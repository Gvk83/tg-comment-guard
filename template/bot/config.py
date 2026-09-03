"""Настройки бота. Читаются из файла .env — в код секреты не попадают."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

MODES = ("observe", "soft", "enforce")


def _ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    return {int(x) for x in raw.replace(" ", "").split(",") if x.lstrip("-").isdigit()}


def _hour(raw: str | None, default: int | None) -> int | None:
    """Час дневной сводки. Пусто или «выкл» — сводку не слать."""
    if raw is None:
        return default
    raw = raw.strip().lower()
    if raw in ("", "off", "выкл", "нет", "no"):
        return None
    return int(raw) % 24 if raw.isdigit() else default


def _int(raw: str | None, default: int) -> int:
    return int(raw) if raw and raw.strip().isdigit() else default


def _bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "да", "on")


@dataclass
class Config:
    token: str
    mode: str = "observe"
    chat_ids: set[int] = field(default_factory=set)
    admin_id: int | None = None
    notify_admin: bool = True
    whitelist_ids: set[int] = field(default_factory=set)
    store_message_text: bool = True
    delete_service_messages: bool = True
    daily_report_hour: int | None = 10
    log_retention_days: int = 90
    block_newcomer_media: bool = False
    db_path: Path = BASE_DIR / "data" / "moderator.db"
    log_level: str = "INFO"

    @property
    def may_delete(self) -> bool:
        return self.mode in ("soft", "enforce")

    @property
    def may_ban(self) -> bool:
        return self.mode == "enforce"


def load_config(env_file: Path | None = None) -> Config:
    load_dotenv(env_file or BASE_DIR / ".env")

    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token or ":" not in token:
        raise SystemExit(
            "Не найден BOT_TOKEN. Скопируйте .env.example в .env и впишите токен от @BotFather."
        )

    mode = (os.getenv("MODE") or "observe").strip().lower()
    if mode not in MODES:
        raise SystemExit(f"MODE должен быть одним из: {', '.join(MODES)}")

    admin_raw = (os.getenv("ADMIN_ID") or "").strip()
    admin_id = int(admin_raw) if admin_raw.lstrip("-").isdigit() else None

    db_path = Path(os.getenv("DB_PATH") or "data/moderator.db")
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    return Config(
        token=token,
        mode=mode,
        chat_ids=_ids(os.getenv("CHAT_IDS")),
        admin_id=admin_id,
        notify_admin=_bool(os.getenv("NOTIFY_ADMIN"), True) and admin_id is not None,
        whitelist_ids=_ids(os.getenv("WHITELIST_IDS")),
        store_message_text=_bool(os.getenv("STORE_MESSAGE_TEXT"), True),
        delete_service_messages=_bool(os.getenv("DELETE_SERVICE_MESSAGES"), True),
        daily_report_hour=_hour(os.getenv("DAILY_REPORT_HOUR"), 10),
        log_retention_days=_int(os.getenv("LOG_RETENTION_DAYS"), 90),
        block_newcomer_media=_bool(os.getenv("BLOCK_NEWCOMER_MEDIA"), False),
        db_path=db_path,
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
    )
