import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stubs; stubs.install()
from core import *
from store import MemoryStore
from engine import Engine, SendError
from commands import Auth, Commands, Ctx
import tg, panel

fails = 0
def check(name, cond, extra=""):
    global fails
    print(("✅" if cond else "❌"), name, ("" if cond else f"-> {extra}"))
    fails += 0 if cond else 1

CFG = Config(min_interval=20, max_jobs=3, margin_s=2, global_gap_s=0)

# ───────────── core ─────────────
check("duration 5m", parse_duration("5m") == 300)
check("duration 1h30m", parse_duration("1h30m") == 5400)
check("duration persian digits+words", parse_duration("۵ دقیقه") == 300 and parse_duration("۲ساعت") == 7200)
check("duration invalid", parse_duration("abc") is None and parse_duration("5x") is None and parse_duration("0m") is None)
s = parse_every("5m for 5h : میو", CFG)
check("every basic", s["interval_s"] == 300 and s["duration"] == 18000 and s["texts"] == ["میو"] and s["chat"] is None, s)
s = parse_every("10m for 2h to https://t.me/mygroup x20 jitter 10 random name meow : a || b || c", CFG)
check("every full options", (s["chat"], s["count"], s["jitter"], s["order"], s["name"], s["texts"]) == ("@mygroup", 20, 10, "random", "meow", ["a", "b", "c"]), s)
s = parse_every("5m to https://t.me/+AbC123 : hi", CFG)
check("invite link with :// survives colon split", s["chat"] == "https://t.me/+AbC123" and s["texts"] == ["hi"], s)
for bad, why in [("5m for 5h", "no text"), ("5s : x", "below min"), ("abc : x", "bad interval"), ("5m foo : x", "bad token"), ("5m for : x", "missing"), ("5m jitter 500 : x", "jitter range")]:
    try: parse_every(bad, CFG); check(f"every rejects ({why})", False)
    except ParseError: check(f"every rejects ({why})", True)
c = parse_click('every=5m for=5h bot=@MeowBot btn="میو پوینت" x=10', CFG)
check("click parse", c["extra"] == {"bot": "@MeowBot", "btn": "میو پوینت"} and c["count"] == 10 and c["interval_s"] == 300, c)
try: parse_click("every=5m", CFG); check("click needs bot", False)
except ParseError: check("click needs bot", True)

# schedule: never early, no drift
j = Job(kind="text", chat_ref="1", interval_s=300, texts=["a"], next_run=1000.0)
j.schedule_next(1000.5, 2); check("next = prev + interval + margin (no drift)", abs(j.next_run - 1302) < 1e-6, j.next_run)
j2 = Job(kind="text", chat_ref="1", interval_s=300, texts=["a"], next_run=1000.0, jitter_pct=20)
okj = True
for _ in range(200):
    j2.next_run = 1000.0; j2.schedule_next(1000.0, 2)
    okj &= 1302 <= j2.next_run <= 1362
check("jitter only adds delay, never early", okj)
j3 = Job(kind="text", chat_ref="1", interval_s=300, texts=["a"], next_run=1000.0)
j3.schedule_next(5000.0, 2); check("after long lag restarts from now (no burst)", abs(j3.next_run - 5302) < 1e-6)
jr = Job(kind="text", chat_ref="1", interval_s=60, texts=["a", "b", "c"], idx=4)
check("sequential rotation", jr.pick_text() == "b")

# ───────────── engine ─────────────
class FakeClock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t

class FakeSender:
    def __init__(self): self.sent, self.script, self.taps = [], [], []
    async def perform(self, job):
        if self.script:
            act = self.script.pop(0)
            if isinstance(act, Exception): raise act
        self.sent.append((job.chat_ref, job.pick_text()))
    async def send_text(self, ref, text): self.sent.append((ref, text))
    async def ask_bot(self, ref, text): return "ok reply", ["میو", "جایزه"]
    async def tap(self, c, b, btn): self.taps.append((c, b, btn))
    async def join(self, ref): self.joined = ref
    async def leave(self, ref): self.left = ref
    async def purge(self, chat, n): self.purged = (chat, n)

async def advance(eng, clk, seconds, step=1):
    end = clk.t + seconds
    while clk.t < end:
        clk.t += step
        await eng.tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

