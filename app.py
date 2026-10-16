"""Application entry point: polling, routers, and background Google sync retry."""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from admin_handlers import create_admin_router
from client_handlers import create_client_router
from config import Settings
from database import Database
from google_sheets import GoogleSheetsMirror


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    # Reduce request/token-adjacent noise from libraries while retaining warnings.
    logging.getLogger("aiogram.event").setLevel(logging.INFO)


async def session_cleanup_worker(db: Database) -> None:
    """Remove abandoned intake drafts at least hourly; submitted leads are unaffected."""
    log = logging.getLogger(__name__)
    while True:
        try:
            removed = await db.clear_expired_sessions(max_age_hours=72)
            expired_broadcasts = await db.clear_expired_broadcast_drafts(max_age_hours=24)
            if removed:
                log.info("Removed %s intake draft(s) inactive for more than 72 hours", removed)
            if expired_broadcasts:
                log.info("Removed %s unconfirmed broadcast draft(s) older than 24 hours", expired_broadcasts)
        except Exception:
            log.exception("Could not clean expired intake drafts")
        await asyncio.sleep(3600)


async def sheets_retry_worker(db: Database, sheets: GoogleSheetsMirror, interval: int) -> None:
    """Retry unsynchronized rows without blocking client updates."""
    if not sheets.enabled:
        logging.getLogger(__name__).info("Google Sheets is not configured; SQLite remains the active store.")
        return
    log = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(interval)
        try:
            pending = await db.pending_google_sync(100)
        except Exception:
            log.exception("Could not read the pending Google Sheets sync queue")
            continue
        for lead in pending:
            try:
                if await sheets.sync_lead(lead):
                    await db.mark_google_synced(lead["id"])
            except Exception:
                log.exception("Google Sheets retry failed for lead %s", lead["id"])


async def run() -> None:
    configure_logging()
    log = logging.getLogger(__name__)
    settings = Settings.from_env()
    db = Database(settings.db_path)
    await db.initialize()
    sheets = GoogleSheetsMirror(settings)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dispatcher = Dispatcher()
    # Admin commands precede the catch-all client message handler.
    dispatcher.include_router(create_admin_router(db, sheets, settings))
    dispatcher.include_router(create_client_router(db, sheets, settings))

    sync_task: asyncio.Task | None = None
    cleanup_task: asyncio.Task | None = None
    try:
        # This project uses long polling, not webhook. Preserve pending updates while
        # removing any leftover webhook configuration that would block getUpdates.
        await bot.delete_webhook(drop_pending_updates=False)
        identity = await bot.get_me()
        log.info("Bot started as @%s; database=%s", identity.username, settings.db_path)
        if settings.admin_chat_id is None:
            log.info("Lead notifications go to each ADMIN_IDS account. Each admin must start the bot first.")
        else:
            log.info("Lead notifications go to configured ADMIN_CHAT_ID=%s", settings.admin_chat_id)
        if sheets.enabled:
            log.info("Google Sheets synchronization is configured.")

        cleanup_task = asyncio.create_task(session_cleanup_worker(db), name="intake-session-cleanup")
        sync_task = asyncio.create_task(
            sheets_retry_worker(db, sheets, settings.sync_interval_seconds),
            name="google-sheets-retry",
        )
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        for task in (sync_task, cleanup_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        logging.getLogger(__name__).info("Bot stopped.")
    except Exception:
        logging.exception("Bot failed to start")
        raise


if __name__ == "__main__":
    main()
