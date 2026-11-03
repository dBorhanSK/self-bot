# -*- coding: utf-8 -*-
"""Self-bot entry point: health server + Telethon user client + (optional) control-panel bot + scheduler."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import urllib.request

from commands import Auth, Commands
from core import Config
from engine import Engine

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("selfbot")

STATE = {"online": False, "error": "", "jobs": 0}


def env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return int(default)


# ── tiny dependency-free HTTP server: answers instantly so Render's proxy is always happy ──
async def http_handler(reader, writer):
    try:
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        body = json.dumps({"status": "ok", **STATE}).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                     b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


async def keepalive(url: str, every: int):
    def ping():
        urllib.request.urlopen(url.rstrip("/") + "/health", timeout=8).read()
    while True:
        await asyncio.sleep(every)
        try:
            await asyncio.to_thread(ping)
        except Exception as ex:
            log.debug("keepalive failed: %s", ex)


async def main():
    port = env_int("PORT", 10000)
    await asyncio.start_server(http_handler, "0.0.0.0", port)
    log.info("health server on :%s", port)

    api_id, api_hash = env_int("API_ID", 0), os.getenv("API_HASH", "").strip()
    session = os.getenv("SESSION_STRING", "").strip()
    if not (api_id and api_hash and session):
        STATE["error"] = "API_ID / API_HASH / SESSION_STRING are not set"
        log.error(STATE["error"])
        await asyncio.Event().wait()

    public = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    if public:
        asyncio.create_task(keepalive(public, max(120, env_int("KEEPALIVE_SECONDS", 240))))

    # A second instance using the same session at the same time (e.g. during a deploy) gets the
    # session killed by Telegram. Give the old instance time to exit first.
    delay = env_int("STARTUP_DELAY", 20)
    log.info("waiting %ss before connecting (old-instance safety)", delay)
    await asyncio.sleep(delay)

    from telethon import TelegramClient
    from telethon.sessions import StringSession
    import panel as panel_mod
    import tg

    cfg = Config(min_interval=env_int("MIN_INTERVAL", 20), max_jobs=env_int("MAX_JOBS", 40),
                 margin_s=float(os.getenv("MARGIN", "2")), global_gap_s=float(os.getenv("GLOBAL_GAP", "1.2")))
    prefix = os.getenv("PREFIX", ".")

    if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"):
        from store import SupabaseStore
        store = SupabaseStore(os.environ["SUPABASE_URL"].strip(), os.environ["SUPABASE_SERVICE_ROLE_KEY"].strip())
    else:
        from store import MemoryStore
        store = MemoryStore()
        log.warning("SUPABASE_* not set: jobs will be lost on restart!")

    client = TelegramClient(StringSession(session), api_id, api_hash, connection_retries=None, retry_delay=2,
                            auto_reconnect=True, flood_sleep_threshold=30)
    await client.connect()
    if not await client.is_user_authorized():
        STATE["error"] = "SESSION_STRING is invalid or revoked. Generate a new one with gen_session.py"
        log.error(STATE["error"])
        await asyncio.Event().wait()
    me = await client.get_me()
    owner_ids = {me.id} | {int(x) for x in re.split(r"[,\s]+", os.getenv("OWNER_ID", "")) if x.isdigit()}
    log.info("logged in as %s (%s); owners=%s", me.first_name, me.id, owner_ids)
    try:
        await client.get_dialogs(limit=300)   # warms the entity cache so numeric chat ids resolve after restarts
    except Exception:
        log.warning("dialog preload failed", exc_info=True)

    bot = None
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if bot_token:
        bot = TelegramClient(StringSession(), api_id, api_hash, connection_retries=None, retry_delay=2)
        await bot.start(bot_token=bot_token)

    async def notify(text: str):
        for oid in owner_ids:
            for target in ((bot, oid), (client, "me" if oid == me.id else oid)):
                if target[0] is None:
                    continue
                try:
                    await target[0].send_message(target[1], text, parse_mode="html")
                    break
                except Exception:
                    continue

    sender = tg.TelethonSender(client)
    auth = Auth(owner_ids, await store.list_admins())
    engine = Engine(store, sender, notify, cfg)
    await engine.load()
    commands = Commands(engine, store, sender, auth, cfg, prefix)
    await commands.load()
    tg.register_userbot(client, commands, auth, prefix)
    if bot:
        panel_mod.Panel(bot, engine, store, sender, auth, cfg).register()
        log.info("control panel bot is up")

    STATE.update(online=True, error="", jobs=len(engine.jobs))
    asyncio.create_task(engine.loop())
    await notify(f"🟢 سلف‌بات روشن شد.\n👤 {me.first_name} · {len(engine.jobs)} کار بارگذاری شد.\nراهنما: <code>{prefix}help</code>")

    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    async def watchdog():
        while not stop.is_set():
            STATE["jobs"] = sum(1 for j in engine.jobs.values() if j.status == "active")
            STATE["online"] = client.is_connected()
            if not client.is_connected():
                try:
                    await client.connect()
                except Exception:
                    log.exception("reconnect failed")
            await asyncio.sleep(15)

    asyncio.create_task(watchdog())
    await stop.wait()
    log.info("shutting down")
    engine.stop()
    await client.disconnect()
    if bot:
        await bot.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