async def engine_tests():
    notes = []
    async def notify(t): notes.append(t)
    clk, sender, store = FakeClock(), FakeSender(), MemoryStore()
    eng = Engine(store, sender, notify, CFG, clock=clk)
    # the headline use case: every 5 minutes for 5 hours
    spec = parse_every("5m for 5h : میو", CFG)
    job = await eng.add(build_job(spec, "-1001", clk.t, 1))
    await advance(eng, clk, 5 * 3600 + 600)
    n = len(sender.sent)
    check("every 5m for 5h => ~60 messages then stops", 58 <= n <= 61, n)
    check("job finished & persisted as done", eng.jobs[job.id].status == "done" and store.jobs[job.id].status == "done")
    check("owner notified at the end", any("تمام شد" in x for x in notes))
    check("all sent to the right chat with the right text", all(c == "-1001" and t == "میو" for c, t in sender.sent))
    gaps_ok = True  # cadence ≥ interval (never early)
    await eng.remove(job.id)

    # count limit
    sender.sent.clear(); notes.clear()
    job = await eng.add(build_job(parse_every("30s x3 : hi", CFG), "5", clk.t, 1))
    await advance(eng, clk, 600)
    check("x3 stops after exactly 3 sends", len(sender.sent) == 3 and eng.jobs[job.id].status == "done", (len(sender.sent), eng.jobs[job.id].status))
    await eng.remove(job.id)

    # pause / resume
    sender.sent.clear()
    job = await eng.add(build_job(parse_every("30s : p", CFG), "5", clk.t, 1))
    await advance(eng, clk, 100); a = len(sender.sent)
    await eng.set_status(job.id, "paused"); await advance(eng, clk, 200)
    check("paused job sends nothing", len(sender.sent) == a, (a, len(sender.sent)))
    await eng.set_status(job.id, "active"); await advance(eng, clk, 100)
    check("resumed job sends again", len(sender.sent) > a)
    await eng.remove(job.id)

    # flood wait: not counted as failure, resumes by itself
    sender.sent.clear(); notes.clear()
    sender.script = [SendError("flood", "flood", 300)]
    job = await eng.add(build_job(parse_every("30s : f", CFG), "5", clk.t, 1))
    await advance(eng, clk, 200)
    check("flood: no sends during wait", len(sender.sent) == 0)
    check("flood: warned owner", any("محدودیت" in x for x in notes))
    await advance(eng, clk, 400)
    check("flood: continues afterwards, still active", len(sender.sent) > 0 and eng.jobs[job.id].status == "active" and eng.jobs[job.id].fails == 0)
    await eng.remove(job.id)

    # fatal error stops the job and tells owner
    notes.clear(); sender.script = [SendError("fatal", "اجازه ارسال پیام در این چت رو نداری")]
    job = await eng.add(build_job(parse_every("30s : x", CFG), "5", clk.t, 1))
    await advance(eng, clk, 10)
    check("fatal: job -> error + owner told", eng.jobs[job.id].status == "error" and any("متوقف" in x for x in notes))
    await eng.remove(job.id)

    # temp errors: retried, stops after MAX_FAILS
    notes.clear(); sender.script = [SendError("temp", "net")] * 5
    job = await eng.add(build_job(parse_every("20s : x", CFG), "5", clk.t, 1))
    await advance(eng, clk, 300)
    check("temp x5 => stopped", eng.jobs[job.id].status == "error")
    await eng.remove(job.id)
    sender.script = [SendError("temp", "net")] * 2
    job = await eng.add(build_job(parse_every("20s : x", CFG), "5", clk.t, 1))
    await advance(eng, clk, 200)
    check("temp x2 then success => recovers, fails reset", eng.jobs[job.id].status == "active" and eng.jobs[job.id].fails == 0)
    await eng.remove(job.id)

    # unexpected exception can't kill the loop
    sender.script = [RuntimeError("boom")]; sender.sent.clear()
    job = await eng.add(build_job(parse_every("20s : x", CFG), "5", clk.t, 1))
    await advance(eng, clk, 120)
    check("unexpected exception tolerated", len(sender.sent) > 0 and eng.jobs[job.id].status == "active")
    await eng.remove(job.id)

    # max jobs
    for i in range(3): await eng.add(build_job(parse_every("1h : x", CFG), "5", clk.t, 1))
    try: await eng.add(build_job(parse_every("1h : x", CFG), "5", clk.t, 1)); check("max_jobs enforced", False)
    except ValueError: check("max_jobs enforced", True)

    # restart: resumes without bursting missed runs
    clk2, sender2 = FakeClock(), FakeSender()
    st = MemoryStore()
    e1 = Engine(st, sender2, notify, CFG, clock=clk2)
    jb = await e1.add(build_job(parse_every("1m : r", CFG), "9", clk2.t, 1)); jb.sent = 7; await st.save_job(jb)
    clk2.t += 3 * 3600  # 3 hours offline
    e2 = Engine(st, sender2, notify, CFG, clock=clk2); await e2.load()
    await e2.tick(); await asyncio.sleep(0)
    check("restart: persisted state restored", e2.jobs[jb.id].sent == 7)
    await advance(e2, clk2, 10)
    check("restart: exactly one catch-up send, not 180", len(sender2.sent) == 1, len(sender2.sent))

