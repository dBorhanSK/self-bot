# -*- coding: utf-8 -*-
"""Button panel (a regular Bot API bot, run through Telethon) to control the self-bot."""
from __future__ import annotations

import logging
import time

from commands import Auth
from core import (Config, Job, build_job, describe, fa, fmt_clock, fmt_duration, normalize_chat, parse_duration)

log = logging.getLogger("selfbot.panel")
IVS = [(60, "۱ دقیقه"), (300, "۵ دقیقه"), (600, "۱۰ دقیقه"), (1800, "۳۰ دقیقه"), (3600, "۱ ساعت")]
DUS = [(3600, "۱ ساعت"), (10800, "۳ ساعت"), (18000, "۵ ساعت"), (43200, "۱۲ ساعت"), (86400, "۲۴ ساعت"), (0, "♾ بدون پایان")]


def is_cb(event) -> bool:
    return "CallbackQuery" in type(event).__qualname__


class Panel:
    def __init__(self, bot, engine, store, sender, auth: Auth, cfg: Config, clock=time.time):
        self.bot, self.engine, self.store, self.sender, self.auth, self.cfg, self.clock = bot, engine, store, sender, auth, cfg, clock
        self.W: dict[int, dict] = {}
        self.B = None

    def register(self):
        from telethon import Button, events
        self.B = Button

        @self.bot.on(events.NewMessage(incoming=True))
        async def on_msg(event):
            try:
                await self.on_text(event)
            except Exception:
                log.exception("panel text failed")

        @self.bot.on(events.CallbackQuery())
        async def on_cb(event):
            try:
                await self.on_callback(event)
            except Exception:
                log.exception("panel callback failed")
                try:
                    await event.answer("⚠️ خطا؛ دوباره تلاش کن", alert=True)
                except Exception:
                    pass

    # ── helpers ──
    def b(self, text, data):
        return self.B.inline(text, data=data.encode())

    def is_owner(self, uid): return self.auth.role(uid) == "owner"

    async def show(self, event, text, rows, edit=True):
        buttons = [[self.b(t, d) for t, d in row] for row in rows]
        if edit and is_cb(event):
            try:
                await event.edit(text, buttons=buttons, parse_mode="html")
                return
            except Exception as ex:
                if "not modified" in str(ex).lower():
                    return
        await event.respond(text, buttons=buttons, parse_mode="html")

    def menu_rows(self, uid):
        rows = [[("📋 کارها", "p:jobs"), ("➕ کار جدید", "p:new")],
                [("📊 وضعیت", "p:stats"), ("🧾 رویدادها", "p:log")]]
        if self.is_owner(uid):
            rows.append([("👥 ادمین‌ها", "p:admins")])
        return rows

    async def menu(self, event):
        js = list(self.engine.jobs.values())
        active = sum(1 for j in js if j.status == "active")
        await self.show(event, f"🤖 <b>پنل کنترل سلف‌بات</b>\n━━━━━━━━━━━━━━\n🟢 کار فعال: <b>{fa(active)}</b> از {fa(len(js))}\n"
                               f"📨 ارسال‌ها: <b>{fa(self.engine.total_sent)}</b>\n\nیکی رو انتخاب کن 👇", self.menu_rows(event.sender_id))

    # ── text ──
    async def on_text(self, event):
        uid, text = event.sender_id, (event.raw_text or "").strip()
        if not self.auth.role(uid):
            if text.startswith("/start"):
                await event.respond("⛔ این پنل خصوصیه.")
            return
        if text.startswith(("/start", "/panel", "/menu")):
            self.W.pop(uid, None)
            await self.menu(event)
            return
        w = self.W.get(uid)
        if not w:
            return
        step, d = w["step"], w["data"]
        if step == "chat_manual":
            d["chat"] = normalize_chat(text)
            await self.ask_text(event)
        elif step == "bot":
            d["extra"] = {"bot": normalize_chat(text), "btn": ""}
            d["chat"] = d["extra"]["bot"]
            w["step"] = "btn"
            await self.show(event, "🔘 متن دکمه‌ای که باید کلیک بشه رو بفرست (بخشی از متن کافیه).\nبرای «اولین دکمه»، بفرست: <code>-</code>", [[("✖️ لغو", "p:menu")]], edit=False)
        elif step == "btn":
            d["extra"]["btn"] = "" if text == "-" else text
            await self.ask_interval(event)
        elif step == "text":
            d["texts"] = [t.strip() for t in text.split("||") if t.strip()]
            await self.ask_interval(event)
        elif step in ("iv_custom", "du_custom"):
            sec = parse_duration(text)
            if not sec:
                await event.respond("⚠️ نامعتبره. مثال: 30s ، 5m ، 2h ، 1d")
                return
            if step == "iv_custom":
                if sec < self.cfg.min_interval:
                    await event.respond(f"⚠️ حداقل فاصله {fmt_duration(self.cfg.min_interval)} هست.")
                    return
                d["interval_s"] = sec
                await self.ask_duration(event)
            else:
                d["duration"] = sec
                await self.confirm(event)

    # ── wizard ──
    async def ask_text(self, event):
        self.W[event.sender_id]["step"] = "text"
        await self.show(event, "💬 متن پیام رو بفرست.\nچند متن؟ با <code>||</code> جدا کن (به‌ترتیب یا تصادفی می‌فرسته).", [[("✖️ لغو", "p:menu")]], edit=False)

    async def ask_interval(self, event):
        self.W[event.sender_id]["step"] = "interval"
        rows = [[(l, f"p:iv:{s}") for s, l in IVS[:3]], [(l, f"p:iv:{s}") for s, l in IVS[3:]] + [("✍️ دلخواه", "p:ivx")], [("✖️ لغو", "p:menu")]]
        await self.show(event, "⏱ هر چند وقت یک‌بار؟", rows, edit=is_cb(event))

    async def ask_duration(self, event):
        self.W[event.sender_id]["step"] = "duration"
        rows = [[(l, f"p:du:{s}") for s, l in DUS[:3]], [(l, f"p:du:{s}") for s, l in DUS[3:]], [("✍️ دلخواه", "p:dux")], [("✖️ لغو", "p:menu")]]
        await self.show(event, "🏁 تا چه مدت ادامه بده؟", rows, edit=is_cb(event))

    async def confirm(self, event):
        w = self.W[event.sender_id]
        d = w["data"]
        w["step"] = "confirm"
        job = self.draft(event.sender_id)
        await self.show(event, "👀 <b>پیش‌نمایش</b>\n━━━━━━━━━━━━━━\n" + describe(job, self.clock()) + "\n\nشروع کنم؟",
                        [[("✅ شروع", "p:ok"), ("✖️ لغو", "p:menu")]], edit=is_cb(event))

    def draft(self, uid) -> Job:
        d = self.W[uid]["data"]
        spec = {"kind": d["kind"], "texts": d.get("texts", []), "interval_s": d["interval_s"], "chat": d["chat"],
                "duration": d.get("duration") or None, "count": None, "jitter": 0, "order": "seq", "name": "",
                "after": 0, "extra": d.get("extra", {})}
        return build_job(spec, d["chat"], self.clock(), uid, d.get("chat_title", ""))

    async def groups_page(self, event, page):
        w = self.W[event.sender_id]
        if "groups" not in w:
            w["groups"] = await self.sender.recent_groups()
        gs, per = w["groups"], 8
        chunk = gs[page * per:(page + 1) * per]
        rows = [[(t[:34], f"p:c:{page * per + i}")] for i, (_, t) in enumerate(chunk)]
        nav = []
        if page > 0:
            nav.append(("⬅️", f"p:cg:{page - 1}"))
        if (page + 1) * per < len(gs):
            nav.append(("➡️", f"p:cg:{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([("✍️ وارد کردن دستی (آیدی/لینک)", "p:cm")])
        rows.append([("✖️ لغو", "p:menu")])
        await self.show(event, "📍 پیام تو کدوم گروه فرستاده بشه؟\n<i>گروه‌های اکانتت:</i>", rows)

    # ── callbacks ──
    async def on_callback(self, event):
        uid = event.sender_id
        data = (event.data or b"").decode()
        if not self.auth.role(uid):
            await event.answer("⛔ دسترسی نداری", alert=True)
            return
        await event.answer()
        p = data.split(":")
        act, arg = p[1], (p[2] if len(p) > 2 else "")
        if act == "menu":
            self.W.pop(uid, None)
            await self.menu(event)
        elif act == "jobs":
            js = list(self.engine.jobs.values())
            icons = {"active": "🟢", "paused": "⏸", "done": "✅", "error": "🔴"}
            rows = [[(f"{icons.get(j.status, '•')} #{j.id} · {j.title()[:22]} · {fa(j.sent)}", f"p:job:{j.id}")] for j in js[:30]]
            rows.append([("➕ کار جدید", "p:new"), ("🔙 منو", "p:menu")])
            await self.show(event, "📋 <b>کارها</b>" + ("" if js else "\nهنوز کاری نیست."), rows)
        elif act == "job":
            await self.job_view(event, int(arg))
        elif act in ("pause", "resume"):
            await self.engine.set_status(int(arg), "paused" if act == "pause" else "active")
            await self.job_view(event, int(arg))
        elif act == "run":
            ok = await self.engine.run_now(int(arg))
            await event.answer("⚡ اجرا شد" if ok else "الان در حال اجراست", alert=False)
        elif act == "rm":
            await self.show(event, f"🗑 کار #{arg} حذف بشه؟", [[("🗑 بله", f"p:rmy:{arg}"), ("↩️ نه", f"p:job:{arg}")]])
        elif act == "rmy":
            await self.engine.remove(int(arg))
            await self.show(event, "🗑 حذف شد.", [[("📋 کارها", "p:jobs"), ("🔙 منو", "p:menu")]])
        elif act == "stats":
            js = list(self.engine.jobs.values())
            c = lambda s: fa(sum(1 for j in js if j.status == s))
            await self.show(event, f"📊 <b>وضعیت</b>\n🟢 {c('active')} · ⏸ {c('paused')} · ✅ {c('done')} · 🔴 {c('error')}\n"
                                   f"📨 ارسال‌ها: {fa(self.engine.total_sent)}\n⏳ آنلاین: {fmt_duration(self.clock() - self.engine.started_at)}\n"
                                   f"👥 ادمین‌ها: {fa(len(self.auth.admins))}", [[("🔄 تازه‌سازی", "p:stats"), ("🔙 منو", "p:menu")]])
        elif act == "log":
            ev = list(self.engine.events)[-15:]
            await self.show(event, "🧾 <b>رویدادها</b>\n" + ("\n".join(ev) or "—"), [[("🔄 تازه‌سازی", "p:log"), ("🔙 منو", "p:menu")]])
        elif act == "admins":
            await self.admins_view(event, uid)
        elif act == "adr" and self.is_owner(uid):
            await self.store.rm_admin(int(arg))
            self.auth.admins.pop(int(arg), None)
            await self.admins_view(event, uid)
        elif act == "new":
            self.W[uid] = {"step": "kind", "data": {}}
            await self.show(event, "➕ <b>کار جدید</b>\nچه کاری انجام بشه؟",
                            [[("💬 ارسال پیام تکراری", "p:k:text")], [("🎮 کلیک روی دکمه ربات بازی", "p:k:click")], [("🔙 منو", "p:menu")]])
        elif act == "k":
            self.W[uid] = {"step": "chat", "data": {"kind": arg}}
            if arg == "click":
                self.W[uid]["step"] = "bot"
                await self.show(event, "🎮 آیدی ربات بازی رو بفرست (مثل <code>@MeowBot</code>).", [[("✖️ لغو", "p:menu")]])
            else:
                await self.groups_page(event, 0)
        elif act == "cg":
            await self.groups_page(event, int(arg))
        elif act == "c":
            w = self.W.get(uid)
            if w:
                cid, title = w["groups"][int(arg)]
                w["data"].update(chat=str(cid), chat_title=title)
                await self.ask_text(event)
        elif act == "cm":
            if uid in self.W:
                self.W[uid]["step"] = "chat_manual"
                await self.show(event, "✍️ آیدی عددی، <code>@username</code> یا لینک گروه رو بفرست.", [[("✖️ لغو", "p:menu")]])
        elif act == "iv" and uid in self.W:
            self.W[uid]["data"]["interval_s"] = max(int(arg), self.cfg.min_interval)
            await self.ask_duration(event)
        elif act == "ivx" and uid in self.W:
            self.W[uid]["step"] = "iv_custom"
            await self.show(event, "✍️ فاصله رو بنویس (مثل 90s ، 7m ، 2h):", [[("✖️ لغو", "p:menu")]])
        elif act == "du" and uid in self.W:
            self.W[uid]["data"]["duration"] = int(arg)
            await self.confirm(event)
        elif act == "dux" and uid in self.W:
            self.W[uid]["step"] = "du_custom"
            await self.show(event, "✍️ مدت رو بنویس (مثل 90m ، 5h ، 2d):", [[("✖️ لغو", "p:menu")]])
        elif act == "ok" and uid in self.W and self.W[uid]["step"] == "confirm":
            job = self.draft(uid)
            self.W.pop(uid, None)
            try:
                job = await self.engine.add(job)
            except ValueError as ex:
                await self.show(event, f"⚠️ {ex}", [[("🔙 منو", "p:menu")]])
                return
            await self.show(event, "✅ <b>شروع شد!</b>\n\n" + describe(job, self.clock()), [[("📋 کارها", "p:jobs"), ("🔙 منو", "p:menu")]])

    async def admins_view(self, event, uid):
        if not self.is_owner(uid):
            await event.answer("فقط مالک", alert=True)
            return
        lines = [f"• <code>{u}</code>" for u in self.auth.admins] or ["هنوز ادمینی نیست."]
        rows = [[(f"🗑 {u}", f"p:adr:{u}")] for u in self.auth.admins] + [[("🔙 منو", "p:menu")]]
        await self.show(event, "👥 <b>ادمین‌ها</b>\n" + "\n".join(lines) + "\n\nبرای افزودن، در چت اکانت بنویس:\n<code>.admin add آیدی</code>", rows)

    async def job_view(self, event, jid):
        j = self.engine.jobs.get(jid)
        if not j:
            await self.show(event, "این کار پیدا نشد.", [[("📋 کارها", "p:jobs")]])
            return
        toggle = ("⏸ توقف", f"p:pause:{jid}") if j.status == "active" else ("▶️ ادامه", f"p:resume:{jid}")
        await self.show(event, describe(j, self.clock()), [[toggle, ("⚡ اجرا الان", f"p:run:{jid}")],
                                                           [("🗑 حذف", f"p:rm:{jid}"), ("🔙 کارها", "p:jobs")]])
