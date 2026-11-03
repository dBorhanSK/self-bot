# -*- coding: utf-8 -*-
"""Command handling for the self-bot (pure; Telegram access goes through `sender`)."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from core import (Config, ParseError, build_job, describe, fa, fmt_clock, fmt_duration, normalize_chat,
                  parse_click, parse_every)
from engine import Engine, SendError


@dataclass
class Ctx:
    text: str
    sender_id: int
    chat_id: int
    is_out: bool
    reply: Callable[[str], Awaitable]
    replied_user: Callable[[], Awaitable[int | None]]
    chat_title: str = ""


class Auth:
    def __init__(self, owner_ids, admins: dict | None = None):
        self.owner_ids = set(int(x) for x in owner_ids)
        self.admins: dict[int, str] = dict(admins or {})

    def role(self, uid, is_out=False) -> str | None:
        if is_out or (uid is not None and int(uid) in self.owner_ids):
            return "owner"
        if uid is not None and int(uid) in self.admins:
            return "admin"
        return None


HELP = """🤖 <b>راهنمای سلف‌بات</b> (پیشوند «{p}»)

⏱ <b>زمان‌بندی</b>
<code>{p}every 5m for 5h : میو</code>
↳ هر ۵ دقیقه تا ۵ ساعت، در همین چت
<code>{p}every 5m for 5h to @group : میو || سلام</code>
↳ مقصد دلخواه + چند متن (با || جدا کن)
گزینه‌ها: <code>x20</code> تعداد · <code>jitter 10</code> تأخیر تصادفی ٪ · <code>random</code> · <code>after 10m</code> شروع با تأخیر · <code>name x</code>
<code>{p}click every=5m for=5h bot=@MeowBot btn="میو"</code>
↳ کلیک خودکار روی دکمه‌ی ربات بازی
<code>{p}jobs</code> · <code>{p}info 3</code> · <code>{p}pause 3|all</code> · <code>{p}resume 3|all</code> · <code>{p}rm 3|all</code> · <code>{p}run 3</code>

🎮 <b>ابزار</b>
<code>{p}send @chat : متن</code> · <code>{p}bot @Bot : متن</code> (جواب ربات رو می‌بینی)
<code>{p}tap @Bot متن دکمه</code> · <code>{p}id</code> · <code>{p}ping</code> · <code>{p}stats</code> · <code>{p}log</code>
<code>{p}join لینک</code> · <code>{p}leave</code> · <code>{p}purge 10</code> (فقط مالک)

🗣 <b>پاسخ خودکار</b>
<code>{p}trigger add * کلمه : پاسخ</code> · <code>{p}trigger list</code> · <code>{p}trigger rm 2</code>

