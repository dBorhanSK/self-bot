# -*- coding: utf-8 -*-
"""Persistence: Supabase (production) or memory (fallback/tests). All methods are async."""
from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import asdict

from core import Job

log = logging.getLogger("selfbot.store")
JOB_COLS = ("kind", "chat_ref", "interval_s", "texts", "name", "jitter_pct", "end_at", "max_count", "sent", "idx",
            "status", "next_run", "last_error", "fails", "extra", "created_by", "created_at")


def job_to_row(j: Job) -> dict:
    d = asdict(j)
    row = {k: d[k] for k in JOB_COLS}
    row["order_mode"] = j.order
    return row


def row_to_job(r: dict) -> Job:
    kw = {k: r.get(k) for k in JOB_COLS if r.get(k) is not None}
    kw["texts"] = r.get("texts") or []
    kw["extra"] = r.get("extra") or {}
    return Job(id=r["id"], order=r.get("order_mode") or "seq", **kw)


class MemoryStore:
    def __init__(self):
        self.jobs, self.admins, self.triggers = {}, {}, {}
        self._ids = itertools.count(1)

    async def load_jobs(self): return [Job(**{**asdict(j)}) for j in self.jobs.values()]

    async def save_job(self, job: Job) -> Job:
        if job.id is None:
            job.id = next(self._ids)
        self.jobs[job.id] = Job(**asdict(job))
        return job

    async def delete_job(self, jid): self.jobs.pop(jid, None)
    async def list_admins(self): return dict(self.admins)
    async def add_admin(self, uid, name=""): self.admins[int(uid)] = name
    async def rm_admin(self, uid): self.admins.pop(int(uid), None)
    async def list_triggers(self): return [dict(t) for t in self.triggers.values()]

    async def add_trigger(self, t: dict) -> int:
        tid = next(self._ids)
        self.triggers[tid] = {**t, "id": tid}
        return tid

    async def rm_trigger(self, tid): return self.triggers.pop(int(tid), None) is not None


class SupabaseStore:
    def __init__(self, url: str, key: str):
        from supabase import create_client
        self.sb = create_client(url, key)

    async def _run(self, fn):
        return await asyncio.to_thread(lambda: fn().execute().data or [])

    async def load_jobs(self):
        return [row_to_job(r) for r in await self._run(lambda: self.sb.table("selfbot_jobs").select("*").limit(1000))]

    async def save_job(self, job: Job) -> Job:
        row = job_to_row(job)
        if job.id is None:
            rows = await self._run(lambda: self.sb.table("selfbot_jobs").insert(row))
            job.id = rows[0]["id"]
        else:
            await self._run(lambda: self.sb.table("selfbot_jobs").update(row).eq("id", job.id))
        return job

    async def delete_job(self, jid): await self._run(lambda: self.sb.table("selfbot_jobs").delete().eq("id", jid))

    async def list_admins(self):
        return {int(r["user_id"]): r.get("name") or "" for r in
                await self._run(lambda: self.sb.table("selfbot_admins").select("*"))}

    async def add_admin(self, uid, name=""):
        await self._run(lambda: self.sb.table("selfbot_admins").upsert({"user_id": int(uid), "name": name[:60]}, on_conflict="user_id"))

    async def rm_admin(self, uid): await self._run(lambda: self.sb.table("selfbot_admins").delete().eq("user_id", int(uid)))
    async def list_triggers(self): return await self._run(lambda: self.sb.table("selfbot_triggers").select("*"))

    async def add_trigger(self, t: dict) -> int:
        return (await self._run(lambda: self.sb.table("selfbot_triggers").insert(t)))[0]["id"]

    async def rm_trigger(self, tid):
        return bool(await self._run(lambda: self.sb.table("selfbot_triggers").delete().eq("id", int(tid))))
