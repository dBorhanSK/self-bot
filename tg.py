# -*- coding: utf-8 -*-
"""Telethon glue: the account-bound sender and userbot event handlers."""
from __future__ import annotations

import asyncio
import logging
import re

from commands import Auth, Commands, Ctx
from engine import SendError

log = logging.getLogger("selfbot.tg")

FATAL_NAMES = {"ChatWriteForbiddenError", "UserBannedInChannelError", "ChannelPrivateError", "ChatAdminRequiredError",
               "UserIsBlockedError", "ChatRestrictedError", "UsernameNotOccupiedError", "UsernameInvalidError",
               "InputUserDeactivatedError", "ChatGuestSendForbiddenError", "PeerIdInvalidError", "ChannelInvalidError"}
FLOOD_NAMES = {"FloodWaitError", "SlowModeWaitError", "FloodError"}
FA_REASON = {"ChatWriteForbiddenError": "اجازه ارسال پیام در این چت رو نداری",
             "UserBannedInChannelError": "اکانت در این گروه بن/محدود شده",
             "ChannelPrivateError": "گروه خصوصیه یا عضوش نیستی",
             "UserIsBlockedError": "ربات/کاربر اکانت رو بلاک کرده"}


def map_error(ex: Exception) -> SendError:
    if isinstance(ex, SendError):
        return ex
    name = type(ex).__name__
    if name in FLOOD_NAMES:
        return SendError("flood", name, int(getattr(ex, "seconds", 60) or 60))
    if name == "PeerFloodError":
        return SendError("fatal", "تلگرام اکانت رو به‌خاطر اسپم محدود کرده (PeerFlood). از @SpamBot وضعیت رو بررسی کن.")
    if name in FATAL_NAMES:
        return SendError("fatal", FA_REASON.get(name, name))
    if isinstance(ex, ValueError):
        return SendError("fatal", "چت پیدا نشد؛ مطمئن شو اکانت عضوشه یا آیدی/لینک درسته.")
    if isinstance(ex, (ConnectionError, asyncio.TimeoutError, OSError)):
        return SendError("temp", f"مشکل شبکه: {type(ex).__name__}")
    return SendError("temp", f"{name}: {str(ex)[:120]}")


class TelethonSender:
    def __init__(self, client):
        self.c = client
        self._cache: dict[str, object] = {}

    async def entity(self, ref):
        key = str(ref)
        if key in self._cache:
            return self._cache[key]
        try:
            if re.fullmatch(r"-?\d+", key):
                ent = await self.c.get_entity(int(key))
            elif "t.me/+" in key or "joinchat/" in key:
                ent = await self._invite(key)
            else:
                ent = await self.c.get_entity(key)
        except Exception as ex:
            raise map_error(ex)
        self._cache[key] = ent
        return ent

    async def _invite(self, link):
        from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
        h = re.search(r"(?:\+|joinchat/)([\w-]+)", link).group(1)
        try:
            upd = await self.c(ImportChatInviteRequest(h))
            return upd.chats[0]
        except Exception as ex:
            if type(ex).__name__ == "UserAlreadyParticipantError":
                return (await self.c(CheckChatInviteRequest(h))).chat
            raise

    async def send_text(self, chat_ref, text):
        ent = await self.entity(chat_ref)
        try:
            await self.c.send_message(ent, text, parse_mode=None)
        except Exception as ex:
            raise map_error(ex)

    async def tap(self, chat_ref, bot_ref, btn=""):
        ent, bot = await self.entity(chat_ref), await self.entity(bot_ref)
        try:
            msgs = await self.c.get_messages(ent, limit=20, from_user=bot)
            for m in msgs:
                flat = [b for row in (m.buttons or []) for b in row]
                if not flat:
                    continue
                if btn:
                    if not any(btn in (b.text or "") for b in flat):
                        continue
                    await m.click(filter=lambda b, btn=btn: btn in (b.text or ""))
                else:
                    await m.click(0)
                return
        except Exception as ex:
            if type(ex).__name__ == "BotResponseTimeoutError":
                return  # the click was delivered; the bot just didn't answer the callback
            raise map_error(ex)
        raise SendError("temp", "پیامی با این دکمه از ربات پیدا نشد")

    async def ask_bot(self, bot_ref, text):
        ent = await self.entity(bot_ref)
        try:
            async with self.c.conversation(ent, timeout=15) as conv:
                await conv.send_message(text)
                r = await conv.get_response()
        except asyncio.TimeoutError:
            raise SendError("temp", "ربات تا ۱۵ ثانیه جواب نداد.")
        except Exception as ex:
            raise map_error(ex)
        return (r.text or ""), [b.text for row in (r.buttons or []) for b in row]

    async def perform(self, job):
        try:
            if job.kind == "click":
                await self.tap(job.chat_ref, job.extra.get("bot", job.chat_ref), job.extra.get("btn", ""))
            else:
                await self.send_text(job.chat_ref, job.pick_text())
        except Exception as ex:
            raise map_error(ex)

    async def join(self, ref):
        try:
            if "t.me/+" in ref or "joinchat/" in ref:
                await self._invite(ref)
            else:
                from telethon.tl.functions.channels import JoinChannelRequest
                await self.c(JoinChannelRequest(await self.entity(ref)))
        except Exception as ex:
            raise map_error(ex)

    async def leave(self, ref):
        try:
            await self.c.delete_dialog(await self.entity(ref))
        except Exception as ex:
            raise map_error(ex)

    async def purge(self, chat_id, n):
        ids = [m.id async for m in self.c.iter_messages(chat_id, from_user="me", limit=n + 1)]
        if ids:
            await self.c.delete_messages(chat_id, ids)

    async def recent_groups(self, limit=60):
        out = []
        async for d in self.c.iter_dialogs(limit=150):
            if d.is_group or d.is_channel:
                out.append((d.id, d.name or str(d.id)))
                if len(out) >= limit:
                    break
        return out


def register_userbot(client, commands: Commands, auth: Auth, prefix: str):
    from telethon import events

    @client.on(events.NewMessage())
    async def on_message(event):
        try:
            text = event.raw_text or ""
            if not text:
                return
            uid = event.sender_id
            if text.startswith(prefix) and auth.role(uid, event.out):
                async def reply(t, _e=event):
                    try:
                        await (_e.edit(t, parse_mode="html") if _e.out else _e.reply(t, parse_mode="html"))
                    except Exception as ex:
                        if "not modified" not in str(ex).lower():
                            raise

                async def replied_user(_e=event):
                    r = await _e.get_reply_message()
                    return r.sender_id if r else None
                chat = await event.get_chat()
                ctx = Ctx(text, uid, event.chat_id, bool(event.out), reply, replied_user, getattr(chat, "title", "") or "")
                await commands.handle(ctx)
                return
            if not event.out and commands.triggers:
                chat = await event.get_chat()
                for resp in commands.match_triggers(event.chat_id, getattr(chat, "username", "") or "", text):
                    await event.reply(resp)
        except Exception:
            log.exception("userbot handler failed")
