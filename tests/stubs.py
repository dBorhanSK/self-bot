"""Minimal telethon stub so glue modules import & run offline."""
import sys, types

class _Ev:
    def __init__(self, **kw): self.kw = kw
class CallbackQuery:
    class Event: pass
class Button:
    @staticmethod
    def inline(text, data=None): return (text, data.decode() if isinstance(data, bytes) else data)

def install():
    t = types.ModuleType("telethon")
    t.events = types.SimpleNamespace(NewMessage=_Ev, CallbackQuery=_Ev)
    t.Button = Button
    t.TelegramClient = object
    sys.modules["telethon"] = t
    s = types.ModuleType("telethon.sessions"); s.StringSession = object; sys.modules["telethon.sessions"] = s

class FloodWaitError(Exception):
    def __init__(self, seconds): super().__init__(f"wait {seconds}"); self.seconds = seconds
class ChatWriteForbiddenError(Exception): pass
class PeerFloodError(Exception): pass
class BotResponseTimeoutError(Exception): pass
