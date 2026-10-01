#!/usr/bin/env python3
"""Board write API - deterministic, no model calls.

Owns data/board/queue.json (the captain's ordered candidate queue) and appends
every change to data/board/events.jsonl, then regenerates the static page.
nginx fronts it at /api/; this process only binds 127.0.0.1.

Sign-in (replaces the old basic-auth popup): GET /signin serves the in-page form,
POST /api/signin checks the captain's password from config/board-password and sets
`board_session` (exp.HMAC-SHA256 with config/board-session-secret; HttpOnly, Secure,
SameSite=Strict, 30 days). nginx auth_request calls GET /api/auth/check before every
page and /api route; no session -> pages redirect to /signin, API answers 401.
GET /api/signout clears the cookie. Failed sign-ins are rate-limited per client IP
(5 per 15 minutes) and logged to data/board/auth.log. Same-origin is enforced by checking Origin/Referer against the
Host header, plus a double-submit CSRF token: GET /api/queue sets cookie
`board_csrf`; every mutating POST must echo it in header `X-CSRF`.

queue.json: {"updated": iso, "items": [{key, summary, status, priority, assignee,
             url, reason, risk, state: candidate|in_progress|done|removed}]}
POST /api/move   {"key":..., "dir": "up"|"down"|"top"|"bottom"}
POST /api/order  {"keys": [...]}            full reorder (drag result)
POST /api/remove {"key":...}                mark removed (kept, greyed)
POST /api/restore {"key":...}               back to candidate
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import config as C
import hashlib, html, hmac, json, os, secrets, subprocess, sys, threading, time
from urllib.parse import parse_qs
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

H = C.FM_HOME
D = f"{H}/data/board"
QUEUE, EVENTS = f"{D}/queue.json", f"{D}/events.jsonl"
GEN = f"{D}/generate.py"
AUTHLOG = f"{D}/auth.log"
SESSION_TTL = 30 * 86400
FAIL_MAX, FAIL_WINDOW = 5, 15 * 60
_fails = {}; _fails_lock = threading.Lock()

def _secret():
    with open(f"{H}/config/board-session-secret") as f: return f.read().strip().encode()
def _password():
    with open(f"{H}/config/board-password") as f:
        for line in f:
            k, _, v = line.rstrip("\n").partition(":")
            if k.strip().lower() == "password": return v.strip()
    return None
def make_session():
    exp = str(int(time.time()) + SESSION_TTL)
    return exp + "." + hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
def valid_session(tok):
    if not tok or "." not in tok: return False
    exp, _, mac = tok.partition(".")
    if not exp.isdigit() or int(exp) < time.time(): return False
    return hmac.compare_digest(mac, hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest())
def authlog(ip, what):
    with open(AUTHLOG, "a") as f: f.write(f"{now()} {ip} {what}\n")
def throttled(ip):
    with _fails_lock:
        t = time.time(); _fails[ip] = [x for x in _fails.get(ip, []) if t - x < FAIL_WINDOW]
        return len(_fails[ip]) >= FAIL_MAX
def note_fail(ip):
    with _fails_lock: _fails.setdefault(ip, []).append(time.time())

SIGNIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOARD_TITLE</title><meta name="robots" content="noindex">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
<script>try{var t=localStorage.getItem('board-theme');if(t&&t!=='auto')document.documentElement.setAttribute('data-theme',t);}catch(e){}</script>
<style>
:root{--bg:#0b0b0c;--panel:#141415;--panel2:#1e1e20;--line:#26262a;--ink:#f4f4f2;--mute:#9a9a9f;--red:#ff8a80;--accent:#c8f542;--accent-ink:#0b0b0c;color-scheme:dark}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#ededec;--panel:#ffffff;--panel2:#f2f2f0;--line:#dcdcd8;--ink:#141414;--mute:#6b6b70;--red:#c62828;--accent:#c8f542;--accent-ink:#141414;color-scheme:light}}
html[data-theme="light"]{--bg:#ededec;--panel:#ffffff;--panel2:#f2f2f0;--line:#dcdcd8;--ink:#141414;--mute:#6b6b70;--red:#c62828;--accent:#c8f542;--accent-ink:#141414;color-scheme:light}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--ink);font:14px/1.45 "Manrope",system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
body{display:flex;align-items:center;justify-content:center;min-height:100vh;padding:16px;box-sizing:border-box}
.card{width:100%;max-width:360px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:28px;box-sizing:border-box}
.eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--mute)}.eyebrow i{width:18px;height:18px;border-radius:6px;background:var(--accent)}
h1{font-size:22px;margin:10px 0 20px;font-weight:700;letter-spacing:-.01em}
label{display:block;font-size:12px;color:var(--mute);margin-bottom:6px}
input{width:100%;box-sizing:border-box;background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:11px 12px;font:inherit}
input:focus{outline:none;border-color:var(--accent)}
button{margin-top:16px;width:100%;background:var(--accent);color:var(--accent-ink);border:0;border-radius:10px;padding:11px;font:inherit;font-weight:700;cursor:pointer}
.err{min-height:20px;margin-top:12px;color:var(--red);font-size:13px}
</style></head><body>
<form class="card" method="post" action="/api/signin" autocomplete="on">
<div class="eyebrow"><i></i>BOARD_TITLE</div><h1>BOARD_TITLE</h1>
<label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button><div class="err" role="alert">__ERR__</div></form></body></html>"""

