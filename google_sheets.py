"""Google Sheets mirror. SQLite remains authoritative if Sheets is unavailable."""
from __future__ import annotations

import asyncio
from typing import Any

from config import Settings
from database import STATUSES

HEADERS = [
    "lead_id", "created_at_utc", "telegram_id", "username", "display_name",
    "service_key", "stack", "deadline", "description", "extra_notes",
    "status", "attachment_count", "source", "expected_budget",
]


class GoogleSheetsMirror:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._sync_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.settings.google_sheets_enabled

    def _worksheet(self):
        if not self.enabled:
            raise RuntimeError("Google Sheets is not configured")
        # Lazy imports keep the bot usable when Google Sheets is not configured.
        import gspread

        client = gspread.service_account(filename=str(self.settings.google_credentials_file))
        spreadsheet = client.open_by_key(self.settings.google_sheet_id)
        return spreadsheet.worksheet(self.settings.google_worksheet)

    @staticmethod
    def _row(lead: dict[str, Any]) -> list[str | int]:
        return [
            int(lead["id"]), lead.get("created_at", ""), int(lead["telegram_id"]),
            lead.get("username") or "", lead.get("display_name") or "",
            lead.get("service_key", ""), lead.get("stack", ""), lead.get("deadline", ""),
            lead.get("description", ""), lead.get("extra_notes", ""),
            STATUSES.get(lead.get("status", "new"), lead.get("status", "")),
            len(lead.get("attachments", [])), lead.get("source", "telegram"),
            lead.get("expected_budget", ""),
        ]

    def _sync_lead_sync(self, lead: dict[str, Any]) -> bool:
        ws = self._worksheet()
        values = ws.get_all_values()
        if not values:
            ws.update(values=[HEADERS], range_name="A1:N1")
            values = [HEADERS]
        header = values[0]
        legacy_headers = HEADERS[:-1]
        if len(header) == len(legacy_headers) and header == legacy_headers:
            # Backward-compatible migration: the budget column is new and appended.
            ws.update(values=[[HEADERS[-1]]], range_name="N1", value_input_option="RAW")
            header = [*header, HEADERS[-1]]
            values[0] = header
        if header[: len(HEADERS)] != HEADERS:
            raise ValueError(
                "Google worksheet header mismatch. Use the exact header row from README_RU.md."
            )

        lead_id = str(lead["id"])
        row_number = next(
            (index for index, row in enumerate(values[1:], start=2) if row and row[0] == lead_id),
            None,
        )
        row = self._row(lead)
        if row_number is None:
            ws.append_row(row, value_input_option="RAW")
        else:
            ws.update(values=[row], range_name=f"A{row_number}:N{row_number}", value_input_option="RAW")
        return True

    async def sync_lead(self, lead: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        async with self._sync_lock:
            return await asyncio.to_thread(self._sync_lead_sync, lead)

    def _delete_user_sync(self, telegram_id: int) -> int:
        ws = self._worksheet()
        values = ws.get_all_values()
        if not values:
            return 0
        header = values[0]
        if "telegram_id" not in header:
            raise ValueError("No telegram_id column in Google worksheet")
        column = header.index("telegram_id")
        rows = [
            index for index, row in enumerate(values[1:], start=2)
            if len(row) > column and row[column] == str(telegram_id)
        ]
        for row_number in reversed(rows):
            ws.delete_rows(row_number)
        return len(rows)

    async def delete_user_rows(self, telegram_id: int) -> int:
        if not self.enabled:
            return 0
        async with self._sync_lock:
            return await asyncio.to_thread(self._delete_user_sync, telegram_id)
