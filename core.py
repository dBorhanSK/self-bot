# -*- coding: utf-8 -*-
"""Pure logic (no Telegram, no DB): durations, job model, command grammar, formatting."""
from __future__ import annotations

import random
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

IR_TZ = timezone(timedelta(hours=3, minutes=30))  # Iran has no DST since 2022
_DIGITS_IN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_DIGITS_OUT = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
UNITS = {"s": 1, "sec": 1, "ثانیه": 1, "ث": 1, "m": 60, "min": 60, "دقیقه": 60, "د": 60,
         "h": 3600, "hr": 3600, "ساعت": 3600, "d": 86400, "روز": 86400}


class ParseError(ValueError):
    pass


def fa(text) -> str:
    return str(text).translate(_DIGITS_OUT)


def norm(text: str) -> str:
    return str(text).translate(_DIGITS_IN)


def parse_duration(text: str) -> int | None:
    t = norm(text).strip().lower()
    if not re.fullmatch(r"(?:\s*\d+(?:\.\d+)?\s*[a-z\u0600-\u06ff]+)+\s*", t):
        return None
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z\u0600-\u06ff]+)", t):
        if unit not in UNITS:
            return None
        total += float(num) * UNITS[unit]
    return int(total) if total > 0 else None


def fmt_duration(sec: float) -> str:
    sec = int(max(0, sec))
    parts = []
    for name, size in (("روز", 86400), ("ساعت", 3600), ("دقیقه", 60), ("ثانیه", 1)):
        if sec >= size:
            parts.append(f"{sec // size} {name}")
            sec %= size
    return fa(" و ".join(parts[:2]) if parts else "۰ ثانیه")


def fmt_clock(ts: float | None) -> str:
    if not ts:
        return "—"
    return fa(datetime.fromtimestamp(ts, IR_TZ).strftime("%H:%M:%S"))


def normalize_chat(ref: str) -> str:
    ref = ref.strip()
    m = re.match(r"^(?:https?://)?(?:t|telegram)\.me/(.+)$", ref)
    if m:
        ref = m.group(1).strip("/")
        if ref.startswith("+") or ref.startswith("joinchat/"):
            return "https://t.me/" + ref
        ref = "@" + ref.split("/")[0].lstrip("@")
    if re.fullmatch(r"-?\d+", norm(ref)):
        return norm(ref)
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", ref):
        ref = "@" + ref
    return ref


@dataclass
class Config:
    min_interval: int = 20      # refuse jobs faster than this (account safety)
    max_jobs: int = 40
    margin_s: float = 2.0       # added to every interval so cooldown-based bots never see us "early"
    global_gap_s: float = 1.2   # minimum gap between any two sends of the account


@dataclass
class Job:
    kind: str                   # "text" | "click"
    chat_ref: str
    interval_s: int
    texts: list = field(default_factory=list)
    id: int | None = None
    name: str = ""
    order: str = "seq"          # seq | random
    jitter_pct: int = 0         # extra random delay only (never early)
    end_at: float | None = None
    max_count: int | None = None
    sent: int = 0
    idx: int = 0
    status: str = "active"      # active | paused | done | error
    next_run: float = 0.0
    last_error: str = ""
    fails: int = 0
    extra: dict = field(default_factory=dict)   # click: {"bot":..., "btn":...}; chat_title
    created_by: int = 0
    created_at: float = 0.0

    def pick_text(self) -> str:
        if not self.texts:
            return ""
        if self.order == "random":
            return random.choice(self.texts)
        return self.texts[self.idx % len(self.texts)]

    def delay(self, margin: float) -> float:
        d = self.interval_s + margin
        if self.jitter_pct:
            d += random.uniform(0, self.interval_s * self.jitter_pct / 100.0)
        return d

    def schedule_next(self, now: float, margin: float):
        """Keep the cadence (no drift) unless we've fallen behind, then restart from now."""
        base = self.next_run if self.next_run and now - self.next_run < self.interval_s else now
        self.next_run = max(base + self.delay(margin), now + 1)

    def finished_after(self) -> bool:
        if self.max_count is not None and self.sent >= self.max_count:
            return True
        return self.end_at is not None and self.next_run > self.end_at

    def title(self) -> str:
        return self.name or (self.extra.get("chat_title") or self.chat_ref)


STATUS_ICON = {"active": "🟢", "paused": "⏸", "done": "✅", "error": "🔴"}


def describe(job: Job, now: float) -> str:
    what = (f"🎮 کلیک روی دکمه «{job.extra.get('btn') or 'اولین دکمه'}» ربات {job.extra.get('bot', '?')}"
            if job.kind == "click" else "💬 " + " || ".join(t[:40] for t in job.texts[:3]))
    lines = [f"{STATUS_ICON.get(job.status, '•')} کار #{fa(job.id)} · {job.title()}",
             f"📍 مقصد: {job.chat_ref}", what,
             f"⏱ هر {fmt_duration(job.interval_s)}" + (f" (+تا {fa(job.jitter_pct)}٪ تأخیر تصادفی)" if job.jitter_pct else ""),
             f"📨 ارسال‌شده: {fa(job.sent)}" + (f" از {fa(job.max_count)}" if job.max_count else "")]
    if job.end_at:
        lines.append(f"🏁 پایان: {fmt_clock(job.end_at)}" + (f" (مانده {fmt_duration(job.end_at - now)})" if job.end_at > now else ""))
    if job.status == "active":
        lines.append(f"⏭ اجرای بعدی: {fmt_clock(job.next_run)} (تا {fmt_duration(job.next_run - now)} دیگر)")
    if job.last_error:
        lines.append(f"⚠️ آخرین خطا: {job.last_error}")
    return "\n".join(lines)


