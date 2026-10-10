import argparse
import asyncio
import logging
import json
import sys
import os
from pathlib import Path
import time

from jmemby.config import Settings
from jmemby.db.store import Store
from jmemby.integrations.komga.bridge import KomgaBridge
from jmemby.providers.command import CommandProvider
from jmemby.runtime import JsonFormatter, backup_database, database_path, instance_lock
from jmemby.services.tasks import TaskService
from jmemby.workers.queue import Worker


async def run(settings, db_path):
    from aiogram import Bot
    from jmemby.bot.app import create_dispatcher, notifications
    provider = CommandProvider(settings.provider_command, settings.operation_timeout)
    komga = KomgaBridge(komga_command(), settings.operation_timeout)
    store = Store(db_path)
    bot = Bot(settings.telegram_bot_token)
    worker = Worker(store, provider, komga, settings)
    dispatcher = create_dispatcher(TaskService(store, provider, settings.provider_domains), settings)
    heartbeat = db_path.parent / "heartbeat"

    async def health():
        while True:
            if time.monotonic() - worker.heartbeat < 30:
                heartbeat.write_text(str(time.time()))
            await asyncio.sleep(5)

    jobs = [asyncio.create_task(worker.run()), asyncio.create_task(notifications(bot, store, settings.komga_public_url)),
            asyncio.create_task(health()), asyncio.create_task(dispatcher.start_polling(bot))]
    try:
        done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        for job in done:
            job.result()
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await bot.session.close()
        store.close()
        heartbeat.unlink(missing_ok=True)


def komga_command():
    return os.getenv("KOMGA_COMMAND") or json.dumps([sys.executable, "-m", "jmemby.integrations.komga.adapter"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "check-config", "check-integrations", "health", "backup"], nargs="?", default="run")
    parser.add_argument("--destination")
    args = parser.parse_args()
    db_path = database_path(os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/app/jmemby.db"))
    if args.command == "health":
        try:
            alive = time.time() - float((db_path.parent / "heartbeat").read_text()) < 60
        except (OSError, ValueError):
            alive = False
        raise SystemExit(0 if alive else 1)
    if args.command == "backup":
        if not args.destination:
            parser.error("--destination is required")
        backup_database(db_path, args.destination)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(handlers=[handler], level=logging.INFO)
    try:
        settings = Settings.from_env()
        if not os.getenv("KOMGA_COMMAND"):
            from jmemby.integrations.komga.adapter import from_env
            from_env()  # Validate required credentials/root without a network request.
        if args.command == "check-integrations":
            import jmcomic
            if not hasattr(jmcomic, "JmOption"):
                raise ValueError("Unsupported jmcomic SDK")
            asyncio.run(KomgaBridge(komga_command(), settings.operation_timeout).request({"operation": "check"}))
            print("JMComic SDK import and Komga library check passed; no manga was downloaded")
            return
        if args.command == "check-config":
            print("Configuration valid; external integrations are not verified by this check")
            return
        with instance_lock(db_path.parent / "instance.lock"):
            asyncio.run(run(settings, db_path))
    except (ValueError, RuntimeError) as exc:
        parser.exit(2, f"Startup failed: {exc}\n")


if __name__ == "__main__":
    main()
