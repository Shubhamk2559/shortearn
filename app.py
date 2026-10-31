"""Telegram source verification for ShortEarn (Flask + MongoDB).

Flow
1. POST /api/sources          -> saves the source and returns a short code (status "verify")
2. The user pastes the code in the channel or bot description inside Telegram
3. POST /api/sources/verify   -> reads the public t.me page; on a match the status becomes "approved"
4. A background job re-checks every source still in "verify" every few minutes

Install:  pip install -r requirements.txt
Env vars: SECRET_KEY, MONGO_URL, DEMO_MODE (1 only while testing)
Run with ONE worker (e.g. gunicorn -w 1), otherwise the scheduler runs once per worker.
"""
import html
import os
import re
import secrets
from datetime import datetime, timezone

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from bson import ObjectId
from flask import Flask, Response, jsonify, request, session
from pymongo import MongoClient

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
sources = MongoClient(os.environ["MONGO_URL"])["shortearn"]["sources"]
BASE = os.path.dirname(os.path.abspath(__file__))
DEMO_MODE = os.environ.get("DEMO_MODE") == "1"


def current_user():
    """Replace with your real login. DEMO_MODE=1 gives everyone one shared test user."""
    return session.get("user_id") or ("demo" if DEMO_MODE else None)

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I, O, 0, 1
PUBLIC_LINK = re.compile(r"^https?://(?:t|telegram)\.me/([A-Za-z0-9_]{4,32})/?$", re.I)
OG_DESC = re.compile(r'<meta\s+property="og:description"\s+content="(.*?)"', re.S | re.I)


def new_code() -> str:
    return "SE-" + "".join(secrets.choice(ALPHABET) for _ in range(6))


def public_description(username: str) -> str:
    """Description text that Telegram shows on the public page of a channel or bot."""
    r = requests.get(
        f"https://t.me/{username}",
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0 (ShortEarn verifier)"},
    )
    r.raise_for_status()
    m = OG_DESC.search(r.text)
    return html.unescape(m.group(1)) if m else ""


def check(src: dict) -> tuple[bool, str]:
    m = PUBLIC_LINK.match(src["url"])
    if not m:
        return False, "Only public t.me/username links can be verified."
    try:
        text = public_description(m.group(1))
    except requests.RequestException:
        return False, "Telegram could not be reached. Try again soon."
    if src["code"].lower() in text.lower():
        return True, ""
    return False, "Code not found in the description yet. Save it in Telegram and try again."


def approve(src_id) -> None:
    sources.update_one(
        {"_id": src_id},
        {"$set": {"status": "approved", "auto": True, "verified_at": datetime.now(timezone.utc)}},
    )


@app.get("/")
def panel():
    with open(os.path.join(BASE, "index.html"), encoding="utf-8") as f:
        page = f.read().replace("const VERIFY_API='';", "const VERIFY_API='/api/sources/verify';")
    return Response(page, mimetype="text/html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/sources")
def list_sources():
    uid = current_user()
    if not uid:
        return jsonify([]), 401
    rows = sources.find({"user_id": uid}).sort("created_at", -1)
    return jsonify([
        {"id": str(x["_id"]), "t": x["type"], "u": x["url"], "d": x["created_at"].isoformat() + "Z",
         "s": x["status"], "code": x["code"], "auto": x.get("auto", False)}
        for x in rows
    ])


@app.post("/api/switch")
def switch_user():
    """Account switcher for the dashboard. Works only while DEMO_MODE=1.
    With a real login system, switch users there instead (this route stays disabled)."""
    if not DEMO_MODE:
        return jsonify(error="Switching is disabled."), 403
    raw = str((request.get_json(silent=True) or {}).get("user", "")).lower()
    uid = re.sub(r"[^a-z0-9_-]", "", raw)[:30]
    if not uid:
        return jsonify(error="Unknown user."), 400
    session["user_id"] = uid
    return jsonify(ok=True)


@app.delete("/api/sources/<sid>")
def delete_source(sid):
    uid = current_user()
    if not uid:
        return jsonify(error="Please log in."), 401
    try:
        sources.delete_one({"_id": ObjectId(sid), "user_id": uid})
    except Exception:
        return jsonify(error="Unknown source."), 400
    return jsonify(ok=True)


@app.post("/api/sources")
def add_source():
    uid = current_user()
    if not uid:
        return jsonify(error="Please log in."), 401
    url = (request.get_json(force=True).get("url") or "").strip()
    if not PUBLIC_LINK.match(url):
        return jsonify(error="Use a public link like https://t.me/yourchannel"), 400
    key = url.lower().rstrip("/")
    if sources.find_one({"key": key}):
        return jsonify(error="This source was already added."), 409
    doc = {
        "user_id": uid, "type": "telegram", "url": url, "key": key,
        "code": new_code(), "status": "verify",
        "created_at": datetime.now(timezone.utc),
    }
    res = sources.insert_one(doc)
    return jsonify(id=str(res.inserted_id), code=doc["code"], status="verify")


@app.post("/api/sources/verify")
def verify_source():
    uid = current_user()
    if not uid:
        return jsonify(verified=False, reason="Please log in."), 401
    try:
        sid = ObjectId(request.get_json(force=True).get("id"))
    except Exception:
        return jsonify(verified=False, reason="Unknown source."), 400
    src = sources.find_one({"_id": sid, "user_id": uid})  # the code is read from the database, never from the client
    if not src:
        return jsonify(verified=False, reason="Unknown source."), 404
    if src["status"] == "approved":
        return jsonify(verified=True)
    ok, reason = check(src)
    if ok:
        approve(sid)
    return jsonify(verified=ok, reason=reason)


def recheck_pending() -> None:
    for src in sources.find({"status": "verify"}):
        ok, _ = check(src)
        if ok:
            approve(src["_id"])


scheduler = BackgroundScheduler()
scheduler.add_job(recheck_pending, "interval", minutes=5)
scheduler.start()

if __name__ == "__main__":
    app.run(debug=False)