def now(): return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def load():
    try:
        with open(QUEUE) as f: return json.load(f)
    except (OSError, ValueError): return {"updated": None, "items": []}
def save(q):
    q["updated"] = now(); tmp = QUEUE + ".tmp"
    with open(tmp, "w") as f: json.dump(q, f, indent=1)
    os.replace(tmp, QUEUE)
def event(kind, **kw):
    with open(EVENTS, "a") as f: f.write(json.dumps({"at": now(), "event": kind, "actor": "captain", **kw}) + "\n")
def regen():
    subprocess.run([sys.executable, GEN, "--fast"], env=dict(os.environ, FM_HOME=H), timeout=90, check=False)

class Handler(BaseHTTPRequestHandler):
    server_version = "ochre/1"
    def log_message(self, *a): pass
    def _json(self, code, obj, extra=None):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _same_origin(self):
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
        src = self.headers.get("Origin") or self.headers.get("Referer") or ""
        return bool(host) and urlparse(src).netloc.split(":")[0] == host.split(":")[0]
    def _cookie(self, name):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name: return v
        return None
    def _ip(self): return self.headers.get("X-Real-IP") or self.client_address[0]
    def _redirect(self, loc, cookie=None):
        self.send_response(303); self.send_header("Location", loc); self.send_header("Cache-Control", "no-store")
        if cookie: self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0"); self.end_headers()
    def _signin_page(self, err=""):
        body = SIGNIN_HTML.replace("__ERR__", err).replace("BOARD_TITLE", html.escape(C.get("title"))).encode()
        self.send_response(401 if err else 200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _authed(self): return valid_session(self._cookie("board_session"))
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/auth/check":
            return self._json(200, {"ok": True}) if self._authed() else self._json(401, {"error": "sign in required"})
        if path == "/signin":
            return self._redirect("/") if self._authed() else self._signin_page()
        if path == "/api/signout":
            return self._redirect("/signin", "board_session=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")
        if not self._authed(): return self._json(401, {"error": "sign in required"})
        if path != "/api/queue": return self._json(404, {"error": "not found"})
        tok = self._cookie("board_csrf") or secrets.token_urlsafe(24)
        self._json(200, dict(load(), csrf=tok), {"Set-Cookie": f"board_csrf={tok}; Path=/; Secure; HttpOnly; SameSite=Strict"})
    def do_POST(self):
        path = urlparse(self.path).path
        if not self._same_origin(): return self._json(403, {"error": "cross-origin refused"})
        if path == "/api/signin":
            ip = self._ip()
            if throttled(ip):
                authlog(ip, "refused: rate limited"); return self._signin_page("Too many attempts. Try again in 15 minutes.")
            try: n = min(int(self.headers.get("Content-Length", "0")), 4096)
            except ValueError: n = 0
            pw = (parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("password") or [""])[0].strip()
            real = _password()
            if real and hmac.compare_digest(pw.encode(), real.encode()):
                authlog(ip, "signed in")
                return self._redirect("/", f"board_session={make_session()}; Path=/; Max-Age={SESSION_TTL}; Secure; HttpOnly; SameSite=Strict")
            note_fail(ip); authlog(ip, "failed sign-in")
            return self._signin_page("Wrong password.")
        if not self._authed(): return self._json(401, {"error": "sign in required"})
        tok = self._cookie("board_csrf")
        if not tok or self.headers.get("X-CSRF") != tok: return self._json(403, {"error": "csrf"})
        try:
            n = int(self.headers.get("Content-Length", "0")); body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
        except ValueError: return self._json(400, {"error": "bad json"})
        q = load(); items = q["items"]; keys = [i["key"] for i in items]
        key = body.get("key")
        if path == "/api/order":
            new = [k for k in body.get("keys", []) if k in keys]
            if sorted(new) != sorted(keys): return self._json(400, {"error": "order must contain every key exactly once"})
            if new == keys: return self._json(200, q)
            items[:] = [next(i for i in items if i["key"] == k) for k in new]; event("reorder", before=keys, after=new)
        elif path == "/api/move":
            if key not in keys: return self._json(404, {"error": "unknown key"})
            d = body.get("dir")
            if d not in ("up", "down", "top", "bottom"): return self._json(400, {"error": "bad dir"})
            cand = [i["key"] for i in items if i.get("state") == "candidate"]   # moves act within the ranked lane
            if key not in cand: return self._json(409, {"error": f"{key} is not in Ranked next"})
            ci = cand.index(key)
            tj = {"up": max(0, ci - 1), "down": min(len(cand) - 1, ci + 1), "top": 0, "bottom": len(cand) - 1}[d]
            if tj != ci:
                mover = items.pop(keys.index(key))
                anchor = cand[tj]
                ai = [i["key"] for i in items].index(anchor)
                items.insert(ai if tj < ci else ai + 1, mover)
                event("move", key=key, dir=d, before=keys, after=[x["key"] for x in items])
        elif path in ("/api/remove", "/api/restore"):
            if key not in keys: return self._json(404, {"error": "unknown key"})
            it = items[keys.index(key)]
            if it.get("state") in ("in_progress", "done"): return self._json(409, {"error": f"{key} is {it['state']}"})
            it["state"] = "removed" if path == "/api/remove" else "candidate"; event(path[5:], key=key)
        else: return self._json(404, {"error": "not found"})
        save(q); regen(); self._json(200, q)

if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8787), Handler).serve_forever()
