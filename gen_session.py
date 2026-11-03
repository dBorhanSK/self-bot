# -*- coding: utf-8 -*-
"""Run this ON YOUR OWN COMPUTER (not on Render) once to create SESSION_STRING.
    pip install telethon
    python gen_session.py
Get API_ID / API_HASH from https://my.telegram.org  ->  API development tools.
The printed string gives FULL access to the account: keep it secret, only put it in Render env vars."""
from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("API_ID: ").strip())
api_hash = input("API_HASH: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:   # asks phone, code, 2FA password
    print("\n===== SESSION_STRING (secret!) =====\n" + client.session.save() + "\n====================================")