asyncio.run(engine_tests())

# ───────────── commands ─────────────
async def cmd_tests():
    out = []
    async def notify(t): pass
    clk, sender, store = FakeClock(), FakeSender(), MemoryStore()
    eng = Engine(store, sender, notify, Config(min_interval=20, max_jobs=40, global_gap_s=0), clock=clk)
    auth = Auth({1}, {})
    cmds = Commands(eng, store, sender, auth, Config(min_interval=20, global_gap_s=0), ".", clock=clk)
    def ctx(text, uid=1, out_=True, chat=-100777, replied=None):
        async def reply(t): out.append(t)
        async def ru(): return replied
        return Ctx(text, uid, chat, out_, reply, ru, "My Group")
    check("owner outgoing command handled", await cmds.handle(ctx(".every 5m for 5h : میو")) and len(eng.jobs) == 1)
    j = list(eng.jobs.values())[0]
    check("default target = current chat, title kept", j.chat_ref == "-100777" and j.extra.get("chat_title") == "My Group")
    check("stranger ignored (no reply)", await cmds.handle(ctx(".every 5m : x", uid=999, out_=False)) is False and len(eng.jobs) == 1)
    check("non-command ignored", await cmds.handle(ctx("hello")) is False)
    out.clear(); await cmds.handle(ctx(".every 5s : x")); check("too-fast interval rejected with message", "کمتر" in out[-1] and len(eng.jobs) == 1, out)
    # admin flow
    await cmds.handle(ctx(".admin add 5550001"))
    check("owner adds admin (persisted)", 5550001 in auth.admins and 5550001 in await store.list_admins())
    check("admin can create jobs", await cmds.handle(ctx(".every 1m : hi", uid=5550001, out_=False)) and len(eng.jobs) == 2)
    out.clear(); await cmds.handle(ctx(".admin add 7770001", uid=5550001, out_=False)); check("admin cannot add admins", "مالک" in out[-1] and 7770001 not in auth.admins)
    out.clear(); await cmds.handle(ctx(".purge 5", uid=5550001, out_=False)); check("admin cannot purge", "مالک" in out[-1] and not hasattr(sender, "purged"))
    await cmds.handle(ctx(".admin add", replied=8880001)); check("admin add via reply", 8880001 in auth.admins)
    await cmds.handle(ctx(".admin rm 5550001")); check("admin removed", 5550001 not in auth.admins)
    check("removed admin is ignored", await cmds.handle(ctx(".jobs", uid=5550001, out_=False)) is False)
    # jobs mgmt
    out.clear(); await cmds.handle(ctx(".jobs")); check("jobs list", "#" in out[-1] or "۱" in out[-1])
    await cmds.handle(ctx(".pause all")); check("pause all", all(x.status == "paused" for x in eng.jobs.values()))
    await cmds.handle(ctx(".resume 1")); check("resume one", eng.jobs[1].status == "active" and eng.jobs[2].status == "paused")
    out.clear(); await cmds.handle(ctx(".info 99")); check("unknown job id handled", "وجود نداره" in out[-1])
    await cmds.handle(ctx(".rm all")); check("rm all", not eng.jobs and not store.jobs)
    # click + tools
    await cmds.handle(ctx('.click every=5m for=5h bot=@MeowBot btn="میو"'))
    jc = list(eng.jobs.values())[0]
    check("click job targets bot DM", jc.kind == "click" and jc.chat_ref == "@MeowBot" and jc.extra["btn"] == "میو", (jc.chat_ref, jc.extra))
    out.clear(); await cmds.handle(ctx(".bot @MeowBot : /start")); check(".bot shows reply + buttons", "ok reply" in out[-1] and "جایزه" in out[-1])
    await cmds.handle(ctx(".send @grp : سلام")); check(".send works", sender.sent[-1] == ("@grp", "سلام"))
    await cmds.handle(ctx(".tap @MeowBot میو پوینت")); check(".tap works", sender.taps[-1] == ("@MeowBot", "@MeowBot", "میو پوینت"))
    out.clear(); await cmds.handle(ctx(".help")); check("help renders (format ok)", "every" in out[-1] and "{p}" not in out[-1])
    out.clear(); await cmds.handle(ctx(".ping")); check("ping", "پونگ" in out[-1])
    # triggers
    await cmds.handle(ctx(".trigger add * سلام : سلام عزیز"))
    check("trigger matches", cmds.match_triggers(5, "", "به همه سلام") == ["سلام عزیز"])
    check("trigger cooldown (no loops)", cmds.match_triggers(5, "", "سلام") == [])
    clk.t += 31; check("trigger fires again after cooldown", cmds.match_triggers(5, "", "سلام") == ["سلام عزیز"])
    await cmds.handle(ctx(".trigger add @onlyhere re:^hi\\d+$ : x"))
    check("trigger scoped to chat + regex", cmds.match_triggers(5, "other", "hi12") == [] and cmds.match_triggers(5, "OnlyHere", "hi12") == ["x"])

