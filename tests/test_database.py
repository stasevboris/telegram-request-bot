"""Standard-library tests for the persistence layer: python -m unittest discover -s tests."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "test.sqlite3")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_persists_consent_session_and_lead(self) -> None:
        await self.db.accept_consent(111, "client", "Client Name")
        self.assertTrue((await self.db.get_user(111))["consent_at"])

        await self.db.save_session(111, "collect_stack", {"service_key": "debugging", "attachments": []})
        session = await self.db.get_session(111)
        self.assertEqual(session["state"], "collect_stack")
        self.assertEqual(session["data"]["service_key"], "debugging")

        lead = await self.db.create_lead({
            "telegram_id": 111,
            "username": "client",
            "display_name": "Client Name",
            "service_key": "debugging",
            "stack": "Python",
            "deadline": "Friday",
            "expected_budget": "1500 руб",
            "description": "Review a script",
            "attachments": [{"message_id": 15, "type": "document"}],
        })
        self.assertEqual(lead["status"], "new")
        self.assertEqual(lead["expected_budget"], "1500 руб")
        self.assertEqual(len(lead["attachments"]), 1)
        self.assertEqual(len(await self.db.pending_google_sync()), 1)

    async def test_marketing_broadcast_requires_separate_opt_in(self) -> None:
        await self.db.accept_consent(225, "subscriber", "Subscriber")
        await self.db.accept_consent(226, "no_ads", "No Ads")
        self.assertEqual(await self.db.marketing_recipients(), [])

        self.assertTrue(await self.db.set_marketing_subscription(225, True))
        self.assertEqual(await self.db.marketing_recipients(), [225])
        self.assertTrue(await self.db.marketing_subscription_state(225))
        await self.db.set_marketing_subscription(225, False)
        self.assertEqual(await self.db.marketing_recipients(), [])

    async def test_broadcast_draft_can_be_consumed_only_once(self) -> None:
        draft_id = await self.db.create_broadcast_draft(999, "Status update")
        consumed = await self.db.consume_broadcast_draft(draft_id, 999)
        self.assertEqual(consumed["message_text"], "Status update")
        self.assertIsNone(await self.db.consume_broadcast_draft(draft_id, 999))
        self.assertIsNone(await self.db.get_broadcast_draft(draft_id, 999))

    async def test_status_updates_reset_sync_and_export(self) -> None:
        await self.db.accept_consent(222, None, "Second")
        lead = await self.db.create_lead({
            "telegram_id": 222, "service_key": "automation", "description": "Automate a report"
        })
        lead_id = lead["id"]
        await self.db.mark_google_synced(lead_id)
        self.assertEqual(await self.db.pending_google_sync(), [])

        self.assertTrue(await self.db.update_status(lead_id, "in_progress", 999))
        updated = await self.db.get_lead(lead_id)
        self.assertEqual(updated["status"], "in_progress")
        self.assertIsNone(updated["google_synced_at"])
        self.assertFalse(await self.db.update_status(lead_id, "not-a-status", 999))

        exported = await self.db.export_leads()
        self.assertEqual(exported[0]["attachment_count"], 0)
        self.assertEqual((await self.db.stats())["leads"], 1)

    async def test_expiry_sweeper_removes_stale_drafts_but_keeps_recent(self) -> None:
        await self.db.save_session(441, "collect_description", {"draft": "stale"})
        await self.db.save_session(442, "collect_description", {"draft": "recent"})
        old_time = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat(timespec="seconds")
        with self.db._connection() as conn:
            conn.execute("UPDATE intake_sessions SET updated_at=? WHERE telegram_id=?", (old_time, 441))
        self.assertEqual(await self.db.clear_expired_sessions(72), 1)
        self.assertIsNone(await self.db.get_session(441))
        self.assertEqual((await self.db.get_session(442))["data"]["draft"], "recent")

    async def test_stale_intake_draft_expires_after_72_hours(self) -> None:
        await self.db.save_session(444, "collect_description", {"draft": "temporary"})
        old_time = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat(timespec="seconds")
        with self.db._connection() as conn:
            conn.execute("UPDATE intake_sessions SET updated_at=? WHERE telegram_id=?", (old_time, 444))
        self.assertIsNone(await self.db.get_session(444))

    async def test_user_deletion_cascades_and_cleans_audit_events(self) -> None:
        await self.db.accept_consent(333, "deleter", "Delete Me")
        await self.db.set_marketing_subscription(333, True)
        await self.db.save_session(333, "collect_description", {"draft": "secret"})
        lead = await self.db.create_lead({
            "telegram_id": 333, "service_key": "mentoring", "description": "private description",
            "attachments": [{"message_id": 45, "type": "photo"}],
        })
        await self.db.add_admin_copy(lead["id"], -100123, 77)

        deleted = await self.db.delete_user_data(333)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["attachments"][0]["message_id"], 45)
        self.assertEqual(deleted[0]["admin_copies"][0]["message_id"], 77)
        self.assertIsNone(await self.db.get_user(333))
        self.assertEqual(await self.db.marketing_recipients(), [])
        self.assertEqual(await self.db.user_leads(333), [])
        self.assertIsNone(await self.db.get_session(333))
        self.assertEqual((await self.db.stats())["leads"], 0)
        with self.db._connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM admin_events").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