# ── command grammar ─────────────────────────────────────────────────────────
def _need(toks, i, what):
    if i + 1 >= len(toks):
        raise ParseError(f"بعد از «{toks[i]}» باید {what} بنویسی.")
    return toks[i + 1]


def _dur(text, what, cfg: Config | None = None, minimum: int = 1):
    d = parse_duration(text)
    if d is None:
        raise ParseError(f"{what} نامعتبره: «{text}». مثال: 30s ، 5m ، 2h ، 1d ، ۵دقیقه")
    if d < minimum:
        raise ParseError(f"{what} نباید کمتر از {fmt_duration(minimum)} باشه.")
    return d


def parse_every(arg: str, cfg: Config) -> dict:
    """.every 5m for 5h to @group x20 jitter 10 random name foo : text1 || text2"""
    safe = arg.replace("://", "§§")
    head, sep, body = safe.partition(":")
    head, body = head.replace("§§", "://"), body.replace("§§", "://")
    texts = [t.strip() for t in body.split("||") if t.strip()]
    if not sep or not texts:
        raise ParseError("متن پیام رو بعد از « : » بنویس.\nمثال: .every 5m for 5h : میو")
    toks = head.split()
    if not toks:
        raise ParseError("فاصله زمانی رو بنویس. مثال: .every 5m for 5h : میو")
    out = {"kind": "text", "texts": texts, "interval_s": _dur(toks[0], "فاصله", minimum=cfg.min_interval),
           "chat": None, "duration": None, "count": None, "jitter": 0, "order": "seq", "name": "", "after": 0}
    i = 1
    while i < len(toks):
        t = toks[i].lower()
        if t in ("for", "برای"):
            out["duration"] = _dur(_need(toks, i, "مدت"), "مدت"); i += 2
        elif t in ("to", "به", "در"):
            out["chat"] = normalize_chat(_need(toks, i, "گروه")); i += 2
        elif t in ("after", "بعد"):
            out["after"] = _dur(_need(toks, i, "تأخیر شروع"), "تأخیر شروع"); i += 2
        elif t in ("jitter", "لرزش"):
            v = norm(_need(toks, i, "درصد")).rstrip("%٪")
            if not v.isdigit() or not 0 <= int(v) <= 100:
                raise ParseError("jitter باید عددی بین ۰ تا ۱۰۰ باشه.")
            out["jitter"] = int(v); i += 2
        elif t in ("x", "count", "تعداد") or re.fullmatch(r"x\d+", t):
            v = norm(t[1:] if re.fullmatch(r"x\d+", t) else _need(toks, i, "تعداد"))
            if not v.isdigit() or int(v) < 1:
                raise ParseError("تعداد باید عدد مثبت باشه.")
            out["count"] = int(v); i += 1 if re.fullmatch(r"x\d+", t) else 2
        elif t in ("random", "تصادفی"):
            out["order"] = "random"; i += 1
        elif t in ("name", "نام"):
            out["name"] = _need(toks, i, "نام")[:40]; i += 2
        else:
            raise ParseError(f"عبارت نامعتبر: «{toks[i]}»")
    return out


def parse_click(arg: str, cfg: Config) -> dict:
    """.click every=5m for=5h to=@group bot=@MeowBot btn="میو" x=10 jitter=5"""
    try:
        toks = shlex.split(arg)
    except ValueError:
        raise ParseError("گیومه‌ها بسته نشدن.")
    kv = {}
    for t in toks:
        if "=" not in t:
            raise ParseError(f"از قالب کلید=مقدار استفاده کن، «{t}» درست نیست.")
        k, v = t.split("=", 1)
        kv[k.lower()] = v
    if "every" not in kv or "bot" not in kv:
        raise ParseError('حداقل every و bot لازمه.\nمثال: .click every=5m for=5h bot=@MeowBot btn="میو"')
    bot = normalize_chat(kv["bot"])
    out = {"kind": "click", "texts": [], "interval_s": _dur(kv["every"], "فاصله", minimum=cfg.min_interval),
           "chat": normalize_chat(kv["to"]) if kv.get("to") else None, "duration": None, "count": None,
           "jitter": 0, "order": "seq", "name": kv.get("name", "")[:40], "after": 0,
           "extra": {"bot": bot, "btn": kv.get("btn", "")}}
    if "for" in kv:
        out["duration"] = _dur(kv["for"], "مدت")
    if "after" in kv:
        out["after"] = _dur(kv["after"], "تأخیر شروع")
    if "x" in kv:
        if not norm(kv["x"]).isdigit() or int(norm(kv["x"])) < 1:
            raise ParseError("x باید عدد مثبت باشه.")
        out["count"] = int(norm(kv["x"]))
    if "jitter" in kv:
        j = norm(kv["jitter"]).rstrip("%")
        if not j.isdigit() or int(j) > 100:
            raise ParseError("jitter باید بین ۰ تا ۱۰۰ باشه.")
        out["jitter"] = int(j)
    return out


def build_job(spec: dict, default_chat: str, now: float, created_by: int, chat_title: str = "") -> Job:
    chat = spec.get("chat") or default_chat
    extra = dict(spec.get("extra") or {})
    if chat_title:
        extra["chat_title"] = chat_title
    return Job(kind=spec["kind"], chat_ref=chat, interval_s=spec["interval_s"], texts=spec.get("texts") or [],
               name=spec.get("name", ""), order=spec.get("order", "seq"), jitter_pct=spec.get("jitter", 0),
               end_at=(now + spec["duration"]) if spec.get("duration") else None, max_count=spec.get("count"),
               next_run=now + spec.get("after", 0), extra=extra, created_by=created_by, created_at=now)