asyncio.run(cmd_tests())

# ───────────── telethon glue ─────────────
async def glue_tests():
    e = tg.map_error(stubs.FloodWaitError(42)); check("map FloodWait", e.kind == "flood" and e.seconds == 42)
    check("map ChatWriteForbidden", tg.map_error(stubs.ChatWriteForbiddenError()).kind == "fatal")
    check("map PeerFlood", tg.map_error(stubs.PeerFloodError()).kind == "fatal")
    check("map ValueError (entity not found)", tg.map_error(ValueError("x")).kind == "fatal")
    check("map network", tg.map_error(ConnectionError()).kind == "temp")

    class Btn:
        def __init__(self, t): self.text = t
    class Msg:
        def __init__(self, rows): self.buttons = rows; self.clicked = None
        async def click(self, i=None, *, filter=None):
            self.clicked = ("idx", i) if filter is None else ("filter", [b.text for row in self.buttons for b in row if filter(b)])
    class FakeClient:
        def __init__(self): self.msgs = []; self.sent = []
        async def get_entity(self, r): return f"ent:{r}"
        async def get_messages(self, ent, limit, from_user): return self.msgs
        async def send_message(self, ent, text, parse_mode="x"): self.sent.append((ent, text, parse_mode))
    c = FakeClient(); s = tg.TelethonSender(c)
    await s.send_text("@g", "سلام"); check("send_text sends raw (parse_mode=None)", c.sent[-1] == ("ent:@g", "سلام", None))
    m1, m2 = Msg([[Btn("منو")]]), Msg([[Btn("میو پوینت"), Btn("جایزه")]])
    c.msgs = [m1, m2]; await s.tap("@bot", "@bot", "میو"); check("tap picks the message containing the button", m2.clicked == ("filter", ["میو پوینت"]) and m1.clicked is None, (m1.clicked, m2.clicked))
    c.msgs = [m1]; m1.clicked = None; await s.tap("@bot", "@bot", ""); check("tap without text clicks first button", m1.clicked == ("idx", 0))
    c.msgs = [Msg([])]
    try: await s.tap("@bot", "@bot", "x"); check("tap with no buttons raises temp", False)
    except SendError as ex: check("tap with no buttons raises temp", ex.kind == "temp")

asyncio.run(glue_tests())

