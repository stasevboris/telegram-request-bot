"""SQLite persistence layer. Uses only the Python standard library.

Each operation runs in a worker thread so disk I/O does not block aiogram's loop.
The DB is the source of truth; Google Sheets is a recoverable reporting mirror.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")

STATUSES: dict[str, str] = {
    "new": "Новая",
    "contacted": "Связался",
    "quoted": "Оценка отправлена",
    "accepted": "Согласовано",
    "in_progress": "В работе",
    "delivered": "Передано клиенту",
    "closed": "Закрыта",
    "declined": "Отказ",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 20000")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def _connection(self):
        """Commit/rollback and always close; sqlite Connection.__exit__ alone does not close."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.to_thread(fn, *args)

    async def initialize(self) -> None:
        await self._run(self._initialize)

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "posix":
                self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    display_name TEXT NOT NULL DEFAULT '',
                    consent_at TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS intake_sessions (
                    telegram_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    username TEXT,
                    display_name TEXT NOT NULL DEFAULT '',
                    service_key TEXT NOT NULL,
                    stack TEXT NOT NULL DEFAULT '',
                    deadline TEXT NOT NULL DEFAULT '',
                    expected_budget TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    extra_notes TEXT NOT NULL DEFAULT '',
                    attachment_messages_json TEXT NOT NULL DEFAULT '[]',
                    admin_copies_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'new',
                    source TEXT NOT NULL DEFAULT 'telegram',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    google_synced_at TEXT,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_leads_created_at ON leads(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_leads_status_created ON leads(status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_leads_telegram_id ON leads(telegram_id);

                CREATE TABLE IF NOT EXISTS admin_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER,
                    admin_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS marketing_subscriptions (
                    telegram_id INTEGER PRIMARY KEY,
                    consent_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (telegram_id) REFERENCES users(telegram_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS broadcast_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER NOT NULL,
                    message_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            lead_columns = {row["name"] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
            if "expected_budget" not in lead_columns:
                conn.execute("ALTER TABLE leads ADD COLUMN expected_budget TEXT NOT NULL DEFAULT ''")
        try:
            if os.name == "posix":
                self.path.chmod(0o600)
        except OSError:
            pass

    async def get_user(self, telegram_id: int) -> dict[str, Any] | None:
        return await self._run(self._get_user, telegram_id)

    def _get_user(self, telegram_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            return dict(row) if row else None

    async def accept_consent(self, telegram_id: int, username: str | None, display_name: str) -> None:
        await self._run(self._accept_consent, telegram_id, username, display_name)

    def _accept_consent(self, telegram_id: int, username: str | None, display_name: str) -> None:
        now = utc_now()
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO users (telegram_id, username, display_name, consent_at, created_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                     username=excluded.username,
                     display_name=excluded.display_name,
                     consent_at=COALESCE(users.consent_at, excluded.consent_at),
                     last_seen_at=excluded.last_seen_at""",
                (telegram_id, username, display_name, now, now, now),
            )

    async def touch_user(self, telegram_id: int, username: str | None, display_name: str) -> None:
        await self._run(self._touch_user, telegram_id, username, display_name)

    def _touch_user(self, telegram_id: int, username: str | None, display_name: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE users SET username=?, display_name=?, last_seen_at=? WHERE telegram_id=?",
                (username, display_name, utc_now(), telegram_id),
            )

    async def save_session(self, telegram_id: int, state: str, data: dict[str, Any]) -> None:
        await self._run(self._save_session, telegram_id, state, data)

    def _save_session(self, telegram_id: int, state: str, data: dict[str, Any]) -> None:
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO intake_sessions (telegram_id, state, data_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                     state=excluded.state, data_json=excluded.data_json, updated_at=excluded.updated_at""",
                (telegram_id, state, _json(data), utc_now()),
            )

    async def get_session(self, telegram_id: int) -> dict[str, Any] | None:
        return await self._run(self._get_session, telegram_id)

    def _get_session(self, telegram_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM intake_sessions WHERE telegram_id=?", (telegram_id,)).fetchone()
            if not row:
                return None
            try:
                updated_at = datetime.fromisoformat(row["updated_at"])
                if datetime.now(timezone.utc) - updated_at > timedelta(hours=72):
                    conn.execute("DELETE FROM intake_sessions WHERE telegram_id=?", (telegram_id,))
                    return None
            except (TypeError, ValueError):
                # A malformed draft should not block the user from starting over.
                conn.execute("DELETE FROM intake_sessions WHERE telegram_id=?", (telegram_id,))
                return None
            result = dict(row)
            try:
                result["data"] = json.loads(result.pop("data_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                conn.execute("DELETE FROM intake_sessions WHERE telegram_id=?", (telegram_id,))
                return None
            return result

    async def clear_session(self, telegram_id: int) -> None:
        await self._run(self._clear_session, telegram_id)

    def _clear_session(self, telegram_id: int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM intake_sessions WHERE telegram_id=?", (telegram_id,))

    async def clear_expired_sessions(self, max_age_hours: int = 72) -> int:
        return await self._run(self._clear_expired_sessions, max_age_hours)

    def _clear_expired_sessions(self, max_age_hours: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, max_age_hours))).isoformat(timespec="seconds")
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM intake_sessions WHERE updated_at < ?", (cutoff,))
            return int(cursor.rowcount)

    async def create_lead(self, data: dict[str, Any]) -> dict[str, Any]:
        return await self._run(self._create_lead, data)

    def _create_lead(self, data: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._connection() as conn:
            cursor = conn.execute(
                """INSERT INTO leads (
                    telegram_id, username, display_name, service_key, stack, deadline,
                    expected_budget, description, extra_notes, attachment_messages_json,
                    status, source, created_at, updated_at, google_synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', 'telegram', ?, ?, NULL)""",
                (
                    int(data["telegram_id"]), data.get("username"), data.get("display_name", ""),
                    data["service_key"], data.get("stack", ""), data.get("deadline", ""),
                    data.get("expected_budget", ""), data.get("description", ""),
                    data.get("extra_notes", ""), _json(data.get("attachments", [])), now, now,
                ),
            )
            lead_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO admin_events (lead_id, admin_id, event_type, details, created_at) VALUES (?, 0, 'created', '', ?)",
                (lead_id, now),
            )
            row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
            return self._lead_dict(row)

    @staticmethod
    def _lead_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["attachments"] = json.loads(result.pop("attachment_messages_json") or "[]")
        result["admin_copies"] = json.loads(result.pop("admin_copies_json") or "[]")
        return result

    async def get_lead(self, lead_id: int) -> dict[str, Any] | None:
        return await self._run(self._get_lead, lead_id)

    def _get_lead(self, lead_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            return self._lead_dict(conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())

    async def list_leads(self, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        return await self._run(self._list_leads, limit, status)

    def _list_leads(self, limit: int, status: str | None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._connection() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM leads WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [self._lead_dict(row) for row in rows if row is not None]

    async def update_status(self, lead_id: int, status: str, admin_id: int) -> bool:
        return await self._run(self._update_status, lead_id, status, admin_id)

    def _update_status(self, lead_id: int, status: str, admin_id: int) -> bool:
        if status not in STATUSES:
            return False
        now = utc_now()
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE leads SET status=?, updated_at=?, google_synced_at=NULL WHERE id=?",
                (status, now, lead_id),
            )
            if cursor.rowcount:
                conn.execute(
                    "INSERT INTO admin_events (lead_id, admin_id, event_type, details, created_at) VALUES (?, ?, 'status', ?, ?)",
                    (lead_id, admin_id, status, now),
                )
            return bool(cursor.rowcount)

    async def add_admin_copy(self, lead_id: int, chat_id: int, message_id: int) -> None:
        await self._run(self._add_admin_copy, lead_id, chat_id, message_id)

    def _add_admin_copy(self, lead_id: int, chat_id: int, message_id: int) -> None:
        with self._connection() as conn:
            row = conn.execute("SELECT admin_copies_json FROM leads WHERE id=?", (lead_id,)).fetchone()
            if not row:
                return
            copies = json.loads(row[0] or "[]")
            copies.append({"chat_id": chat_id, "message_id": message_id})
            conn.execute("UPDATE leads SET admin_copies_json=? WHERE id=?", (_json(copies), lead_id))

    async def mark_google_synced(self, lead_id: int) -> None:
        await self._run(self._mark_google_synced, lead_id)

    def _mark_google_synced(self, lead_id: int) -> None:
        with self._connection() as conn:
            conn.execute("UPDATE leads SET google_synced_at=? WHERE id=?", (utc_now(), lead_id))

    async def pending_google_sync(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._run(self._pending_google_sync, limit)

    def _pending_google_sync(self, limit: int) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM leads WHERE google_synced_at IS NULL ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
            return [self._lead_dict(row) for row in rows if row is not None]

    async def user_leads(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self._run(self._user_leads, telegram_id)

    def _user_leads(self, telegram_id: int) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM leads WHERE telegram_id=? ORDER BY id", (telegram_id,)).fetchall()
            return [self._lead_dict(row) for row in rows if row is not None]

    async def recent_lead_count(self, telegram_id: int, since_iso: str) -> int:
        return await self._run(self._recent_lead_count, telegram_id, since_iso)

    def _recent_lead_count(self, telegram_id: int, since_iso: str) -> int:
        with self._connection() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM leads WHERE telegram_id=? AND created_at>=?",
                (telegram_id, since_iso),
            ).fetchone()[0])

    async def stats(self) -> dict[str, Any]:
        return await self._run(self._stats)

    def _stats(self) -> dict[str, Any]:
        with self._connection() as conn:
            users = conn.execute("SELECT COUNT(*) FROM users WHERE consent_at IS NOT NULL").fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM leads GROUP BY status").fetchall()
            pending = conn.execute("SELECT COUNT(*) FROM leads WHERE google_synced_at IS NULL").fetchone()[0]
            opted_in = conn.execute("SELECT COUNT(*) FROM marketing_subscriptions WHERE is_active=1").fetchone()[0]
            return {
                "users": users, "leads": total,
                "by_status": {row["status"]: row["n"] for row in rows},
                "sheets_pending": pending, "marketing_opt_ins": opted_in,
            }

    async def set_marketing_subscription(self, telegram_id: int, active: bool) -> bool:
        return await self._run(self._set_marketing_subscription, telegram_id, active)

    def _set_marketing_subscription(self, telegram_id: int, active: bool) -> bool:
        now = utc_now()
        with self._connection() as conn:
            user = conn.execute(
                "SELECT consent_at FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if not user or not user["consent_at"]:
                return False
            if active:
                conn.execute(
                    """INSERT INTO marketing_subscriptions (telegram_id, consent_at, is_active, updated_at)
                       VALUES (?, ?, 1, ?)
                       ON CONFLICT(telegram_id) DO UPDATE SET
                         consent_at=excluded.consent_at, is_active=1, updated_at=excluded.updated_at""",
                    (telegram_id, now, now),
                )
            else:
                conn.execute(
                    "UPDATE marketing_subscriptions SET is_active=0, updated_at=? WHERE telegram_id=?",
                    (now, telegram_id),
                )
            return True

    async def marketing_subscription_state(self, telegram_id: int) -> bool:
        return await self._run(self._marketing_subscription_state, telegram_id)

    def _marketing_subscription_state(self, telegram_id: int) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT is_active FROM marketing_subscriptions WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            return bool(row and row["is_active"])

    async def marketing_recipients(self) -> list[int]:
        return await self._run(self._marketing_recipients)

    def _marketing_recipients(self) -> list[int]:
        with self._connection() as conn:
            rows = conn.execute(
                """SELECT s.telegram_id FROM marketing_subscriptions s
                   JOIN users u ON u.telegram_id=s.telegram_id
                   WHERE s.is_active=1 AND u.consent_at IS NOT NULL
                   ORDER BY s.telegram_id"""
            ).fetchall()
            return [int(row["telegram_id"]) for row in rows]

    async def deactivate_marketing_subscription(self, telegram_id: int) -> None:
        await self._run(self._deactivate_marketing_subscription, telegram_id)

    def _deactivate_marketing_subscription(self, telegram_id: int) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE marketing_subscriptions SET is_active=0, updated_at=? WHERE telegram_id=?",
                (utc_now(), telegram_id),
            )

    async def create_broadcast_draft(self, admin_id: int, message_text: str) -> int:
        return await self._run(self._create_broadcast_draft, admin_id, message_text)

    def _create_broadcast_draft(self, admin_id: int, message_text: str) -> int:
        with self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO broadcast_drafts (admin_id, message_text, created_at) VALUES (?, ?, ?)",
                (admin_id, message_text, utc_now()),
            )
            return int(cursor.lastrowid)

    async def get_broadcast_draft(self, draft_id: int, admin_id: int) -> dict[str, Any] | None:
        return await self._run(self._get_broadcast_draft, draft_id, admin_id)

    def _get_broadcast_draft(self, draft_id: int, admin_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM broadcast_drafts WHERE id=? AND admin_id=?", (draft_id, admin_id)
            ).fetchone()
            return dict(row) if row else None

    async def consume_broadcast_draft(self, draft_id: int, admin_id: int) -> dict[str, Any] | None:
        return await self._run(self._consume_broadcast_draft, draft_id, admin_id)

    def _consume_broadcast_draft(self, draft_id: int, admin_id: int) -> dict[str, Any] | None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM broadcast_drafts WHERE id=? AND admin_id=?", (draft_id, admin_id)
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            conn.execute("DELETE FROM broadcast_drafts WHERE id=? AND admin_id=?", (draft_id, admin_id))
            return result

    async def delete_broadcast_draft(self, draft_id: int, admin_id: int) -> None:
        await self._run(self._delete_broadcast_draft, draft_id, admin_id)

    def _delete_broadcast_draft(self, draft_id: int, admin_id: int) -> None:
        with self._connection() as conn:
            conn.execute("DELETE FROM broadcast_drafts WHERE id=? AND admin_id=?", (draft_id, admin_id))

    async def clear_expired_broadcast_drafts(self, max_age_hours: int = 24) -> int:
        return await self._run(self._clear_expired_broadcast_drafts, max_age_hours)

    def _clear_expired_broadcast_drafts(self, max_age_hours: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, max_age_hours))).isoformat(timespec="seconds")
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM broadcast_drafts WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount)

    async def export_leads(self, limit: int = 1000) -> list[dict[str, Any]]:
        return await self._run(self._export_leads, limit)

    def _export_leads(self, limit: int) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (max(1, min(limit, 10000)),)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["attachment_count"] = len(json.loads(item.pop("attachment_messages_json") or "[]"))
                item.pop("admin_copies_json", None)
                result.append(item)
            return result

    async def delete_user_data(self, telegram_id: int) -> list[dict[str, Any]]:
        return await self._run(self._delete_user_data, telegram_id)

    def _delete_user_data(self, telegram_id: int) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM leads WHERE telegram_id=?", (telegram_id,)).fetchall()
            leads = [self._lead_dict(row) for row in rows if row is not None]
            lead_ids = [lead["id"] for lead in leads]
            if lead_ids:
                placeholders = ",".join("?" for _ in lead_ids)
                conn.execute(f"DELETE FROM admin_events WHERE lead_id IN ({placeholders})", lead_ids)
            conn.execute("DELETE FROM users WHERE telegram_id=?", (telegram_id,))
            conn.execute("DELETE FROM intake_sessions WHERE telegram_id=?", (telegram_id,))
            return leads