👑 <b>مالک</b>: <code>{p}admin add 123</code> (یا ریپلای) · <code>{p}admin rm 123</code> · <code>{p}admin list</code>"""


class Commands:
    OWNER_ONLY = {"admin", "join", "leave", "purge"}

    def __init__(self, engine: Engine, store, sender, auth: Auth, cfg: Config, prefix=".", clock=time.time):
        self.engine, self.store, self.sender, self.auth, self.cfg = engine, store, sender, auth, cfg
        self.prefix, self.clock = prefix, clock
        self.triggers: list[dict] = []
        self._trig_last: dict[int, float] = {}

    async def load(self):
        self.triggers = [t for t in await self.store.list_triggers() if t.get("active", True)]

    # ── dispatch ──
    async def handle(self, ctx: Ctx) -> bool:
        role = self.auth.role(ctx.sender_id, ctx.is_out)
        if not role or not ctx.text.startswith(self.prefix):
            return False
        name, _, arg = ctx.text[len(self.prefix):].partition(" ")
        name, arg = name.lower().strip(), arg.strip()
        fn = getattr(self, f"c_{name}", None)
        if not fn or not name.isalpha():
            return False
        if name in self.OWNER_ONLY and role != "owner":
            await ctx.reply("⛔ این دستور فقط برای مالکه.")
            return True
        try:
            await fn(ctx, arg, role)
        except ParseError as ex:
            await ctx.reply(f"⚠️ {ex}")
        except SendError as ex:
            await ctx.reply(f"⚠️ {ex}")
        except Exception as ex:
            await ctx.reply(f"❌ خطا: {type(ex).__name__}: {str(ex)[:200]}")
        return True

    # ── scheduling ──
    async def _create(self, ctx: Ctx, spec: dict):
        default = spec.get("extra", {}).get("bot") if spec["kind"] == "click" else str(ctx.chat_id)
        title = ctx.chat_title if not spec.get("chat") and spec["kind"] == "text" else ""
        job = build_job(spec, default, self.clock(), ctx.sender_id, title)
        job = await self.engine.add(job)
        await ctx.reply("✅ کار ساخته شد\n\n" + describe(job, self.clock()))

    async def c_every(self, ctx, arg, role):
        await self._create(ctx, parse_every(arg, self.cfg))

    async def c_click(self, ctx, arg, role):
        await self._create(ctx, parse_click(arg, self.cfg))

    def _ids(self, arg: str) -> list[int]:
        if arg.lower() in ("all", "همه"):
            return list(self.engine.jobs)
        if not arg.strip().isdigit():
            raise ParseError("شماره کار رو بنویس (یا all). مثال: .pause 3")
        jid = int(arg)
        if jid not in self.engine.jobs:
            raise ParseError(f"کار #{jid} وجود نداره.")
        return [jid]

    async def c_jobs(self, ctx, arg, role):
        if not self.engine.jobs:
            await ctx.reply("هیچ کاری ثبت نشده. با <code>.help</code> راهنما رو ببین.")
            return
        now = self.clock()
        icons = {"active": "🟢", "paused": "⏸", "done": "✅", "error": "🔴"}
        lines = [f"{icons.get(j.status, '•')} #{fa(j.id)} · {j.title()[:20]} · هر {fmt_duration(j.interval_s)} · {fa(j.sent)} ارسال"
                 + (f" · بعدی {fmt_clock(j.next_run)}" if j.status == 'active' else "") for j in self.engine.jobs.values()]
        await ctx.reply("📋 <b>کارها</b>\n" + "\n".join(lines))

    async def c_info(self, ctx, arg, role):
        for jid in self._ids(arg)[:1]:
            await ctx.reply(describe(self.engine.jobs[jid], self.clock()))

    async def c_pause(self, ctx, arg, role):
        ids = self._ids(arg)
        for i in ids:
            await self.engine.set_status(i, "paused")
        await ctx.reply(f"⏸ {fa(len(ids))} کار متوقف شد.")

    async def c_resume(self, ctx, arg, role):
        ids = self._ids(arg)
        for i in ids:
            await self.engine.set_status(i, "active")
        await ctx.reply(f"▶️ {fa(len(ids))} کار فعال شد.")

    async def c_rm(self, ctx, arg, role):
        ids = self._ids(arg)
        for i in ids:
            await self.engine.remove(i)
        await ctx.reply(f"🗑 {fa(len(ids))} کار حذف شد.")

    async def c_run(self, ctx, arg, role):
        (jid,) = self._ids(arg)[:1] or [0]
        ok = await self.engine.run_now(jid)
        await ctx.reply("⚡ اجرا شد." if ok else "الان در حال اجراست یا پیدا نشد.")

    # ── tools ──
    @staticmethod
    def _split_colon(arg: str):
        safe = arg.replace("://", "§§")
        head, sep, body = safe.partition(":")
        return head.replace("§§", "://").strip(), body.replace("§§", "://").strip(), bool(sep)

    async def c_send(self, ctx, arg, role):
        head, body, ok = self._split_colon(arg)
        if not ok or not body or not head:
            raise ParseError("مثال: .send @group : سلام")
        await self.sender.send_text(normalize_chat(head), body)
        await ctx.reply("✅ ارسال شد.")

    async def c_bot(self, ctx, arg, role):
        head, body, ok = self._split_colon(arg)
        if not ok or not body or not head:
            raise ParseError("مثال: .bot @MeowBot : /start")
        text, buttons = await self.sender.ask_bot(normalize_chat(head), body)
        msg = "🤖 <b>جواب ربات:</b>\n" + (text[:1500] or "—")
        if buttons:
            msg += "\n\n🔘 دکمه‌ها: " + " · ".join(buttons[:20])
        await ctx.reply(msg)

    async def c_tap(self, ctx, arg, role):
        parts = arg.split(None, 1)
        if not parts:
            raise ParseError("مثال: .tap @MeowBot میو")
        await self.sender.tap(normalize_chat(parts[0]), normalize_chat(parts[0]), parts[1] if len(parts) > 1 else "")
        await ctx.reply("✅ کلیک شد.")

    async def c_join(self, ctx, arg, role):
        if not arg:
            raise ParseError("مثال: .join https://t.me/+abc123")
        await self.sender.join(normalize_chat(arg))
        await ctx.reply("✅ عضو شد.")

    async def c_leave(self, ctx, arg, role):
        await self.sender.leave(normalize_chat(arg) if arg else str(ctx.chat_id))
        if arg:
            await ctx.reply("✅ خارج شد.")

    async def c_purge(self, ctx, arg, role):
        n = int(arg) if arg.isdigit() else 0
        if not 1 <= n <= 100:
            raise ParseError("مثال: .purge 10 (بین ۱ تا ۱۰۰؛ فقط پیام‌های خودت پاک می‌شن)")
        await self.sender.purge(ctx.chat_id, n)

    async def c_id(self, ctx, arg, role):
        u = await ctx.replied_user()
        lines = [f"💬 چت: <code>{ctx.chat_id}</code>", f"👤 تو: <code>{ctx.sender_id}</code>"]
        if u:
            lines.append(f"↩️ کاربر ریپلای‌شده: <code>{u}</code>")
        await ctx.reply("\n".join(lines))

    async def c_ping(self, ctx, arg, role):
        t0 = time.perf_counter()
        await ctx.reply("🏓 ...")
        await ctx.reply(f"🏓 پونگ! {fa(round((time.perf_counter() - t0) * 1000))} میلی‌ثانیه · آنلاین {fmt_duration(self.clock() - self.engine.started_at)}")

    async def c_stats(self, ctx, arg, role):
        js = list(self.engine.jobs.values())
        by = lambda s: sum(1 for j in js if j.status == s)
        await ctx.reply("📊 <b>آمار</b>\n"
                        f"🟢 فعال: {fa(by('active'))} · ⏸ متوقف: {fa(by('paused'))} · ✅ تمام: {fa(by('done'))} · 🔴 خطا: {fa(by('error'))}\n"
                        f"📨 کل ارسال از آخرین روشن‌شدن: {fa(self.engine.total_sent)}\n"
                        f"⏳ آنلاین: {fmt_duration(self.clock() - self.engine.started_at)}\n"
                        f"👥 ادمین‌ها: {fa(len(self.auth.admins))}")

    async def c_log(self, ctx, arg, role):
        ev = list(self.engine.events)[-15:]
        await ctx.reply("🧾 <b>آخرین رویدادها</b>\n" + ("\n".join(ev) if ev else "—"))

    async def c_help(self, ctx, arg, role):
        await ctx.reply(HELP.format(p=self.prefix))

    c_start = c_help

    # ── admins ──
    async def c_admin(self, ctx, arg, role):
        parts = arg.split()
        sub = parts[0].lower() if parts else "list"
        if sub == "list":
            lines = [f"• <code>{u}</code> {n}" for u, n in self.auth.admins.items()]
            await ctx.reply("👥 <b>ادمین‌ها</b>\n" + ("\n".join(lines) if lines else "هنوز ادمینی نیست."))
            return
        uid = None
        if len(parts) > 1 and re.fullmatch(r"\d{5,}", parts[1]):
            uid = int(parts[1])
        else:
            uid = await ctx.replied_user()
        if not uid or sub not in ("add", "rm"):
            raise ParseError("مثال: .admin add 123456789  (یا روی پیام کاربر ریپلای کن)")
        if uid in self.auth.owner_ids:
            raise ParseError("مالک همیشه دسترسی کامل داره.")
        if sub == "add":
            await self.store.add_admin(uid)
            self.auth.admins[uid] = ""
            await ctx.reply(f"✅ <code>{uid}</code> ادمین شد.")
        else:
            await self.store.rm_admin(uid)
            self.auth.admins.pop(uid, None)
            await ctx.reply(f"🗑 <code>{uid}</code> از ادمینی برداشته شد.")

    # ── auto-replies ──
    async def c_trigger(self, ctx, arg, role):
        m = re.search(r"\s:\s|\s:$", arg)   # " : " so regex patterns like re:^hi can contain colons
        if m:
            head, body, has = arg[:m.start()].strip(), arg[m.end():].strip(), True
        else:
            head, body, has = self._split_colon(arg)
        toks = head.split()
        sub = toks[0].lower() if toks else "list"
        if sub == "list":
            lines = [f"#{t['id']} · {t['chat_ref']} · «{t['pattern']}» ← {t['response'][:30]}" for t in self.triggers]
            await ctx.reply("🗣 <b>پاسخ‌های خودکار</b>\n" + ("\n".join(lines) if lines else "خالی."))
        elif sub == "add":
            if len(toks) < 3 or not has or not body:
                raise ParseError("مثال: .trigger add * سلام : سلام عزیز  (به‌جای * می‌تونی @group بذاری)")
            ref = "*" if toks[1] == "*" else normalize_chat(toks[1])
            t = {"chat_ref": ref, "pattern": " ".join(toks[2:]), "response": body, "active": True}
            t["id"] = await self.store.add_trigger(t)
            self.triggers.append(t)
            await ctx.reply(f"✅ پاسخ خودکار #{t['id']} ساخته شد.")
        elif sub == "rm" and len(toks) > 1 and toks[1].isdigit():
            ok = await self.store.rm_trigger(int(toks[1]))
            self.triggers = [t for t in self.triggers if t["id"] != int(toks[1])]
            await ctx.reply("🗑 حذف شد." if ok else "پیدا نشد.")
        else:
            raise ParseError("از list / add / rm استفاده کن.")

    def match_triggers(self, chat_id: int, username: str, text: str) -> list[str]:
        out, now = [], self.clock()
        for t in self.triggers:
            ref = t["chat_ref"]
            if ref != "*" and ref != str(chat_id) and ref.lower() != f"@{(username or '').lower()}":
                continue
            pat = t["pattern"]
            hit = bool(re.search(pat[3:], text, re.I)) if pat.startswith("re:") else pat.lower() in text.lower()
            if hit and now - self._trig_last.get(t["id"], 0) >= 30:   # cooldown: no loops, no floods
                self._trig_last[t["id"]] = now
                out.append(t["response"])
        return out