# ───────────── panel wizard ─────────────
async def panel_tests():
    class CB:   # class qualname must contain "CallbackQuery"
        pass
    CB.__qualname__ = "CallbackQuery.Event"
    class MS: pass
    MS.__qualname__ = "NewMessage.Event"
    log = []
    def mk(kind, uid, data=None, text=""):
        o = (CB if kind == "cb" else MS)()
        o.sender_id = uid; o.raw_text = text; o.data = data.encode() if data else b""
        async def edit(t, buttons=None, parse_mode=None): log.append(("edit", t, buttons))
        async def respond(t, buttons=None, parse_mode=None): log.append(("respond", t, buttons))
        async def answer(m="", alert=False): log.append(("answer", m))
        o.edit, o.respond, o.answer = edit, respond, answer
        return o
    class PS(FakeSender):
        async def recent_groups(self, limit=60): return [(-1001, "Cats"), (-1002, "Dogs")]
    clk, sender, store = FakeClock(), PS(), MemoryStore()
    async def notify(t): pass
    cfg = Config(min_interval=20, global_gap_s=0)
    eng = Engine(store, sender, notify, cfg, clock=clk)
    auth = Auth({1}, {2: ""})
    p = panel.Panel(object(), eng, store, sender, auth, cfg, clock=clk); p.B = stubs.Button
    await p.on_text(mk("msg", 99, text="/start")); check("stranger gets no panel", all("منو" not in str(x) for x in log if x[0] != "respond" or "خصوصیه" in x[1]) )
    log.clear(); await p.on_text(mk("msg", 1, text="/start")); check("owner gets menu", "پنل کنترل" in log[-1][1])
    await p.on_callback(mk("cb", 1, "p:new")); await p.on_callback(mk("cb", 1, "p:k:text"))
    check("wizard lists account groups", any("Cats" in str(x[2]) for x in log[-1:]), log[-1])
    await p.on_callback(mk("cb", 1, "p:c:0")); await p.on_text(mk("msg", 1, text="میو || سلام"))
    await p.on_callback(mk("cb", 1, "p:iv:300")); await p.on_callback(mk("cb", 1, "p:du:18000"))
    check("confirm preview shown", "پیش‌نمایش" in log[-1][1] and "Cats" in log[-1][1], log[-1][1])
    await p.on_callback(mk("cb", 1, "p:ok"))
    j = list(eng.jobs.values())[0]
    check("wizard created the right job", (j.chat_ref, j.interval_s, j.texts, j.kind) == ("-1001", 300, ["میو", "سلام"], "text") and j.end_at == clk.t + 18000, j)
    # custom interval validation + click wizard + unlimited
    await p.on_callback(mk("cb", 2, "p:new")); await p.on_callback(mk("cb", 2, "p:k:click"))
    await p.on_text(mk("msg", 2, text="@MeowBot")); await p.on_text(mk("msg", 2, text="میو"))
    await p.on_callback(mk("cb", 2, "p:ivx")); await p.on_text(mk("msg", 2, text="5s"))
    check("custom interval below minimum rejected", "حداقل" in log[-1][1])
    await p.on_text(mk("msg", 2, text="۷ دقیقه")); await p.on_callback(mk("cb", 2, "p:du:0")); await p.on_callback(mk("cb", 2, "p:ok"))
    j2 = [x for x in eng.jobs.values() if x.kind == "click"][0]
    check("click wizard job (admin, unlimited)", j2.chat_ref == "@MeowBot" and j2.interval_s == 420 and j2.end_at is None and j2.extra["btn"] == "میو", j2)
    # job controls
    await p.on_callback(mk("cb", 2, f"p:pause:{j2.id}")); check("panel pause", eng.jobs[j2.id].status == "paused")
    await p.on_callback(mk("cb", 2, f"p:resume:{j2.id}")); check("panel resume", eng.jobs[j2.id].status == "active")
    await p.on_callback(mk("cb", 2, f"p:rmy:{j2.id}")); check("panel delete", j2.id not in eng.jobs)
    log.clear(); await p.on_callback(mk("cb", 2, "p:admins")); check("admin can't open admin management", any("فقط مالک" in str(x) for x in log))
    await p.on_callback(mk("cb", 99, "p:jobs")); check("stranger callback denied", log[-1][0] == "answer" and "دسترسی" in log[-1][1])
    log.clear(); await p.on_callback(mk("cb", 1, "p:admins")); check("owner sees admins", "ادمین" in log[-1][1])
    await p.on_callback(mk("cb", 1, "p:adr:2")); check("owner removes admin from panel", 2 not in auth.admins)

asyncio.run(panel_tests())
print("\nFAILED:", fails)
sys.exit(1 if fails else 0)
