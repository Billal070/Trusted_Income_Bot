"""
Entry point - runs both User Bot and Admin Bot in a single async process.

Uses python-telegram-bot v21+ non-blocking API:
  app.initialize() -> app.start() -> app.updater.start_polling()
Both bots share one asyncio event loop.
"""

import asyncio
import logging
import os
import signal
import threading

from database import init_db
from user_bot import build_user_bot
from admin_bot import build_admin_bot

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

def _start_webhook():
    try:
        from webhook import app as webhook_app
        port = int(os.getenv("PORT", "8080"))
        logger.info("Starting webhook on port %s", port)
        webhook_app.run(host="0.0.0.0", port=port, use_reloader=False)
    except Exception as e:
        logger.warning("Webhook failed to start: %s", e)


async def main():
    init_db()
    logger.info("Database initialized.")

    # Start SMS webhook in background thread (for auto payment verification)
    threading.Thread(target=_start_webhook, daemon=True).start()

    user_app = build_user_bot()
    admin_app = build_admin_bot()

    await user_app.initialize()
    await admin_app.initialize()
    await user_app.start()
    await admin_app.start()
    await user_app.updater.start_polling(drop_pending_updates=True)
    await admin_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Both bots are running.")

    # Wait for a shutdown signal
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await stop_event.wait()

    logger.info("Shutting down...")
    await user_app.updater.stop()
    await admin_app.updater.stop()
    await user_app.stop()
    await admin_app.stop()
    await user_app.shutdown()
    await admin_app.shutdown()
    logger.info("Both bots stopped.")


if __name__ == "__main__":
    asyncio.run(main())