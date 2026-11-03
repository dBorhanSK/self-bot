# -*- coding: utf-8 -*-
"""Scheduler engine. Talks to Telegram only through the `sender` object (see SendError)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from core import Config, Job, fmt_duration

log = logging.getLogger("selfbot.engine")


class SendError(Exception):
    """kind: flood (wait `seconds`) | fatal (stop the job) | temp (retry next interval)."""
    def __init__(self, kind: str, message: str = "", seconds: int = 0):
        super().__init__(message)
        self.kind, self.seconds = kind, seconds


class Engine:
    MAX_FAILS = 5

    def __init__(self, store, sender, notify, cfg: Config, clock=time.time):
        self.store, self.sender, self.notify, self.cfg, self.clock = store, sender, notify, cfg, clock
        self.jobs: dict[int, Job] = {}
        self.events = deque(maxlen=100)
        self.started_at = clock()
        self.total_sent = 0
        self._running: set[int] = set()
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._stop = False

    def log(self, msg: str):
        self.events.append(f"{time.strftime('%H:%M:%S')} {msg}")
        log.info(msg)

    async def load(self):
        now = self.clock()
        stagger = 0
        for j in await self.store.load_jobs():
            if j.status == "active" and j.next_run < now:      # resume without catching up/bursting
                j.next_run = now + 3 + stagger
                stagger += 2
            self.jobs[j.id] = j
        self.log(f"{len(self.jobs)} job(s) loaded")

    # ── management ──
    async def add(self, job: Job) -> Job:
        if sum(1 for j in self.jobs.values() if j.status in ("active", "paused")) >= self.cfg.max_jobs:
            raise ValueError(f"حداکثر {self.cfg.max_jobs} کار فعال مجازه.")
        await self.store.save_job(job)
        self.jobs[job.id] = job
        self.log(f"job #{job.id} created")
        return job

    async def remove(self, jid: int) -> bool:
        if jid not in self.jobs:
            return False
        self.jobs.pop(jid)
        await self.store.delete_job(jid)
        return True

    async def set_status(self, jid: int, status: str) -> bool:
        j = self.jobs.get(jid)
        if not j:
            return False
        j.status = status
        if status == "active":
            j.fails, j.last_error = 0, ""
            j.next_run = max(j.next_run, self.clock() + 1)
            if j.end_at and j.end_at <= self.clock():
                j.status = "done"
        await self.store.save_job(j)
        return True

    async def run_now(self, jid: int) -> bool:
        j = self.jobs.get(jid)
        if not j or jid in self._running:
            return False
        asyncio.create_task(self._run(j, manual=True))
        return True

    # ── loop ──
    async def tick(self):
        now = self.clock()
        for j in list(self.jobs.values()):
            if j.status == "active" and j.next_run <= now and j.id not in self._running:
                self._running.add(j.id)
                asyncio.create_task(self._run(j))

    async def loop(self):
        while not self._stop:
            await self.tick()
            await asyncio.sleep(1)

    def stop(self):
        self._stop = True

    async def _gap(self):
        wait = self._last_send + self.cfg.global_gap_s - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def _run(self, job: Job, manual: bool = False):
        self._running.add(job.id)
        try:
            now = self.clock()
            if not manual and job.end_at and now > job.end_at + job.interval_s:
                job.status = "done"
                await self.store.save_job(job)
                await self.notify(f"✅ کار #{job.id} ({job.title()}) تمام شد. ارسال‌ها: {job.sent}")
                return
            try:
                async with self._send_lock:
                    await self._gap()
                    try:
                        await self.sender.perform(job)
                    finally:
                        self._last_send = time.monotonic()
                job.sent += 1
                job.idx += 1
                job.fails, job.last_error = 0, ""
                self.total_sent += 1
                self.log(f"job #{job.id} sent ({job.sent})")
            except SendError as ex:
                if ex.kind == "flood":
                    job.last_error = f"FloodWait {ex.seconds}s"
                    job.next_run = self.clock() + ex.seconds + 2
                    self.log(f"job #{job.id} flood wait {ex.seconds}s")
                    await self.store.save_job(job)
                    if ex.seconds >= 120:
                        await self.notify(f"⚠️ کار #{job.id}: تلگرام {fmt_duration(ex.seconds)} محدودیت گذاشت؛ خودکار ادامه می‌دم.")
                    return
                job.last_error = str(ex)[:200]
                job.fails += 1
                self.log(f"job #{job.id} error ({ex.kind}): {ex}")
                if ex.kind == "fatal" or job.fails >= self.MAX_FAILS:
                    job.status = "error"
                    await self.store.save_job(job)
                    await self.notify(f"🔴 کار #{job.id} ({job.title()}) متوقف شد:\n{job.last_error}")
                    return
            except Exception as ex:  # never let one job kill the loop
                job.last_error = f"{type(ex).__name__}: {ex}"[:200]
                job.fails += 1
                log.exception("job %s crashed", job.id)
                if job.fails >= self.MAX_FAILS:
                    job.status = "error"
                    await self.store.save_job(job)
                    await self.notify(f"🔴 کار #{job.id} متوقف شد:\n{job.last_error}")
                    return
            if manual:
                await self.store.save_job(job)
                return
            job.schedule_next(self.clock(), self.cfg.margin_s)
            if job.finished_after():
                job.status = "done"
                await self.notify(f"✅ کار #{job.id} ({job.title()}) با موفقیت تمام شد.\n📨 ارسال‌ها: {job.sent}")
            await self.store.save_job(job)
        finally:
            self._running.discard(job.id)
