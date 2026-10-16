"""Configuration loaded from environment variables / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _parse_ids(raw: str) -> frozenset[int]:
    result: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            try:
                result.add(int(item))
            except ValueError as exc:
                raise ValueError(f"ADMIN_IDS contains a non-numeric value: {item!r}") from exc
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    admin_chat_id: int | None
    db_path: Path
    contact_username: str
    google_sheet_id: str | None
    google_credentials_file: Path | None
    google_worksheet: str
    sync_interval_seconds: int
    max_attachments: int
    max_attachment_bytes: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        admin_ids = _parse_ids(os.getenv("ADMIN_IDS", ""))
        admin_chat_raw = os.getenv("ADMIN_CHAT_ID", "").strip()
        google_sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip() or None
        credentials_raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        credentials_file = Path(credentials_raw).expanduser() if credentials_raw else None
        if credentials_file and not credentials_file.is_absolute():
            credentials_file = BASE_DIR / credentials_file
        db_path = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "bot.sqlite3"))).expanduser()
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        settings = cls(
            bot_token=token,
            admin_ids=admin_ids,
            admin_chat_id=int(admin_chat_raw) if admin_chat_raw else None,
            db_path=db_path,
            contact_username=os.getenv("CONTACT_USERNAME", "").strip().lstrip("@"),
            google_sheet_id=google_sheet_id,
            google_credentials_file=credentials_file,
            google_worksheet=os.getenv("GOOGLE_WORKSHEET", "Заявки").strip() or "Заявки",
            sync_interval_seconds=max(60, int(os.getenv("SHEETS_SYNC_INTERVAL_SECONDS", "300"))),
            max_attachments=max(1, min(10, int(os.getenv("MAX_ATTACHMENTS", "5")))),
            max_attachment_bytes=max(1, int(os.getenv("MAX_ATTACHMENT_MB", "20"))) * 1024 * 1024,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise ValueError("Set BOT_TOKEN in telegram_bot/.env. Get it from @BotFather.")
        if not self.admin_ids:
            raise ValueError("Set ADMIN_IDS to your numeric Telegram user ID (not a username).")
        if self.admin_chat_id is not None and self.admin_chat_id == 0:
            raise ValueError("ADMIN_CHAT_ID must be a valid chat ID or left empty.")
        if bool(self.google_sheet_id) != bool(self.google_credentials_file):
            raise ValueError("Set both GOOGLE_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE, or leave both empty.")
        if self.google_credentials_file and not self.google_credentials_file.is_file():
            raise ValueError(f"Google service-account file does not exist: {self.google_credentials_file}")
        # Google Sheets is deliberately optional at startup when both settings are
        # empty; once configured, temporary API outages do not stop SQLite storage.

    @property
    def google_sheets_enabled(self) -> bool:
        return bool(self.google_sheet_id and self.google_credentials_file)
