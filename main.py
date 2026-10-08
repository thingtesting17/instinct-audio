# main.py — server version of instinct_audio.py (runs on Railway)
# Every minute: check the inbox for new emails from Instinct, turn each one
# into a voice note with ElevenLabs, and send it to you on Telegram.
# Railway keeps it running; logs show up in the Deployments tab.

import email
import html
import imaplib
import os
import re
import sys
import time
from email.header import decode_header, make_header
from email.utils import parseaddr

import requests

sys.stdout.reconfigure(line_buffering=True)  # show log lines immediately

# ====== SETTINGS — read from the server's environment variables ======
# No keys live in this file, so it's safe to upload to GitHub.
# You enter the real values in Railway under "Variables".

REQUIRED = [
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ELEVENLABS_API_KEY",
    "VOICE_SR", "VOICE_RU", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "INSTINCT_ADDRESS",
]
missing = [name for name in REQUIRED if not os.environ.get(name)]
if missing:
    raise SystemExit("Missing settings in Railway Variables: " + ", ".join(missing))

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]
VOICES = {"sr": os.environ["VOICE_SR"], "ru": os.environ["VOICE_RU"]}
GMAIL_ADDRESS = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
INSTINCT_ADDRESS = os.environ["INSTINCT_ADDRESS"]

DEFAULT_LANG = os.environ.get("DEFAULT_LANG", "sr")
CHECK_EVERY_SECONDS = int(os.environ.get("CHECK_EVERY_SECONDS", "60"))
MODEL = os.environ.get("MODEL", "eleven_v3")
# =======================================================================

MAX_TTS_CHARS = 5000    # eleven_v3's limit per request
MAX_TELEGRAM_CHARS = 4000


# ---------- ElevenLabs and Telegram (same idea as speak.py) ----------

def make_audio(text, lang):
    """Send text to ElevenLabs and get back MP3 audio."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICES[lang]}"
    r = requests.post(
        url,
        headers={"xi-api-key": ELEVENLABS_API_KEY},
        json={"text": text[:MAX_TTS_CHARS], "model_id": MODEL, "language_code": lang},
        timeout=120,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs error {r.status_code}: {r.text}")
    return r.content


def telegram(method, data, files=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, **data}, files=files, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Telegram error ({method}) {r.status_code}: {r.text}")


def send_to_telegram(audio, text):
    """Voice note first, then the text hidden behind a spoiler (tap to reveal)."""
    telegram("sendVoice", {}, files={"voice": ("message.mp3", audio, "audio/mpeg")})
    shown = html.escape(text[:MAX_TELEGRAM_CHARS])
    telegram("sendMessage", {"text": f"<tg-spoiler>{shown}</tg-spoiler>", "parse_mode": "HTML"})


# ---------- Reading the emails ----------

def html_to_text(raw):
    """Rough conversion of an HTML email to plain text."""
    raw = re.sub(r"(?is)<(script|style).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw)


def _decode(part):
    payload = part.get_payload(decode=True) or b""
    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def get_body(msg):
    """Pull the readable text out of an email."""
    plain, rich = None, None
    for part in msg.walk():
        if "attachment" in str(part.get("Content-Disposition", "")):
            continue
        if part.get_content_type() == "text/plain" and plain is None:
            plain = _decode(part)
        elif part.get_content_type() == "text/html" and rich is None:
            rich = html_to_text(_decode(part))
    text = plain if plain and plain.strip() else (rich or "")
    text = re.sub(r"\n{3,}", "\n\n", text)   # squash big gaps
    return text.strip()


def get_language(subject):
    """'SR: ...' -> sr, 'RU: ...' -> ru, anything else -> DEFAULT_LANG."""
    match = re.match(r"\s*(SR|RU)\b", subject, re.IGNORECASE)
    return match.group(1).lower() if match else DEFAULT_LANG


def check_inbox():
    """Process every unread email from Instinct, then mark it as read."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    mail.select("INBOX")

    _, found = mail.search(None, "UNSEEN", "FROM", f'"{INSTINCT_ADDRESS}"')
    for num in found[0].split():
        _, data = mail.fetch(num, "(BODY.PEEK[])")   # PEEK = don't mark read yet
        msg = email.message_from_bytes(data[0][1])

        sender = parseaddr(msg.get("From", ""))[1].lower()
        if sender != INSTINCT_ADDRESS.lower():
            continue  # search matched loosely; skip anything not exactly from Instinct

        subject = str(make_header(decode_header(msg.get("Subject", ""))))
        lang = get_language(subject)
        text = get_body(msg)
        print(f"New email: '{subject}' ({lang}, {len(text)} characters)")

        try:
            if not text:
                raise RuntimeError("the email had no text in it")
            send_to_telegram(make_audio(text, lang), text)
            print("  -> sent to Telegram")
        except Exception as error:
            print(f"  -> failed: {error}")
            try:
                telegram("sendMessage", {"text": f"Couldn't turn '{subject}' into audio: {error}"})
            except Exception:
                pass

        # Mark as read either way, so a broken email isn't retried every minute.
        mail.store(num, "+FLAGS", "\\Seen")

    mail.logout()


if __name__ == "__main__":
    print(f"Watching {GMAIL_ADDRESS} for emails from {INSTINCT_ADDRESS}.")
    while True:
        try:
            check_inbox()
        except Exception as error:
            # e.g. wifi drops for a moment — log it and try again next round
            print(f"Problem checking the inbox: {error}")
        time.sleep(CHECK_EVERY_SECONDS)
