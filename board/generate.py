#!/usr/bin/env python3
"""Deterministic agent-fleet status board generator (no model calls).

Renders one static HTML page from a Firstmate home's durable records only:
  - bin/fm-fleet-snapshot.sh --json   -> workers, their state feed and recorded PRs
  - state/<id>.meta                   -> each worker's real model and effort
  - /proc (or lsof where there is none) -> which worker agents are actually running
  - bin/fm-tasks-axi.sh list          -> backlog (queued work)
  - data/board/queue.json             -> the captain's ordered candidate queue
  - data/board/tickets.jsonl          -> tickets this home picked / opened PRs for / saw closed
  - data/board/events.jsonl           -> queue events (reorders, removals, refills)
  - data/board/fleet.json             -> orchestrator name/model and the real ship-slot capacity
  - data/board/jira-status.json       -> live Jira status per ticket (read-only, refreshed at most every 8 minutes)
  - data/prioritization.md, data/pr-protocol.md, data/slot-policy.md, data/completeness-rule.md,
    data/<rules_sync_dir>/README.md, data/<rules_dir>/VERSIONS.json -> the Rules section
Secrets never enter the page: only ticket keys, task titles, states, model names and PR URLs.
Usage: generate.py [--fast] [--out PATH]   (default: config out)
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import config as C
import fleetlib as FL
REPO_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
import base64, csv, html, io, json, os, re, subprocess, sys, time, urllib.request
from datetime import datetime, timezone

H = C.FM_HOME
OUT = C.get("out")
LIVE = "--live" in sys.argv          # every 30s: refresh worker state + backlog, reuse GitHub/Jira/usage from the last full run
FAST = "--fast" in sys.argv or LIVE  # queue edits: reuse the slow sources cached by the last full run
if "--out" in sys.argv:
    OUT = sys.argv[sys.argv.index("--out") + 1]
SRC_CACHE = f"{H}/data/board/.sources-cache.json"
REPOS = C.GH_REPOS
REPO = REPOS[0]
MAIN = C.PROFILE == "main"           # main-home profile: panels without a source file are not drawn
JIRA = C.JIRA_BROWSE
CARDS_PER_LANE = 3
FAILED = []  # names of sources that could not be read


# ------------------------------------------------------------------ small helpers
def read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def jread(path, default):
    txt = read(path)
    if txt is None:
        return default
    try:
        return json.loads(txt)
    except ValueError:
        FAILED.append(os.path.basename(path))
        return default


def meta_field(tid, key):
    for ln in (read(f"{H}/state/{tid}.meta") or "").splitlines():
        if ln.startswith(key + "="):
            return ln.split("=", 1)[1].strip()
    return ""


def run(cmd, timeout=60):
    env = dict(os.environ, FM_HOME=H, FM_CREW_STATE_NO_FORGE="1")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=H)


SECRET_RE = re.compile(r"(ATATT[0-9A-Za-z_\-]+|xox[abprs]-[0-9A-Za-z\-]{10,}|gh[pousr]_[0-9A-Za-z]{20,}|[A-Za-z0-9_\-]{32,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{20,})")


def esc(s):
    return html.escape(SECRET_RE.sub("[redacted]", str("" if s is None else s)), quote=True)


def when(ts):
    return str(ts or "").replace("T", " ").replace("Z", "")[:19]


def inline(s):
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r'<strong style="font-weight:600">\1</strong>', s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


def md_lite(text):
    """Small deterministic Markdown subset: paragraphs, ordered and bullet lists, ## sub-heads."""
    out, block = [], []

    def flush():
        if not block:
            return
        kind = "ol" if re.match(r"^\d+\.\s", block[0]) else "ul" if block[0].startswith("- ") else "p"
        if kind == "p":
            out.append(f'<p style="margin:0">{inline(" ".join(block))}</p>')
        else:
            items = "".join(f"<li>{inline(re.sub(r'^(\d+\.|-)\s+', '', b))}</li>" for b in block)
            out.append(f'<{kind} class="rl">{items}</{kind}>')
        block.clear()

    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line.strip():
            flush()
        elif line.startswith("# "):
            flush()
        elif line.startswith("## "):
            flush()
            out.append(f'<div style="font-weight:600;margin-bottom:-6px">{esc(line[3:])}</div>')
        elif re.match(r"^(\d+\.|-)\s", line):
            if block and not re.match(r"^(\d+\.|-)\s", block[0]):
                flush()
            block.append(line)
        else:
            if block and re.match(r"^(\d+\.|-)\s", block[0]):
                block[-1] += " " + line.strip()
            else:
                block.append(line.strip())
    flush()
    return "".join(out)


# ------------------------------------------------------------------ sources
_cache = {}
try:
    _cache = json.load(open(SRC_CACHE))
except (OSError, ValueError):
    _cache = {}
_use_cache = FAST and not LIVE and time.time() - float(_cache.get("at") or 0) < 900 and "snap" in _cache
snap = {}
try:
    if _use_cache:
        snap = _cache["snap"]
    else:
        _fixed = f"{H}/data/board/fleet-snapshot.json"  # static snapshot (sample/demo homes)
        if os.path.exists(_fixed):
            snap = jread(_fixed, {})
        else:
            p = run([f"{H}/bin/fm-fleet-snapshot.sh", "--json"], 90)
            snap = json.loads(p.stdout) if p.stdout.strip() else {}
    if not isinstance(snap.get("tasks"), list):
        raise ValueError("no tasks")
except Exception:  # noqa: BLE001
    FAILED.append("fleet snapshot")
    snap = {"tasks": []}

BACKLOG_OK, backlog_queued = True, []
try:
    if _use_cache:
        raise LookupError
    if os.path.exists(f"{H}/data/board/backlog.txt"):  # static backlog listing (sample/demo homes)
        p = subprocess.CompletedProcess([], 0, read(f"{H}/data/board/backlog.txt"), "")
    else:
        p = run([f"{H}/bin/fm-tasks-axi.sh", "list", "--state", "queued"], 60)
    if p.returncode != 0 and "error" in p.stdout.lower():
        raise ValueError(p.stdout[:120])
    for line in p.stdout.splitlines():
        if line.startswith("  ") and "," in line:
            row = next(csv.reader(io.StringIO(line.strip())), [])
            if len(row) >= 5:
                blob = " ".join(row)
                m = re.search(r"\b" + C.KEY_RE + r"\b", blob)
                if m:
                    backlog_queued.append({"key": m.group(0), "id": row[0], "title": re.sub(r"\\n.*$", "", row[4])})
except LookupError:
    backlog_queued = _cache.get("backlog_queued") or []
    BACKLOG_OK = bool(_cache.get("backlog_ok", True))
    if not BACKLOG_OK:
        FAILED.append("backlog")
except Exception:  # noqa: BLE001
    BACKLOG_OK = False
    FAILED.append("backlog")
if not _use_cache and snap.get("tasks") is not None and "fleet snapshot" not in FAILED:
    try:
        with open(SRC_CACHE + ".tmp", "w") as _f:
            json.dump({"at": time.time(), "snap": snap, "backlog_queued": backlog_queued, "backlog_ok": BACKLOG_OK}, _f)
        os.replace(SRC_CACHE + ".tmp", SRC_CACHE)
    except OSError:
        pass

queue = jread(f"{H}/data/board/queue.json", {})
qitems = [i for i in (queue.get("items") or []) if isinstance(i, dict) and i.get("key")]
fleet = jread(f"{H}/data/board/fleet.json", {})
if not FAST and not MAIN:  # Jira sync and the cap gate are team-home ops scripts
    subprocess.run([sys.executable, os.path.join(REPO_ROOT, "ops", "jira_pr_sync.py")], env=dict(os.environ, FM_HOME=H), timeout=120, check=False, capture_output=True)
    subprocess.run([sys.executable, os.path.join(REPO_ROOT, "ops", "cap_gate.py")], env=dict(os.environ, FM_HOME=H), timeout=90, check=False, capture_output=True)
CAPG = jread(f"{H}/data/board/cap.json", {})
CAP_KNOWN = bool(CAPG.get("effective") or fleet.get("ship_slots"))
SHOW_CAP = CAP_KNOWN or not MAIN
CAPACITY = int(CAPG.get("effective") or fleet.get("ship_slots") or 3)

tickets = []
for line in (read(f"{H}/data/board/tickets.jsonl") or "").splitlines():
    if line.strip():
        try:
            tickets.append(json.loads(line))
        except ValueError:
            pass
events = []
for line in (read(f"{H}/data/board/events.jsonl") or "").splitlines():
    if line.strip():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass


def jira_statuses(keys):
    path = f"{H}/data/board/jira-status.json"
    cache = jread(path, {})
    have = cache.get("status") or {}
    if time.time() - float(cache.get("fetched_at") or 0) < 480 and set(keys) <= set(have):
        return have
    env = {}
    for ln in (read(f"{H}/.env") or "").splitlines():
        if "=" in ln and not ln.lstrip().startswith("#"):
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    if not (env.get("JIRA_SITE") and env.get("JIRA_EMAIL") and env.get("JIRA_API_TOKEN")):
        return have
    try:
        body = json.dumps({"jql": "key in (%s)" % ",".join(sorted(keys)), "fields": ["status"], "maxResults": 100}).encode()
        req = urllib.request.Request(env["JIRA_SITE"].rstrip("/") + "/rest/api/3/search/jql", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{env['JIRA_EMAIL']}:{env['JIRA_API_TOKEN']}".encode()).decode())
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        fresh = {i["key"]: (i.get("fields", {}).get("status") or {}).get("name") for i in data.get("issues", [])}
        have = {**have, **{k: v for k, v in fresh.items() if v}}
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"fetched_at": time.time(), "status": have}, f)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001  keep the last known statuses
        pass
    return have


JSTAT = jira_statuses([i["key"] for i in qitems])


# ------------------------------------------------------------------ derived facts
def pr_state_from(event):
    low = str(event or "").lower()
    if "merged" in low:
        return "merged"
    if "closed" in low or "abandon" in low:
        return "closed"
    return "open"


def pr_ledger():
    """All of this team's PRs from every configured repo (full refresh only; --live/--fast read the cache)."""
    path = f"{H}/data/board/prs.json"
    cached = (jread(path, {}) or {}).get("prs") or []
    if FAST:
        return cached
    merged, failed = [], []
    for repo in REPOS:
        try:
            p = run(["gh", "pr", "list", "-R", repo, "--author", C.GH_AUTHOR, "--state", "all", "--limit", "100",
                     "--json", "number,title,state,isDraft,createdAt,mergedAt,closedAt,headRefName,url"], 60)
            data = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
        except Exception:  # noqa: BLE001
            data = None
        if isinstance(data, list):
            merged += [dict(x, repo=repo) for x in data]
        else:  # keep this repo's last known PRs rather than dropping them
            failed.append(repo)
            merged += [x for x in cached if x.get("repo") == repo or (not x.get("repo") and len(REPOS) == 1)]
    if failed:
        FAILED.append("GitHub PRs" + (" (%s)" % ", ".join(failed) if len(REPOS) > 1 else ""))
    if len(failed) < len(REPOS):
        try:
            with open(path + ".tmp", "w") as f:
                json.dump({"fetched_at": time.time(), "prs": merged}, f)
            os.replace(path + ".tmp", path)
        except OSError:
            pass
    return merged


def pr_key(pr):
    m = re.search(C.KEY_RE, (pr.get("title") or "").upper()) or re.search(C.KEY_RE, (pr.get("headRefName") or "").upper().replace("FIX-" + C.KEY + "-", C.KEY + "-"))
    return m.group(0) if m else ""


LEDGER = pr_ledger()
# Branch -> task id, so a PR whose title carries no ticket key still lands on its task's card.
BRANCH_TASK = {}
for _t in snap.get("tasks") or []:
    if isinstance(_t, dict) and _t.get("id") and _t.get("branch"):
        BRANCH_TASK.setdefault(str(_t["branch"]), []).append(_t)


def pr_ref(pr):
    """What a PR's card is keyed on: its ticket key, else the task id whose branch it came from."""
    k = pr_key(pr)
    if k:
        return k
    for t in BRANCH_TASK.get(pr.get("headRefName") or "", []):
        repo = FL.task_repo(t, meta_field(t["id"], "repo"), REPOS)
        if not repo or not pr.get("repo") or repo == pr["repo"]:
            return str(t["id"])
    return ""


prs = {}  # ticket -> {pr_url: state}, ordered by event time
merged_note = {}
for e in sorted(tickets, key=lambda x: str(x.get("at", ""))):
    k, u = e.get("ticket"), e.get("pr")
    if k and u and str(u).startswith("https://"):
        d = prs.setdefault(k, {})
        st = pr_state_from(e.get("event"))
        d[u] = "merged" if d.get(u) == "merged" else st
        if st == "merged":
            m = re.search(r"merged by ([\w\-]+)", str(e.get("event")))
            merged_note[k] = "Merged by %s" % (m.group(1) if m else "a human") + (" · Jira %s" % JSTAT.get(k) if JSTAT.get(k) else "")
for it in qitems:
    if it.get("pr"):
        prs.setdefault(it["key"], {}).setdefault(it["pr"], it.get("pr_state") or "open")
for _pr in sorted(LEDGER, key=lambda x: x.get("createdAt") or ""):  # GitHub is the source of truth for PR state
    _k = pr_ref(_pr)
    if _k:
        _st = "merged" if _pr.get("state") == "MERGED" else "closed" if _pr.get("state") == "CLOSED" else ("draft" if _pr.get("isDraft") else "open")
        _d = prs.setdefault(_k, {})
        _d.pop(_pr["url"], None)
        _d[_pr["url"]] = _st
        if _st == "merged" and _k not in merged_note:
            merged_note[_k] = "Merged" + (" · Jira %s" % JSTAT.get(_k) if JSTAT.get(_k) else "")


def worker_ticket(tid):
    m = re.match(r"fix-(" + C.KEY.lower() + r"-\d+)", tid or "")
    return m.group(1).upper() if m else ""


def worker_ref(tid):
    """Identity a worker's card and PRs are keyed on: its ticket key, else its task id."""
    return worker_ticket(tid) or tid or ""


def live_agent_worktrees():
    """Recorded task worktrees with a running worker agent (claude / pi / node) whose cwd is that worktree."""
    import glob as _glob
    wts = set()
    for m in _glob.glob(f"{H}/state/*.meta"):
        for ln in (read(m) or "").splitlines():
            if ln.startswith("worktree="):
                wts.add(ln[9:].strip())
    try:
        return FL.live_worktrees(wts)
    except (OSError, subprocess.SubprocessError):
        FAILED.append("live worker detection")
        return set()


LIVE = live_agent_worktrees()


workers = []
for t in snap.get("tasks") or []:
    if not isinstance(t, dict) or t.get("kind") not in ("ship", "scout"):
        continue
    tid = t.get("id")
    wt = meta_field(tid, "worktree")
    live = bool(wt) and wt in LIVE
    cs = (t.get("current_state") or {}).get("state") if isinstance(t.get("current_state"), dict) else None
    if not live:
        state = cs if cs in ("done", "failed") else "stopped"
    else:
        state = cs or "working"
    model = meta_field(tid, "model")
    effort = meta_field(tid, "effort")
    key = worker_ref(tid)
    pr = (t.get("pr") or {}).get("url") if isinstance(t.get("pr"), dict) else None
    if pr:
        prs.setdefault(key, {}).setdefault(pr, "open")
    workers.append({"task": tid, "kind": t.get("kind"), "state": state, "live": live, "key": key, "ticket": worker_ticket(tid),
                    "repo": FL.task_repo(t, meta_field(tid, "repo"), REPOS) or (REPOS[0] if len(REPOS) == 1 else ""),
                    "model": (model + (" · " + effort if effort else "")) if model else "not reported",
                    "has_window": bool(meta_field(tid, "window")), "meta_pr": meta_field(tid, "pr") or ""})
ACTIVE = sum(1 for w in workers if w["live"] and w["kind"] == "ship")


STOP_REASONS = jread(f"{H}/data/board/stop-reasons.json", {})


def last_status(tid):
    lines = [ln for ln in (read(f"{H}/state/{tid}.status") or "").splitlines() if ln.strip()]
    if not lines:
        return "", ""
    m = re.match(r"^([a-z\-]+)((?:\s*\[[^\]]*\])*)\s*:\s*(.*)$", lines[-1])
    return (m.group(1), m.group(3)) if m else ("", "")


def first_clause(text, n=70):
    t = re.split(r"[;\u2014]| - ", str(text or ""), maxsplit=1)[0].strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "\u2026"


def stop_reason(w):
    """One-line reason for a non-running worker, from structured records only (no model calls)."""
    rec = STOP_REASONS.get(w["task"])
    if isinstance(rec, dict) and rec.get("reason"):
        return str(rec["reason"])
    if isinstance(rec, str) and rec:
        return rec
    pr = latest_pr(w["key"])
    if pr and pr["state"] == "merged":
        return "PR merged"
    if pr and pr["state"] == "closed":
        return "PR closed"
    prefix, note = last_status(w["task"])
    if pr and pr["state"] == "open" and prefix in ("done", "paused", "resolved", ""):
        return "PR open - waiting for review"
    if prefix == "paused":
        return "parked: " + first_clause(note)
    if prefix == "blocked":
        return "blocked: " + first_clause(note)
    if prefix == "needs-decision":
        return "waiting on a decision"
    if prefix == "failed":
        return "failed: " + first_clause(note)
    if prefix == "working" and w.get("has_window"):
        return "crashed / endpoint gone"
    return "reason not recorded"


def latest_pr(key):
    d = prs.get(key) or {}
    if not d:
        return None
    u = list(d)[-1]
    n = re.search(r"/pull/(\d+)", u)
    return {"url": u, "num": n.group(1) if n else "", "state": d[u]}


def split_summary(s):
    m = re.match(r"^\s*(\[[^\]]+\])\s*(.*)$", str(s or ""))
    return (m.group(1), m.group(2)) if m else ("", str(s or ""))


def prio(p):
    return re.sub(r"\s*-\s*", " · ", str(p or ""), count=1)


def chips_and_reason(reason):
    reason = str(reason or "")
    if " · " in reason:
        main, tail = reason.split(" · ", 1)
        parts = re.split(r",\s+(?=[a-zA-Z ]+:)", tail)
        chips = [c.strip() for c in parts if c.strip() and not c.strip().lower().startswith("status:")]
        return main.strip(), chips
    return reason.strip(), []


LANES = [("ranked", "Ranked next", "var(--blue)", "top is picked next", "Queue empty"),
         ("working", "Working", "var(--green)", "worker active or PR awaiting review", "No active workers"),
         ("parked", "Parked", "var(--amber)", "fix complete or waiting on a ruling", "Nothing parked"),
         ("done", "Done", "var(--purple)", "merged by humans", "Nothing merged yet")]
lane_of_state = {"candidate": "ranked", "in_progress": "working", "parked": "parked", "blocked": "parked", "done": "done"}
by_lane = {k: [] for k, *_ in LANES}
def evidence_lane(it):
    """Lane from GitHub/worker evidence, falling back to the hand-kept queue.json state."""
    q = lane_of_state.get(it.get("state"))
    states = list((prs.get(it["key"]) or {}).values())
    if q == "parked" or it.get("state") == "removed":
        return q
    if any(w["key"] == it["key"] and w["live"] for w in workers):
        return "working"
    if "open" in states or "draft" in states:
        return "working"
    if states and all(x in ("merged", "closed") for x in states) and "merged" in states:
        return "done"
    return q


for it in qitems:
    q = lane_of_state.get(it.get("state"))
    ln = evidence_lane(it)
    if ln and q and ln != q:
        it["drift"] = "queue says %s" % dict((k, t) for k, t, *_ in LANES).get(q, q)
    if ln:
        by_lane[ln].append(it)
removed = [it for it in qitems if it.get("state") == "removed"]
SHOW_QUEUE = not MAIN or os.path.exists(f"{H}/data/board/queue.json")
if SHOW_QUEUE and (not BACKLOG_OK or backlog_queued):
    LANES.append(("backlog", "Backlog", "var(--line)", "queued in the backlog" if BACKLOG_OK else "source unreadable",
                  "Nothing queued" if BACKLOG_OK else "Backlog could not be read"))
    by_lane["backlog"] = [{"key": b["key"], "summary": b["title"], "state": "backlog"} for b in backlog_queued]


# ------------------------------------------------------------------ rendering
def button(cls, title, glyph, act, key, extra=""):
    return f'<button class="ib {cls}" title="{title}" data-act="{act}" data-key="{esc(key)}" {extra}>{glyph}</button>'


def controls(key):
    return ('<div class="ctl">' + button("", "to top", "⤒", "move", key, 'data-dir="top"') + button("", "up", "↑", "move", key, 'data-dir="up"')
            + button("", "down", "↓", "move", key, 'data-dir="down"') + button("x", "remove from queue", "✕", "remove", key) + "</div>")


def pr_pill(state):
    color = {"open": "var(--green)", "merged": "var(--purple)", "closed": "var(--red)", "submitted": "var(--amber)"}.get(state, "var(--mute)")
    return f'<span class="prs" style="color:{color};background:color-mix(in oklab, {color} 20%, transparent)">{esc(state)}</span>'


def worker_for(key):
    live = [w["task"] for w in workers if w["key"] == key and w["live"]]
    return live[0] if live else next((w["task"] for w in workers if w["key"] == key), "")


# ---- token usage (data/board/usage.py; deterministic, no model calls)
if not FAST:
    subprocess.run([sys.executable, os.path.join(REPO_ROOT, "board", "usage.py")], env=dict(os.environ, FM_HOME=H), timeout=120, check=False, capture_output=True)
USAGE = jread(f"{H}/data/board/usage.json", {})


def fmt_tok(n):
    n = int(n or 0)
    return f"{n/1e9:.2f}B" if n >= 1e9 else f"{n/1e6:.1f}M" if n >= 1e6 else f"{n/1e3:.0f}K" if n >= 1e3 else str(n)


def ticket_tokens(key):
    k = key.lower()
    hits = [v for t, v in (USAGE.get("tickets") or {}).items() if re.search(rf"(^|-){re.escape(k)}(-|$)", t)]
    return sum(v.get("tokens", 0) for v in hits) if hits else None


def tok_label(key):
    n = ticket_tokens(key)
    return f"{fmt_tok(n)} tokens" if n is not None else "tokens: not attributable"


def card(it, lane, rank=None):
    key = it["key"]
    area, summ = split_summary(it.get("summary"))
    pr = latest_pr(key)
    jira = JSTAT.get(key) or it.get("jira_status") or it.get("status") or ""
    note = it.get("note") or ""
    color = "var(--mute)" if lane == "done" else "var(--amber)"
    if not note:
        if lane == "done":
            note = merged_note.get(key, "")
        elif lane == "working" and pr and pr["state"] == "open" and not any(w["key"] == key and w["live"] for w in workers):
            note = "PR open · waiting for human review; the worker is closed"
        elif lane == "parked" and it.get("state") == "blocked":
            note = "Blocked · waiting on a ruling"
    reason, chips = chips_and_reason(it.get("reason"))
    risk = it.get("risk") or ""
    top = ""
    if rank is not None:
        bg, ink = ("var(--accent)", "var(--accent-ink)") if rank == 1 else ("var(--panel2)", "var(--mute)")
        top += f'<span class="rank" style="background:{bg};color:{ink}">{rank}</span>'
    top += f'<a class="key" href="{JIRA}{esc(key)}" target="_blank" rel="noopener">{esc(key)}</a>'
    if jira:
        top += f'<span class="pill">{esc(jira)}</span>'
    if it.get("drift"):
        top += f'<span class="pill" style="color:var(--amber)" title="The queue record disagrees with GitHub/worker evidence">{esc(it["drift"])}</span>'
    if it.get("milestone"):
        top += f'<span class="pill ms">{esc(it["milestone"])}</span>'
    top += f'<span class="prio">{esc(prio(it.get("priority")))}</span>'
    body = f'<div class="sum"><span style="color:var(--mute)">{esc(area)}</span> {esc(summ)}</div>'
    if pr:
        w = worker_for(key)
        body += (f'<div class="prrow"><a href="{esc(pr["url"])}" target="_blank" rel="noopener">PR #{esc(pr["num"])}</a>'
                 f'{pr_pill(pr["state"])}<span style="color:var(--mute)">{esc(w)}</span></div>')
    if note:
        body += f'<div style="font-size:12px;color:{color}">{esc(note)}</div>'
    body += f'<div class="tok">{esc(tok_label(key))}</div>'
    footer = '<div class="foot"><button class="tg" data-toggle="%s"><span class="chev">▸</span>Why picked · risk</button>' % esc(key)
    if rank is not None:
        footer += controls(key)
    footer += "</div>"
    detail = '<div class="detail" hidden>'
    detail += f'<div style="text-wrap:pretty">{esc(reason) or "No selection rationale recorded."}</div>'
    if chips:
        detail += '<div style="display:flex;flex-wrap:wrap;gap:4px">' + "".join(f'<span class="pill">{esc(c)}</span>' for c in chips) + "</div>"
    if risk:
        detail += f'<div class="riskrow"><span>Risk</span><span style="color:var(--ink);text-wrap:pretty">{esc(risk)}</span></div>'
    detail += "</div>"
    drag = ' draggable="true"' if rank is not None else ""
    mv = " moving" if lane == "working" and any(w["key"] == key and w["live"] for w in workers) else ""
    return f'<article class="card{mv}" data-key="{esc(key)}"{drag}><div class="top">{top}</div>{body}{footer}{detail}</article>'


lane_html, full_rows, ranked_keys = [], [], []
_wk = time.time() - 7 * 86400
merged_count = sum(1 for _p in LEDGER if _p.get("state") == "MERGED" and _p.get("mergedAt") and datetime.fromisoformat(_p["mergedAt"].replace("Z", "+00:00")).timestamp() >= _wk)
for lk, title, color, hint, empty in LANES:
    items = by_lane.get(lk) or []
    shown = items[:CARDS_PER_LANE]
    cards = ""
    for n, it in enumerate(shown, 1):
        cards += card(it, lk, n if lk == "ranked" else None) if it.get("state") != "backlog" else (
            f'<article class="card"><div class="top"><a class="key" href="{JIRA}{esc(it["key"])}" target="_blank" rel="noopener">{esc(it["key"])}</a></div>'
            f'<div class="sum">{esc(it.get("summary"))}</div></article>')
    if not items:
        cards = f'<div class="empty">{esc(empty)}</div>'
    more = f'<a class="more morelink" href="#queue-block">+{len(items) - CARDS_PER_LANE} more · show as table</a>' if len(items) > CARDS_PER_LANE else ""
    lane_html.append(
        f'<div class="lane"><div class="lhead"><div class="l1"><span class="dot" style="background:{color}"></span><span class="lt">{esc(title)}</span>'
        f'<span class="cnt">{len(items)}</span></div><span class="lh">{esc(hint)}</span></div>{cards}{more}</div>')
    for n, it in enumerate(items, 1):
        if lk == "ranked":
            ranked_keys.append(it["key"])
        area, summ = split_summary(it.get("summary"))
        pr = latest_pr(it["key"])
        prcell = f'<a href="{esc(pr["url"])}" target="_blank" rel="noopener">#{esc(pr["num"])}</a>' if pr else ""
        full_rows.append(
            f'<div class="frow" data-key="{esc(it["key"])}"><span class="rk">{n if lk == "ranked" else "—"}</span>'
            f'<a class="key" href="{JIRA}{esc(it["key"])}" target="_blank" rel="noopener">{esc(it["key"])}</a>'
            f'<span class="fms">{esc(it.get("milestone") or "—")}</span>'
            f'<span class="fsum"><span style="color:var(--mute)">{esc(area)}</span> {esc(summ)}</span>'
            f'<span class="fstate"><span class="dot" style="background:{color}"></span>{esc(title)}</span>'
            f'<span style="font-size:12px">{prcell}</span><span class="ftok">{esc(fmt_tok(ticket_tokens(it["key"])) if ticket_tokens(it["key"]) is not None else "n/a")}</span><span class="fctl">{controls(it["key"]) if lk == "ranked" else ""}</span></div>')
all_keys = [i["key"] for i in qitems]
order_saved = when(queue.get("updated")) or "never"
now = datetime.now(timezone.utc)

WORKER_NOTES = jread(f"{H}/data/board/worker-notes.json", {})
STATE_COLORS = {  # one distinct color per state
    "running": "var(--accent)", "working": "var(--accent)", "validating": "var(--blue)", "parked": "var(--amber)",
    "paused": "var(--orange)", "blocked": "var(--red)", "failed": "var(--red)", "needs-decision": "var(--purple)", "at a gate": "var(--amber)", "finishing pr": "var(--blue)", "finishing PR": "var(--blue)",
    "done": "var(--teal)", "pr open": "var(--teal)", "stopped": "var(--mute)", "unknown": "var(--mute)",
}


MOVING = ("working", "validating", "running", "finishing PR")


def state_key(w):
    st = str(w.get("state") or "unknown").lower()
    if w.get("live"):
        return {"parked": "at a gate", "done": "finishing PR"}.get(st, st)
    prefix, note = last_status(w["task"])
    pr = latest_pr(w["key"])
    if pr and pr["state"] == "merged":
        return "done"
    if prefix in ("blocked", "failed", "needs-decision"):
        return prefix
    if prefix == "paused" and pr and pr["state"] == "open" and "review" in note:
        return "pr open"
    if prefix == "paused":
        return "parked" if ("ruling" in note) else "paused"
    if pr and pr["state"] == "open":
        return "pr open"
    return "stopped"


def state_pill(st, live=False):
    c = STATE_COLORS.get(st, "var(--mute)")
    mv = " moving" if st in MOVING else ""
    return (f'<span class="bpill{mv}" style="background:color-mix(in oklab, {c} 22%, var(--panel2));color:var(--ink);'
            f'box-shadow:inset 0 0 0 1px color-mix(in oklab, {c} 55%, transparent);font-weight:600">{esc(st)}</span>')


def latest_note(w):
    rec = WORKER_NOTES.get(w["task"])
    prefix, note = last_status(w["task"])
    st_at = 0
    lines = [ln for ln in (read(f"{H}/state/{w['task']}.status") or "").splitlines() if ln.strip()]
    if lines:
        mm = re.search(r"\[at=(\d+)\]", lines[-1])
        st_at = int(mm.group(1)) if mm else 0
    if isinstance(rec, dict) and rec.get("note") and int(rec.get("at") or 0) >= st_at - 60:
        return str(rec["note"]), int(rec.get("at") or 0), "firstmate"
    if note:
        text = (prefix + ": " if prefix else "") + note
        return (text if len(text) <= 260 else text[:259].rstrip() + "\u2026"), st_at, "worker"
    return "", 0, ""


def latest_section(w):
    note, at, src = latest_note(w)
    why = "" if w.get("live") else stop_reason(w)
    if not note and not why:
        return ""
    body = ""
    if note:
        stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%d %b %H:%M UTC") if at else ""
        body += f'<div class="lnote">{esc(note)}</div><div class="lmeta">{esc(stamp)}{" · " if stamp else ""}{esc("Supervisor update" if src == "firstmate" else "worker status")}</div>'
    if why:
        body += f'<div class="lwhy"><span>Why stopped</span> {esc(why)}</div>'
    return f'<div class="latest"><div class="lh">Latest</div>{body}</div>'


sys.path.insert(0, f"{H}/data/board")
try:
    import pipeline as _pipe
except Exception:  # noqa: BLE001
    _pipe = None
STEP_COLORS = {"completed": "var(--green)", "running": "var(--blue)", "fixing": "var(--blue)", "awaiting_approval": "var(--amber)",
               "failed": "var(--red)", "skipped": "var(--mute)", "pending": "var(--mute)"}


def _dur(ms):
    s = int(ms or 0) // 1000
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else (f"{s}s" if s else "")


def db_change(task):
    """Alembic migration files this worker added vs origin/dev (committed or not)."""
    wt = meta_field(task, "worktree")
    if not wt or not os.path.isdir(wt):
        return []
    try:
        out = subprocess.run(["git", "-C", wt, "diff", "--name-only", "--diff-filter=A", "origin/dev", "--", "backend/alembic/versions/"],
                             capture_output=True, text=True, timeout=3).stdout.split()
        out += subprocess.run(["git", "-C", wt, "ls-files", "--others", "--exclude-standard", "--", "backend/alembic/versions/"],
                              capture_output=True, text=True, timeout=3).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted({os.path.basename(f) for f in out if f.endswith(".py")})


def db_chip(files, pr_title=""):
    if not files and "[DB]" not in (pr_title or ""):
        return ""
    tip = ", ".join(files) if files else "PR marked [DB]"
    return f'<span class="dbchip" title="Database change: {esc(tip)}">DB change</span>'


_PIPE_STATES = {}


def prefetch_pipelines(ws):
    """Read every live worker's pipeline in parallel (each call is bounded at 1.5s)."""
    from concurrent.futures import ThreadPoolExecutor
    todo = {w["task"]: meta_field(w["task"], "worktree") for w in ws if w.get("live")}
    todo = {k: v for k, v in todo.items() if v}
    if not _pipe or not todo:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
        for k, v in zip(todo, ex.map(lambda wt: _safe_state(wt), todo.values())):
            _PIPE_STATES[k] = v


def _safe_state(wt):
    try:
        return _pipe.pipeline_state(wt)
    except Exception:  # noqa: BLE001
        return None


def pipeline_html(w):
    try:
        return _pipeline_html(w)
    except Exception:  # noqa: BLE001 - never let pipeline text break the board
        return "", ""


def _pipeline_html(w):
    """Card-face progress label + hover panel from the worker's no-mistakes run (None when no run)."""
    if not _pipe or not w.get("live"):
        return "", ""
    st = _PIPE_STATES.get(w["task"])
    if not st:
        return "", ""
    pos, tot, label = _pipe.progress(st)
    a = st.get("active") or {}
    seg = "".join(f'<i class="pseg{" on" if a.get("step") == x["step"] else ""}" style="background:{STEP_COLORS.get(x["status"], "var(--mute)")}" title="{esc(x["step"])}: {esc(x["status"])}"></i>' for x in st["steps"])
    done = [x for x in st["steps"] if x["status"] == "completed" and x["duration_ms"]]
    longest = max(done, key=lambda x: x["duration_ms"]) if done else None
    sub = f'{pos}/{tot} · {esc(label)}{(" · " + esc(a.get("active_for"))) if a.get("active_for") else ""}'
    if longest and longest["duration_ms"] >= 60000:
        sub += f' · {esc(longest["step"])} took {esc(_dur(longest["duration_ms"]))}'
    face_open = '<div class="pprog" tabindex="0" role="button" aria-label="Validation pipeline details">'
    face_body = f'<div class="pstrip">{seg}</div><span class="psub">{sub}</span>'
    chips = ""
    for x in st["steps"]:
        stt = x["status"]
        c = STEP_COLORS.get(stt, "var(--mute)")
        live = " on" if a.get("step") == x["step"] else ""
        extra = f' · {x["findings"]} finding{"s" if x["findings"] != 1 else ""}' if x["findings"] else ""
        chips += (f'<div class="pstep{live}"><span class="pdot" style="background:{c}"></span><span class="pname">{esc(x["step"])}</span>'
                  f'<span class="pst">{esc(stt.replace("_", " "))}{esc(extra)}</span><span class="pdur">{esc(_dur(x["duration_ms"]))}</span><span class="pdb"><i style="width:{round(100 * x["duration_ms"] / max([y["duration_ms"] for y in st["steps"]] + [1]))}%"></i></span></div>')
    rows = []
    if a:
        rows.append(f'<b>Now:</b> {esc(a.get("step"))} - {esc(a.get("status"))}, {esc(a.get("round") or "")} · running {esc(a.get("active_for"))}'
                    + (f' (this round {esc(a.get("round_active_for"))})' if a.get("round_active_for") else ""))
        if a.get("last_activity"):
            rows.append(f'<b>Last activity:</b> {esc(a.get("last_activity"))}')
    if st.get("findings_summary") and st["findings_summary"] not in ("none", "0"):
        rows.append(f'<b>Findings:</b> {esc(st["findings_summary"])}')
    for f in st.get("findings", [])[:3]:
        d = f["description"]
        rows.append(f'<span class="pfind">{esc(f["id"])} · {esc(f["severity"])} · {esc(f["action"])}: {esc(d if len(d) < 140 else d[:139] + "\u2026")}</span>')
    rows.append(f'<b>Pushed:</b> {"yes" if st.get("pushed") else "not yet"}' + (f' · phase {esc(st["phase"].replace("_", " "))}' if st.get("phase") else ""))
    rows.append(f'<span class="mono" style="color:var(--mute)">run {esc(st.get("run", "")[-8:])} · head {esc(st.get("head", ""))}</span>')
    pop = f'<div class="ppop" role="tooltip"><div class="lh">Validation pipeline</div>{chips}<div class="pinfo">{"".join(f"<div>{r}</div>" for r in rows)}</div></div>'
    face = face_open + face_body + pop + '</div>'
    pop = ''
    return face, pop


# ---- "Now" band (deterministic, from the PR ledger, worker state and usage.json)
def _ts(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _age(sec):
    sec = max(0, int(sec))
    return f"{sec // 86400}d {sec % 86400 // 3600}h" if sec >= 86400 else f"{sec // 3600}h {sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m"


def spark(vals, color="var(--blue)", w=120, h=28):
    vals = [float(v or 0) for v in vals]
    if not vals or max(vals) <= 0:
        return f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true"><line x1="0" y1="{h-1}" x2="{w}" y2="{h-1}" stroke="var(--line)"/></svg>'
    m, n = max(vals), len(vals)
    bw = w / n
    bars = "".join(f'<rect x="{i*bw+1:.1f}" y="{h - max(1, v/m*(h-2)):.1f}" width="{max(1, bw-2):.1f}" height="{max(1, v/m*(h-2)):.1f}" rx="1" fill="{color}"><title>{v:g}</title></rect>' for i, v in enumerate(vals))
    return f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">{bars}</svg>'


def now_band():
    now_t = time.time()
    days = [datetime.fromtimestamp(now_t - 86400 * k, timezone.utc).strftime("%Y-%m-%d") for k in range(13, -1, -1)]
    merged_by_day, cycle_by_day = {d: 0 for d in days}, {d: [] for d in days}
    open_prs = []
    for p_ in LEDGER:
        if p_.get("state") == "OPEN" and not p_.get("isDraft"):
            open_prs.append(p_)
        mt, ct = _ts(p_.get("mergedAt")), _ts(p_.get("createdAt"))
        if p_.get("state") == "MERGED" and mt:
            d = datetime.fromtimestamp(mt, timezone.utc).strftime("%Y-%m-%d")
            if d in merged_by_day:
                merged_by_day[d] += 1
                if ct:
                    cycle_by_day[d].append((mt - ct) / 3600)
    today = days[-1]
    week = days[-7:]
    m7 = sum(merged_by_day[d] for d in week)
    cyc7 = sorted(x for d in week for x in cycle_by_day[d])
    med = cyc7[len(cyc7) // 2] if cyc7 else None
    med_series = [sorted(cycle_by_day[d])[len(cycle_by_day[d]) // 2] if cycle_by_day[d] else 0 for d in days]
    decisions = [w for w in workers if state_key(w) in ("needs-decision", "blocked", "failed")]
    oldest = min((_ts(p_.get("createdAt")) or now_t for p_ in open_prs), default=None)
    waiting = len(open_prs) + len(decisions)
    udays = USAGE.get("days") or {}
    tok = lambda d: sum(sum(v.values()) for v in (udays.get(d) or {}).values())
    gauge = "".join(f'<i class="{"on" if k < ACTIVE else ""}"></i>' for k in range(CAPACITY))
    tiles = [
        ("fleet-block", "Workers running", f'{ACTIVE}<small> / {CAPACITY}</small>' if SHOW_CAP else str(ACTIVE), f'<span class="gauge">{gauge}</span>' if SHOW_CAP else "",
         esc(CAPG.get("reason") or "") + ((" · free " + str(CAPG.get("mem_available_mb")) + " MB") if CAPG.get("mem_available_mb") else "") if SHOW_CAP else "live worker agents"),
        ("throughput-block", "Merged today", str(merged_by_day[today]), spark([merged_by_day[d] for d in days], "var(--purple)"), f"{m7} in the last 7 days · 14-day trend"),
        ("throughput-block", "PR open to merge", (f"{med:.1f}<small>h</small>" if med is not None else "—"), spark(med_series, "var(--teal)"), "median, last 7 days"),
        ("usage-block", "Tokens today", fmt_tok(tok(today)), spark([tok(d) for d in days], "var(--blue)"), "14-day trend"),
    ]
    return "".join(f'<a class="stat" href="#{t[0]}"><div class="sl">{esc(t[1])}</div><div class="sv">{t[2]}</div>{t[3]}<div class="sn2">{t[4]}</div></a>' for t in tiles)


def pr_age_html():
    now_t = time.time()
    rows = []
    for p_ in LEDGER:
        if p_.get("state") != "OPEN":
            continue
        ct = _ts(p_.get("createdAt"))
        if not ct:
            continue
        rows.append((now_t - ct, p_))
    if not rows:
        return '<div class="empty">No PRs waiting on a human</div>'
    rows.sort(key=lambda x: -x[0])
    mx = max(r[0] for r in rows) or 1
    out = ""
    for age, p_ in rows:
        hrs = age / 3600
        c = "var(--green)" if hrs < 4 else "var(--amber)" if hrs < 24 else "var(--red)"
        k = pr_key(p_)
        js = JSTAT.get(k, "")
        tl = re.sub(r"^\(" + C.KEY_RE + r"\)\s*", "", p_.get("title") or "")
        out += (f'<div class="agerow"><a href="{esc(p_["url"])}" target="_blank" rel="noopener">#{esc(p_["number"])}</a>'
                f'<span>{(f"<a href=\"{JIRA}{esc(k)}\" target=\"_blank\" rel=\"noopener\">{esc(k)}</a>") if k else "—"}</span>'
                f'<span class="agetitle">{esc(tl)}{" · draft" if p_.get("isDraft") else ""}{(" · Jira " + esc(js)) if js else ""}</span>'
                f'<span class="agebar"><i style="width:{max(3, round(100 * age / mx))}%;background:{c}"></i></span><span class="ageval">{esc(_age(age))}</span></div>')
    return f'<div class="agebox">{out}</div><div class="sn2" style="margin-top:6px">Bar = time since the PR opened · green under 4h, amber under 24h, red after a day · merges are by humans</div>'


def throughput_html():
    now_t = time.time()
    days = [datetime.fromtimestamp(now_t - 86400 * k, timezone.utc).strftime("%Y-%m-%d") for k in range(13, -1, -1)]
    tix, infra, pts = {d: 0 for d in days}, {d: 0 for d in days}, []
    for p_ in LEDGER:
        mt, ct = _ts(p_.get("mergedAt")), _ts(p_.get("createdAt"))
        if p_.get("state") != "MERGED" or not mt:
            continue
        d = datetime.fromtimestamp(mt, timezone.utc).strftime("%Y-%m-%d")
        if d not in tix:
            continue
        (tix if pr_key(p_) else infra)[d] += 1
        if ct:
            pts.append((mt, (mt - ct) / 3600, p_))
    W, Hh = 560, 170
    peak = max([tix[d] + infra[d] for d in days] + [1])
    bw = W / len(days)
    bars = ""
    for i, d in enumerate(days):
        x = i * bw + 3
        y = Hh
        for n, c, lbl in ((tix[d], "var(--purple)", "ticket fixes"), (infra[d], "var(--mute)", "infra / tooling")):
            if n:
                h = n / peak * (Hh - 10)
                y -= h
                bars += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw - 6:.1f}" height="{h:.1f}" rx="2" fill="{c}"><title>{d}: {n} {lbl}</title></rect>'
        if i % 2 == 1 or i == len(days) - 1:
            bars += f'<text x="{x + (bw - 6) / 2:.1f}" y="{Hh + 16}" text-anchor="middle" font-size="12" fill="var(--mute)">{d[8:]}/{d[5:7]}</text>'
    chart_a = f'<svg viewBox="0 0 {W} {Hh + 22}" width="100%" role="img" aria-label="Merged PRs per day" style="display:block">{bars}</svg>'
    # cycle time scatter
    t0 = _ts(days[0] + "T00:00:00Z") or now_t - 14 * 86400
    ymax = max([y for _, y, _ in pts] + [1])
    dots = ""
    for mt, hrs, p_ in pts:
        x = (mt - t0) / (now_t - t0) * (W - 20) + 10
        y = Hh - (hrs / ymax) ** .5 * (Hh - 12) - 2
        dots += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{"var(--teal)" if pr_key(p_) else "var(--mute)"}" opacity=".85"><title>#{p_["number"]} {esc(pr_key(p_) or "infra")}: open {hrs:.1f}h</title></circle>'
    ys = sorted(y for _, y, _ in pts)
    med = ys[len(ys) // 2] if ys else None
    if med is not None:
        my = Hh - (med / ymax) ** .5 * (Hh - 12) - 2
        dots += f'<line x1="0" x2="{W}" y1="{my:.1f}" y2="{my:.1f}" stroke="var(--teal)" stroke-dasharray="4 4"/><text x="40" y="{my - 4:.1f}" font-size="12" fill="var(--teal)">median {med:.1f}h</text>'
    dots += f'<text x="4" y="12" font-size="12" fill="var(--mute)">{ymax:.0f}h</text><text x="4" y="{Hh - 4}" font-size="12" fill="var(--mute)">0h</text>'
    chart_b = f'<svg viewBox="0 0 {W} {Hh + 22}" width="100%" role="img" aria-label="Hours each PR was open before merge" style="display:block">{dots}<text x="{W/2}" y="{Hh + 16}" text-anchor="middle" font-size="12" fill="var(--mute)">merge date, last 14 days (height: square-root scale)</text></svg>'
    return (f'<div class="tpgrid"><div class="ucard"><div class="lh">PRs merged per day</div>{chart_a}<div class="lg2"><span class="lgd"><i style="background:var(--purple)"></i>ticket fixes</span><span class="lgd"><i style="background:var(--mute)"></i>infra / tooling</span></div></div>'
            f'<div class="ucard"><div class="lh">Hours each PR was open before merge</div>{chart_b}<div class="lg2"><span class="lgd"><i style="background:var(--teal)"></i>ticket fix</span><span class="lgd"><i style="background:var(--mute)"></i>infra</span></div></div></div>')


# ---- fleet
def repo_kv(w):
    return f'<span>Repo</span><span class="mono">{esc(w["repo"])}</span>' if len(REPOS) > 1 and w.get("repo") else ""


orch_strip = (f'<div class="orchline"><span class="dot live" style="background:var(--live)"></span><b>{esc(fleet.get("orchestrator_name") or "supervisor")}</b>'
              f'<span class="mute">orchestrator · {esc(fleet.get("orchestrator_model") or "model not reported")} · reports to the captain{f" · {ACTIVE} / {CAPACITY} ship slots" if SHOW_CAP else ""}</span></div>')
fleet_html = ""
stopped_rows = ""
prefetch_pipelines(workers)
_RANK = {"needs-decision": 1, "blocked": 1, "failed": 1, "pr open": 2, "finishing PR": 0}
for w in sorted(workers, key=lambda w: (0 if w["live"] else _RANK.get(state_key(w), 3), w["task"])):
    _pf, _pp = pipeline_html(w)
    pr = latest_pr(w["key"])
    mp = re.search(r"/pull/(\d+)", w.get("meta_pr") or "")
    if mp and (not pr or pr["num"] != mp.group(1)):
        pr = dict(pr or {}, url=w["meta_pr"], num=mp.group(1))
    tk = f'<a href="{JIRA}{esc(w["ticket"])}" target="_blank" rel="noopener">{esc(w["ticket"])}</a>' if w["ticket"] else "—"
    prc = f'<a href="{esc(pr["url"])}" target="_blank" rel="noopener">PR #{esc(pr["num"])}</a>' if pr else "—"
    _sk = state_key(w)
    _db = db_chip(db_change(w["task"]), (pr or {}).get("title", ""))
    if not w["live"] and _sk not in ("needs-decision", "blocked", "failed"):
        note, at, _src = latest_note(w)
        stamp = datetime.fromtimestamp(at, timezone.utc).strftime("%d %b %H:%M") if at else ""
        stopped_rows += (f'<div class="srow"><span class="mono">{esc(w["task"])}</span>{state_pill(_sk)}<span>{tk} {_db}</span><span>{prc}{(" " + pr_pill(pr["state"])) if pr and pr.get("state") else ""}</span>'
                         f'<span class="swhy">{esc(stop_reason(w))}</span><span class="mute">{esc(stamp)}</span></div>')
        continue
    if w["live"]:
        top = (f'<div class="box orch{" moving" if _sk in MOVING else ""}"><div class="btop"><span class="dot live" style="background:var(--live)"></span>'
               f'<span class="bl">{esc(w["kind"])} worker</span>{state_pill(_sk, True)}</div>')
    else:
        top = (f'<div class="box"><div class="btop"><span class="dot hollow"></span><span class="bl">{esc(w["kind"])} worker</span>'
               f'{state_pill(_sk)}</div>')
    fleet_html += (
        top + f'<div class="bname mono">{esc(w["task"])} {_db}</div>{_pf}'
        f'<div class="kv"><span>Model</span><span class="mono">{esc(w["model"])}</span>{repo_kv(w)}<span>Ticket</span>{tk}<span>PR</span><span>{prc}</span></div>{latest_section(w)}{_pp}</div>')
if stopped_rows:
    stopped_html = (f'<details class="stopped" open><summary>Recently stopped workers ({stopped_rows.count("class=\"srow\"")})</summary>'
                    f'<div class="stab">{stopped_rows}</div></details>')
else:
    stopped_html = ""
if not fleet_html:
    fleet_html = '<div class="empty" style="grid-column:1/-1">No workers running right now</div>'

# ---- second mates (main home): one row each from the fleet snapshot's secondmate_current block
BOARDS = FL.parse_boards(C.get("secondmate_boards"))
SM_ROWS, SM_OMITTED = FL.secondmate_rows(snap, BOARDS)
SHOW_SM = MAIN or bool(SM_ROWS)


def secondmate_html():
    if not SM_ROWS:
        return '<div class="empty">No data: the fleet snapshot lists no second mates</div>'
    nodata = '<span class="mute" title="the second mate\'s home could not be read">no data</span>'
    num = lambda v, warn=False: nodata if v is None else (f'<b style="color:var(--amber)">{v}</b>' if warn and v else str(v))
    rows = ""
    for r in SM_ROWS:
        name = esc(r["id"])
        if r["url"]:
            name = f'<a href="{esc(r["url"])}" target="_blank" rel="noopener" title="Open its own board">{name} \u2197</a>'
        fresh = r["freshness"] + (f" \u00b7 {_age(r['age'])}" if r["age"] is not None else "")
        rows += (f'<div class="smrow"><span class="smname">{name}</span><span>{state_pill(r["state"].lower())}</span>'
                 f'<span data-l="Active">{num(r["active"])}</span><span data-l="Queued">{num(r["queued"])}</span>'
                 f'<span data-l="Decisions">{num(r["decisions"], True)}</span><span data-l="Landed">{num(r["landed"])}</span>'
                 f'<span class="mute">{esc(fresh)}{" \u00b7 partial" if r["partial"] else ""}</span></div>')
    more = f'<div class="sn2" style="margin-top:6px">{SM_OMITTED} more second mate{"s" if SM_OMITTED != 1 else ""} not listed in the snapshot</div>' if SM_OMITTED else ""
    return ('<div class="smtab"><div class="smh"><span>Second mate</span><span>State</span><span>Active</span><span>Queued</span><span>Decisions</span>'
            f'<span>Landed</span><span>Data</span></div>{rows}</div>{more}')


# ---- rules
directive = read(f"{H}/data/prioritization.md") or ""
d_bullets = re.findall(r"^-\s+(.+)$", directive, re.M)
d_head = next((ln.strip() for ln in directive.splitlines() if ln.strip() and not ln.startswith("-") and not ln.startswith(("Addition", "Selection"))), "How to choose tickets")
d_adds = re.findall(r"^Addition\s*\(([^)]*)\):\s*(.+)$", directive, re.M)
d_scope = re.search(r"^Selection scope:\s*(.+)$", directive, re.M)
scope_meta = ""
if d_scope:
    sm = d_scope.group(1)
    m1 = re.search(r"Milestone\s*=\s*(\w+(?:\s+or\s+\w+)?)", sm)
    scope_meta = " · ".join(x for x in ["scope: " + m1.group(1) if m1 else "", ("unassigned or captain" if C.get("owner_name") in sm else "unassigned") if "no assignee" in sm else "", "Open" if "Open" in sm else "", "Required = Yes" if "Required = Yes" in sm else ""] if x)
dir_body = ""
if directive.strip():
    dir_body = f'<div><div class="mute" style="margin-bottom:4px">{esc(d_head)}</div><ul class="rl" style="padding-left:18px;gap:0">' + "".join(f"<li>{esc(b)}</li>" for b in d_bullets) + "</ul></div>"
    for who, words in d_adds:
        dir_body += f'<div><div class="mute" style="margin-bottom:4px">Addition ({esc(who)})</div><blockquote>{esc(words)}</blockquote></div>'
    if d_scope:
        dir_body += f'<div class="mute">Selection scope: {esc(d_scope.group(1))}</div>'
else:
    dir_body = '<div class="mute">No directive on file. Ticket selection is idle until the captain provides one.</div>'


def details(title, meta, body, maxw=""):
    return (f'<details class="dt"><summary><span class="mk">▸</span>{esc(title)}<span class="meta">{esc(meta)}</span></summary>'
            f'<div class="dbody"{maxw}>{body}</div></details>')


proto = read(f"{H}/data/pr-protocol.md") or ""
pv = re.search(r"\(v(\d+)[^)]*?(\d{4}-\d{2}-\d{2})", proto)
rv = jread(f"{H}/data/{C.get('rules_dir')}/VERSIONS.json", {})
ann = rv.get("announced") or {}
lag = rv.get("lagging") or []
sync_rows = "".join(
    f'<tr><td>{esc(r.get("label"))}</td><td><code>data/{esc(C.get('rules_dir'))}/{esc(n)}</code></td><td>{esc(r.get("version") or "n/a (no version line)")}</td>'
    f'<td>{esc(r.get("last_fetched") or "never")}</td><td>{esc(ann.get("version") or "-")}</td>'
    f'<td>{esc(r.get("format") or "-")}{(" · %s B markdown from %s B raw HTML kept as %s.html" % (r.get("markdown_bytes"), r.get("html_bytes"), esc(n[:-3]))) if r.get("html_bytes") else ""}</td>'
    f'<td>{esc(r.get("status") or "not synced yet")}</td></tr>' for n, r in sorted((rv.get("documents") or {}).items()))
sync_body = (f'<div class="mute">Last run {esc(rv.get("last_run") or "never")} · status <strong style="color:var(--ink)">{esc(rv.get("last_status") or "never run")}</strong>'
             f' · announced version in the team channel: <strong style="color:var(--ink)">{esc(ann.get("version") or "none seen yet")}</strong>'
             + (f' · <span class="prs" style="color:var(--red);background:color-mix(in oklab, var(--red) 20%, transparent)">LAGGING: {esc("; ".join(lag))}</span>' if lag else "") + "</div>"
             '<div class="tw"><table class="rt"><tr><th>Document</th><th>File</th><th>Version on disk</th><th>Last fetched from Slack</th><th>Announced</th><th>Conversion</th><th>Sync status</th></tr>'
             + sync_rows + "</table></div>" + md_lite(read(f"{H}/data/{C.get('rules_sync_dir')}/README.md") or ""))
pr_link_doc = ("Every ticket card, fleet box and log row shows the full PR link beside the Jira link once a PR exists or the ticket reaches PR Submitted, with the PR state "
               "(open, merged, closed or submitted) taken from this team's own records, and it stays after the ticket moves on (Merged, Ready for UAT, ...) so history stays traceable.")
memory_doc = ("The cap is %d concurrent ship workers while memory is measured. Every worker closes its browser session and dev servers when it finishes, "
              "and ops/orphan_check.py (add --kill to reap) runs before every worker start." % CAPACITY)
rule_panes = [
    ("directive", "Prioritization directive", scope_meta, dir_body, ""),
    ("pr-protocol", "PR evidence protocol", ("v%s · %s" % (pv.group(1), pv.group(2))) if pv else "", md_lite(proto), ' style="max-width:820px"'),
    ("completeness", "Completeness rule", "team rule", md_lite(read(f"{H}/data/completeness-rule.md") or "No completeness rule on file."), ""),
    ("jira-style", "Jira comment style", "team rule", md_lite(read(f"{H}/data/jira-comment-style.md") or "No style rule on file."), ""),
    ("slot-policy", "Slot policy", "team rule", md_lite(read(f"{H}/data/slot-policy.md") or "No slot policy on file."), ""),
    ("memory", "Browser and memory", "team rule", f'<p style="margin:0">{esc(memory_doc)}</p>', ""),
    ("pr-links", "PR links", "how the board shows PRs", f'<p style="margin:0">{esc(pr_link_doc)}</p>', ""),
    ("rules-sync", "Rules sync", "rules %s · announced %s" % (rv.get("last_status") or "never run", ann.get("version") or "none"), sync_body, ""),
]
if MAIN:  # only panes whose source file exists; the two static explainers describe the team-home ops scripts
    _src = {"directive": ["prioritization.md"], "pr-protocol": ["pr-protocol.md"], "completeness": ["completeness-rule.md"],
            "jira-style": ["jira-comment-style.md"], "slot-policy": ["slot-policy.md"],
            "rules-sync": [f"{C.get('rules_dir')}/VERSIONS.json", f"{C.get('rules_sync_dir')}/README.md"]}
    rule_panes = [p_ for p_ in rule_panes if any(os.path.exists(f"{H}/data/{f}") for f in _src.get(p_[0], []))]
SHOW_RULES = bool(rule_panes)
rules_nav = "".join(f'<button type="button" class="rn" data-rule="{rid}"{" aria-current=\"true\"" if n == 0 else ""}>{esc(title)}</button>'
                    for n, (rid, title, _m, _b, _w) in enumerate(rule_panes))
rules_body = "".join(f'<div class="rp" id="rule-{rid}"{"" if n == 0 else " hidden"}><div class="rph"><h3>{esc(title)}</h3><span class="meta">{esc(meta)}</span></div>'
                     f'<div class="dbody"{maxw}>{body}</div></div>' for n, (rid, title, meta, body, maxw) in enumerate(rule_panes))
rules_html = f'<div class="rules"><nav class="rnav" aria-label="Rules">{rules_nav}</nav><div class="rpanes">{rules_body}</div></div>'

# ---- usage section
def usage_html():
    days = USAGE.get("days") or {}
    provs = USAGE.get("providers") or {}
    measured = [p for p, v in provs.items() if v.get("measured")]
    if not days or not measured:
        return '<div class="mute">No usage collected yet.</div>'
    colors = {"Anthropic": "var(--purple)", "Z.AI": "var(--live)"}
    series = sorted({p for d in days.values() for p in d if sum(d[p].values()) > 0})
    keys = sorted(days)[-14:]
    tot = lambda d, p=None: sum(sum(v.values()) for q, v in d.items() if p is None or q == p)
    peak = max((tot(days[k]) for k in keys), default=1) or 1
    W, Hh, gap = 1000, 160, 10
    bw = min(64, (W - gap * (len(keys) - 1)) / max(len(keys), 1))
    bars = ""
    for i, k in enumerate(keys):
        x, y = i * (bw + gap), Hh
        for p in series:
            v = sum((days[k].get(p) or {}).values())
            if not v: continue
            h = v / peak * (Hh - 4)
            y -= h
            bars += f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="3" fill="{colors.get(p, "var(--blue)")}"><title>{esc(k)} · {esc(p)}: {fmt_tok(v)}</title></rect>'
        bars += f'<text x="{x + bw/2:.1f}" y="{Hh + 14}" text-anchor="middle" font-size="13" fill="var(--mute)">{esc(k[5:])}</text>'
    svg = f'<svg viewBox="0 -4 {W} {Hh + 22}" width="100%" role="img" aria-label="Tokens per day by provider" style="display:block;min-height:120px">{bars}</svg>'
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    td = days.get(today, {})
    grand = sum(tot(d) for d in days.values())
    legend = "".join(f'<span class="lgd"><i style="background:{colors.get(p, "var(--blue)")}"></i>{esc(p)} · {fmt_tok(sum(tot(d, p) for d in days.values()))} total · today {fmt_tok(tot(td, p))}</span>' for p in series)
    unmeasured = "".join(f'<div class="mute" style="font-size:12px">{esc(p)}: not measured ({esc(v.get("note") or "no data")})</div>' for p, v in provs.items() if not v.get("measured"))
    split = " · ".join(f"{k.replace('_', ' ')} {fmt_tok(sum((v.get(k) or 0) for d in td.values() for v in [d]))}" for k in ("input", "output", "cache_read", "cache_write"))
    tix = sorted(((k, v) for k, v in (USAGE.get("tickets") or {}).items() if v.get("tokens")), key=lambda kv: -kv[1]["tokens"])[:10]
    tmax = max([v["tokens"] for _, v in tix] + [1])
    trows = ""
    for task, v in tix:
        m = re.search(C.KEY.lower() + r"-(\d+)", task)
        key = f"{C.KEY}-{m.group(1)}" if m else ""
        segs = "".join(f'<i style="width:{100 * n / tmax:.1f}%;background:{colors.get(pv, "var(--blue)")}" title="{esc(pv)}: {fmt_tok(n)}"></i>' for pv, n in sorted((v.get("by_provider") or {}).items()))
        lbl = f'<a href="{JIRA}{esc(key)}" target="_blank" rel="noopener">{esc(key)}</a>' if key else esc(task)
        trows += f'<div class="trow"><span class="mono">{lbl}</span><span class="tsub">{esc(task)}</span><span class="tbar">{segs}</span><span class="tval">{fmt_tok(v["tokens"])}</span></div>'
    wk = [d for d in sorted(days)[-7:]]
    merged7 = sum(1 for _p in LEDGER if _p.get("state") == "MERGED" and (_ts(_p.get("mergedAt")) or 0) >= time.time() - 7 * 86400 and pr_key(_p))
    per = fmt_tok(sum(tot(days[d]) for d in wk) / merged7) if merged7 else "—"
    ticket_html = (f'<div class="ucard"><div class="lh">Tokens per ticket (top 10, worker sessions)</div>{trows or "<div class=mute>No attributable ticket sessions yet.</div>"}'
                   f'<div class="sn2">About {per} tokens per merged ticket over the last 7 days (all usage ÷ {merged7} merged ticket PRs).</div></div>')
    return (f'<div class="ucard">{svg}<div class="lg2">{legend}</div>'
            f'<details class="showmore"><summary>How this is measured</summary><div class="mute" style="font-size:12px;padding:0 18px 12px">Today by kind: {esc(split)}. Tokens include cache reads and writes. '
            f'Sources: Claude Code transcripts (message-deduplicated) and Pi session logs; collected {esc(when(USAGE.get("generated")) or "never")}. Per-ticket totals count worker sessions only, matched exactly by working folder and spawn time; '
            f'tickets worked before tracking began show "not attributable". No costs are shown - there is no pricing source.</div>{unmeasured}</details></div>{ticket_html}')

usage_block = usage_html()

# ---- logs
handled = ""
for e in sorted(tickets, key=lambda x: str(x.get("at", "")), reverse=True)[:60]:
    k, u = e.get("ticket") or "", e.get("pr") or ""
    pr_html = ""
    if str(u).startswith("https://"):
        n = re.search(r"/pull/(\d+)", u)
        merged = "merged" in str(e.get("event")).lower()
        pr_html = (f'<span><a href="{esc(u)}" target="_blank" rel="noopener">PR #{esc(n.group(1) if n else "")}</a>'
                   + (' <span class="mg">merged</span>' if merged else "") + "</span>")
    handled += (f'<div class="lrow"><span class="ts">{esc(when(e.get("at")))}</span><div style="display:flex;flex-direction:column;gap:3px;min-width:0">'
                f'<div style="display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px"><a class="mono" style="font-size:12px;font-weight:600" href="{JIRA}{esc(k)}" target="_blank" rel="noopener">{esc(k)}</a>'
                f'<span style="text-wrap:pretty">{esc(e.get("event"))}</span></div><div style="display:flex;flex-wrap:wrap;gap:6px 10px;font-size:12px;color:var(--mute)">{pr_html}<span>{esc(e.get("note") or "")}</span></div></div></div>')
if not handled:
    handled = '<div class="empty" style="border:0">No tickets picked yet</div>'
qev = ""
for e in sorted(events, key=lambda x: str(x.get("at", "")), reverse=True)[:60]:
    ks = e.get("keys") or e.get("removed") or ([e["key"]] if e.get("key") else []) or (e.get("after") or [])[:6]
    note = e.get("note") or ""
    if e.get("dir"):
        note = (note + " " if note else "") + "moved " + str(e["dir"])
    if e.get("source"):
        note = (note + " " if note else "") + "· source " + str(e["source"])
    chips = "".join(f'<span class="kc">{esc(k)}</span>' for k in ks)
    qev += (f'<div class="lrow"><span class="ts">{esc(when(e.get("at")))}</span><div style="display:flex;flex-direction:column;gap:4px;min-width:0">'
            f'<div style="display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 8px"><span class="mono" style="font-size:12px;font-weight:600">{esc(e.get("event"))}</span>'
            f'<span style="font-size:12px;color:var(--mute)">by {esc(e.get("actor"))}</span></div>'
            + (f'<div style="display:flex;flex-wrap:wrap;gap:4px">{chips}</div>' if chips else "")
            + f'<div style="font-size:12.5px;color:var(--mute);text-wrap:pretty">{esc(note)}</div></div></div>')
if not qev:
    qev = '<div class="empty" style="border:0">No reorders yet</div>'


def cap_rows(html_rows, n=10):
    rows = [r for r in re.split(r'(?=<div class="lrow">)', html_rows) if r]
    if len(rows) <= n:
        return html_rows
    return "".join(rows[:n]) + f'<details class="showmore"><summary>Show {len(rows) - n} more</summary>{"".join(rows[n:])}</details>'


SHOW_TICKETS = not MAIN or os.path.exists(f"{H}/data/board/tickets.jsonl")
SHOW_EVENTS = not MAIN or os.path.exists(f"{H}/data/board/events.jsonl")
handled = cap_rows(handled)
qev = cap_rows(qev)

removed_html = ""
if removed:
    removed_html = ('<details class="dt" style="margin-top:2px"><summary><span class="mk">▸</span>Removed from the queue (%d)<span class="meta">restore puts a ticket back into Ranked next</span></summary><div class="dbody">'
                    % len(removed) + "".join(
                        f'<div style="display:flex;gap:10px;align-items:center"><a class="key" href="{JIRA}{esc(i["key"])}" target="_blank" rel="noopener">{esc(i["key"])}</a>'
                        f'<span class="fsum">{esc(i.get("summary"))}</span><button class="rs" data-act="restore" data-key="{esc(i["key"])}">restore</button></div>' for i in removed)
                    + "</div></details>")

problem = ""
if FAILED:
    names = ", ".join(sorted(set(FAILED)))
    tail = " The Backlog lane below is shown as empty, not confirmed empty." if "backlog" in FAILED else ""
    problem = ('<div class="warn"><span class="dot" style="background:var(--amber);flex:none"></span><span><strong style="font-weight:600">Source read problem:</strong> '
               f'{esc(names)} could not be read.{tail}</span></div>')
TMPS = jread(f"{H}/data/board/tmp.json", {})
if TMPS.get("warn"):
    problem += ('<div class="warn"><span class="dot" style="background:var(--amber);flex:none"></span><span><strong style="font-weight:600">Server scratch space is filling:</strong> '
                f'/tmp is {esc(TMPS.get("pct"))}% full (of {esc(TMPS.get("total_mb"))} MB, held in memory). Idle worker leftovers are cleared automatically before each worker start; the rest belongs to running work.</span></div>')
stale_state = ""
if workers and all(w["state"] == "unknown" for w in workers):
    stale_state = "Worker state reads <em>unknown</em> for every task: the state feed is not wired, not a fleet fault."
over = '<div style="font-size:12px;color:var(--amber)">more workers listed than slots</div>' if ACTIVE > CAPACITY else ""

CSS = r"""
:root{--bg:#0b0b0c;--panel:#141415;--panel2:#1e1e20;--line:#26262a;--ink:#f4f4f2;--mute:#9a9a9f;--green:#c8f542;--blue:#8b8bff;--red:#ff8a80;--amber:#f2c94c;--purple:#c9a8ff;--teal:#5eead4;--orange:#fdba74;--accent:#c8f542;--accent-ink:#0b0b0c;--live:#c8f542;color-scheme:dark}
@media (prefers-color-scheme: light){:root{--bg:#ededec;--panel:#ffffff;--panel2:#f2f2f0;--line:#dcdcd8;--ink:#141414;--mute:#6b6b70;--green:#4d7c0f;--blue:#4f46e5;--red:#c62828;--amber:#9a6700;--purple:#7c3aed;--teal:#0f766e;--orange:#c2410c;--accent:#c8f542;--accent-ink:#141414;--live:#4d7c0f;color-scheme:light}}
html[data-theme="dark"]{--bg:#0b0b0c;--panel:#141415;--panel2:#1e1e20;--line:#26262a;--ink:#f4f4f2;--mute:#9a9a9f;--green:#c8f542;--blue:#8b8bff;--red:#ff8a80;--amber:#f2c94c;--purple:#c9a8ff;--teal:#5eead4;--orange:#fdba74;--accent:#c8f542;--accent-ink:#0b0b0c;--live:#c8f542;color-scheme:dark}
html[data-theme="light"]{--bg:#ededec;--panel:#ffffff;--panel2:#f2f2f0;--line:#dcdcd8;--ink:#141414;--mute:#6b6b70;--green:#4d7c0f;--blue:#4f46e5;--red:#c62828;--amber:#9a6700;--purple:#7c3aed;--teal:#0f766e;--orange:#c2410c;--accent:#c8f542;--accent-ink:#141414;--live:#4d7c0f;color-scheme:light}
html,body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 "Manrope",system-ui,-apple-system,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
summary{cursor:pointer;list-style:none}summary::-webkit-details-marker{display:none}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}code{font-size:.92em}
.page{max-width:1480px;margin:0 auto;padding:28px 28px 48px;display:flex;flex-direction:column;gap:32px}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px 32px}
.hl{display:flex;flex-direction:column;gap:6px}
.eyebrow{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--mute)}.eyebrow i{width:18px;height:18px;border-radius:6px;background:var(--accent)}
h1{margin:0;font-size:26px;font-weight:600;letter-spacing:-.02em}
.hm{display:flex;flex-wrap:wrap;gap:6px 18px;color:var(--mute);font-size:13px}
.hm .signout{color:var(--mute)}.hm .signout:hover{color:var(--ink)}
.hm .ts2{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.th{display:inline-flex;gap:8px;font-size:12px}.th button{background:none;border:0;padding:0;color:var(--mute);font:inherit;font-size:12px;cursor:pointer}.th button:hover,.th button.on{color:var(--ink)}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}
.stat{display:flex;flex-direction:column;gap:6px;padding:16px 18px;border:1px solid var(--line);border-radius:16px;background:var(--panel);color:inherit;text-decoration:none;min-width:0}.stat:hover{border-color:var(--mute)}.stat .sn2{font-size:12px;color:var(--mute)}.spark{display:block;max-width:100%}.gauge{display:flex;gap:4px}.gauge i{flex:1;height:8px;border-radius:3px;background:var(--panel2)}.gauge i.on{background:var(--live)}
@media (max-width:1100px){.stats{grid-template-columns:repeat(3,minmax(0,1fr))}}@media (max-width:760px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}.stat .sv{font-size:28px!important}}
.stat .sl{font-size:13px;color:var(--mute)}.stat .sv{font-size:34px;line-height:1;font-weight:500;letter-spacing:-.03em;font-variant-numeric:tabular-nums}.stat .sv small{color:var(--mute);font-weight:400;font-size:34px}
.warn{display:flex;gap:12px;align-items:center;padding:12px 18px;border:1px solid color-mix(in oklab, var(--amber) 35%, transparent);border-radius:999px;background:color-mix(in oklab, var(--amber) 10%, transparent);font-size:13px}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none}.dot.hollow{border:1px solid var(--mute);box-sizing:border-box}
section{display:flex;flex-direction:column;gap:12px}
.sh{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px 24px}
h2{margin:0;font-size:18px;font-weight:600;letter-spacing:-.01em;color:var(--ink)}
.sn{font-size:12px;color:var(--mute)}
.fleet{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.box{display:flex;flex-direction:column;gap:12px;padding:16px 18px;border:1px solid var(--line);border-radius:16px;background:var(--panel);min-width:0}
.box.orch{border-color:color-mix(in oklab, var(--accent) 55%, var(--line))}
.btop{display:flex;align-items:center;gap:8px}.bl{font-size:12px;color:var(--mute)}
.latest{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:4px;font-size:12.5px}.latest .lh{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute)}.lnote{text-wrap:pretty}.lmeta{font-size:11px;color:var(--mute)}.lwhy{color:var(--mute)}.lwhy span{color:var(--ink);font-weight:600}
@keyframes pelpulse{0%{box-shadow:0 0 0 0 color-mix(in oklab, var(--live) 70%, transparent)}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
@keyframes pelsheen{0%{background-position:-120% 0}100%{background-position:220% 0}}
@keyframes pelflow{0%{background-position:0 0}100%{background-position:0 -200%}}
.dot.live{animation:pelpulse 1.8s ease-out infinite}
.bpill.moving{background-image:linear-gradient(100deg,transparent 30%,color-mix(in oklab,var(--ink) 18%,transparent) 50%,transparent 70%)!important;background-size:200% 100%!important;background-repeat:no-repeat!important;animation:pelsheen 2.4s linear infinite}
.box.moving{position:relative}.box.moving::before,.card.moving::before{content:"";position:absolute;left:0;top:10px;bottom:10px;width:3px;border-radius:3px;background:linear-gradient(180deg,var(--live),transparent,var(--live));background-size:100% 200%;animation:pelflow 2.2s linear infinite}
.card.moving{position:relative}
.liveind{display:inline-flex;align-items:center;gap:6px}.liveind i{width:7px;height:7px;border-radius:50%;background:var(--live);animation:pelpulse 1.8s ease-out infinite}
@media (prefers-reduced-motion: reduce){.pstep.on .pdot,.dot.live,.bpill.moving,.box.moving::before,.card.moving::before,.liveind i{animation:none}}
.pprog{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--mute);margin:2px 0 8px}.pbar{flex:0 0 54px;height:5px;border-radius:3px;background:var(--panel2);overflow:hidden}.pbar i{display:block;height:100%;background:var(--blue)}
.box{position:relative}.ppop{display:none;position:absolute;z-index:20;left:0;top:100%!important;min-width:min(320px,calc(100vw - 32px));max-width:calc(100vw - 32px);top:calc(100% - 6px);background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;box-shadow:0 12px 32px rgba(0,0,0,.28);font-size:12px}
.pprog:hover .ppop,.pprog:focus-within .ppop,.pprog:focus .ppop{display:block}@media (hover:none){.pprog:hover .ppop{display:none}.pprog:focus .ppop,.pprog:focus-within .ppop{display:block}}.ppop .lh{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--mute);margin-bottom:6px}
.pstep{display:grid;grid-template-columns:10px 70px 1fr auto;gap:8px;align-items:center;padding:3px 4px;border-radius:6px}.pstep.on{background:var(--panel2)}.pdot{width:8px;height:8px;border-radius:50%}.pstep.on .pdot{animation:pelpulse 1.8s ease-out infinite}.pname{font-weight:600}.pst{color:var(--mute)}.pdur{color:var(--mute);font-variant-numeric:tabular-nums}
.pinfo{margin-top:8px;display:flex;flex-direction:column;gap:3px}.pinfo b{font-weight:600}.pfind{color:var(--mute)}
.pstrip{display:grid;grid-template-columns:repeat(9,1fr);gap:3px;margin-bottom:4px}.pseg{display:block;height:6px;border-radius:2px}.pseg.on{outline:2px solid var(--ink);outline-offset:1px}
.pprog{display:block;position:relative;margin:2px 0 8px;font-size:12px;color:var(--mute);cursor:pointer}.psub{display:block}
.pstep{grid-template-columns:10px 70px 1fr auto 60px!important}.pdb{display:block;height:4px;border-radius:2px;background:var(--panel2);overflow:hidden}.pdb i{display:block;height:100%;background:var(--blue)}
.orchline{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:13px;margin-bottom:12px}.orchline .mute{color:var(--mute)}
.stopped{margin-top:14px}.stopped summary{cursor:pointer;color:var(--mute);font-size:13px;margin-bottom:8px}
.stab{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.srow{display:grid;grid-template-columns:150px 110px 90px 150px minmax(0,1fr) 90px;gap:12px;align-items:center;padding:9px 14px;border-bottom:1px solid var(--line);font-size:12.5px}.srow:last-child{border-bottom:0}.srow .agebox{background:var(--panel);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.agerow{display:grid;grid-template-columns:60px 90px minmax(0,1.2fr) minmax(120px,1fr) 70px;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);font-size:13px}.agerow:last-child{border-bottom:0}
.agetitle{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--mute)}.agebar{height:8px;border-radius:4px;background:var(--panel2);overflow:hidden}.agebar i{display:block;height:100%}.ageval{text-align:right;font-variant-numeric:tabular-nums}
@media (max-width:760px){.agerow{grid-template-columns:52px 80px 1fr 60px}.agetitle{display:none}}
.secnav{position:sticky;top:0;z-index:30;display:flex;gap:4px;overflow-x:auto;padding:8px 0;background:color-mix(in oklab,var(--bg) 92%,transparent);backdrop-filter:blur(6px);border-bottom:1px solid var(--line);scrollbar-width:none}.secnav a{flex:none;padding:5px 11px;border-radius:999px;color:var(--mute);text-decoration:none;font-size:13px}.secnav a:hover{background:var(--panel2);color:var(--ink)}
section{scroll-margin-top:56px}#stats{scroll-margin-top:56px}.showmore summary{cursor:pointer;color:var(--mute);font-size:12.5px;padding:10px 18px}
.qview{display:inline-flex;gap:2px;padding:3px;border:1px solid var(--line);border-radius:999px;background:var(--panel);margin-bottom:12px}.qview button{all:unset;cursor:pointer;padding:5px 12px;border-radius:999px;font-size:13px;color:var(--mute)}.qview button[aria-pressed=true]{background:var(--panel2);color:var(--ink)}
.fctl button{min-width:36px;min-height:36px}
.trow{display:grid;grid-template-columns:90px 150px minmax(0,1fr) 70px;gap:12px;align-items:center;padding:6px 0;font-size:13px}.tsub{color:var(--mute);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.tbar{display:flex;height:10px;border-radius:5px;background:var(--panel2);overflow:hidden}.tbar i{display:block;height:100%}.tval{text-align:right;font-variant-numeric:tabular-nums}
@media (max-width:760px){.trow{grid-template-columns:80px 1fr 60px}.tsub{display:none}}
.dbchip{display:inline-block;font:600 11px/1.6 system-ui,sans-serif;padding:0 7px;border-radius:999px;background:#f59e0b22;color:#b45309;border:1px solid #f59e0b88;vertical-align:middle;margin-left:6px}
.smtab{border:1px solid var(--line);border-radius:16px;background:var(--panel);overflow:hidden}
.smh,.smrow{display:grid;grid-template-columns:minmax(0,1.4fr) 110px repeat(4,72px) minmax(120px,1fr);gap:12px;padding:12px 18px;align-items:center;font-size:13px}
.smh{padding:10px 18px;font-size:12px;color:var(--mute);background:var(--panel2);border-bottom:1px solid var(--line)}.smrow{border-bottom:1px solid var(--line)}.smrow:last-child{border-bottom:0}
.smname{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}.smrow .bpill{margin-left:0}
@media (max-width:760px){.smh{display:none}.smrow{grid-template-columns:repeat(4,minmax(0,1fr))}.smrow .smname,.smrow .mute:last-child{grid-column:1/-1}.smrow>span[data-l]::before{content:attr(data-l) " ";display:block;font-size:11px;color:var(--mute)}}
.tpgrid{display:grid;grid-template-columns:1fr 1fr;gap:14px}@media (max-width:900px){.tpgrid{grid-template-columns:1fr}}
.bpill{margin-left:0;justify-self:start}.swhy{color:var(--mute)}
@media (max-width:760px){.srow{grid-template-columns:1fr auto;gap:4px 10px}.srow .swhy{grid-column:1/-1}}
.bpill{margin-left:auto;font-size:11px;padding:2px 9px;border-radius:999px;background:var(--panel2);color:var(--mute)}
.bname{font-size:16px;font-weight:700;letter-spacing:-.01em}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 12px;font-size:12.5px}.kv>span:nth-child(odd){color:var(--mute)}
.kv .mono{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lanes{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px;align-items:start}
.lane{display:flex;flex-direction:column;gap:8px;min-width:0}
.lhead{display:flex;flex-direction:column;gap:2px;padding:4px 6px 8px;height:48px;box-sizing:border-box}
.l1{display:flex;align-items:center;gap:10px;min-width:0;height:22px}
.lt{font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cnt{font-size:11px;font-weight:600;min-width:20px;text-align:center;padding:1px 6px;border-radius:999px;background:var(--panel2);color:var(--ink);font-variant-numeric:tabular-nums;flex:none}
.lh{padding-left:18px;font-size:11px;color:var(--mute);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.empty{padding:18px 12px;border:1px dashed var(--line);border-radius:16px;color:var(--mute);font-size:13px;text-align:center}
.card{display:flex;flex-direction:column;gap:8px;padding:16px 16px 12px;border:1px solid var(--line);border-radius:16px;background:var(--panel);min-width:0}
.card[draggable=true]{cursor:grab}.card.dragover{border-color:var(--accent)}
.top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.rank{font-size:11px;font-weight:700;min-width:22px;height:22px;display:grid;place-items:center;border-radius:50%;font-variant-numeric:tabular-nums}
.key{font-size:14px;font-weight:700;letter-spacing:-.01em;color:var(--ink)}
.pill{font-size:11px;padding:2px 9px;border-radius:999px;background:var(--panel2);color:var(--mute)}
.prio{margin-left:auto;font-size:11px;color:var(--mute)}
.sum{font-size:14px;line-height:1.45;text-wrap:pretty}
.prrow{display:flex;align-items:center;gap:8px;font-size:12px}
.prs{font-size:11px;font-weight:600;padding:2px 9px;border-radius:999px}
.foot{display:flex;align-items:center;gap:6px;border-top:1px solid var(--line);padding-top:8px;margin-top:2px}
.tg{background:none;border:0;padding:0;color:var(--mute);font:inherit;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:6px}.tg:hover{color:var(--ink)}.chev{display:inline-block;width:10px}
.ctl{margin-left:auto;display:flex;gap:4px}.fctl{display:flex;justify-content:flex-end}.fctl .ctl{margin-left:0}
.ib{width:26px;height:26px;display:grid;place-items:center;background:var(--panel2);border:1px solid var(--line);border-radius:50%;color:var(--mute);font-size:12px;padding:0;cursor:pointer;font-family:inherit}
.ib:hover{background:var(--accent);color:var(--accent-ink);border-color:var(--accent)}.ib.x{font-size:11px}.ib.x:hover{background:var(--panel2);color:var(--red);border-color:var(--red)}
.detail{display:flex;flex-direction:column;gap:10px;font-size:12.5px;line-height:1.5;color:var(--ink)}.detail[hidden]{display:none}
.riskrow{display:grid;grid-template-columns:auto 1fr;gap:8px;color:var(--mute)}.riskrow>span:first-child{font-size:11px;letter-spacing:.06em;text-transform:uppercase;padding-top:2px}
.more{display:flex;justify-content:center;padding:10px;border:1px dashed var(--line);border-radius:999px;font-size:12.5px;color:var(--mute)}.more:hover{color:var(--ink);border-color:var(--mute);text-decoration:none}
.dt{border:1px solid var(--line);border-radius:16px;background:var(--panel)}
.dt>summary{display:flex;align-items:center;gap:10px;padding:14px 18px;font-weight:600;font-size:14px}
.dt>summary .mk{color:var(--mute)}.dt[open]>summary .mk{display:inline-block;transform:rotate(90deg)}
.pill.ms{background:color-mix(in oklab, var(--accent) 22%, var(--panel2));color:var(--ink);font-weight:600}.fms{font-size:12px;font-weight:600}
.tok{font-size:12px;color:var(--mute)}.ftok{font-size:12px;color:var(--mute)}
.ucard{margin-top:14px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px;display:flex;flex-direction:column;gap:12px}
.lg2{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:12.5px}.lgd{display:inline-flex;align-items:center;gap:6px}.lgd i{width:10px;height:10px;border-radius:3px;display:inline-block}
.rules{display:grid;grid-template-columns:220px 1fr;gap:20px;align-items:start}
.rnav{position:sticky;top:16px;display:flex;flex-direction:column;gap:4px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:8px}
.rn{all:unset;cursor:pointer;padding:9px 12px;border-radius:9px;color:var(--mute);font-weight:500}.rn:hover{color:var(--ink);background:var(--panel2)}.rn[aria-current=true]{background:var(--panel2);color:var(--ink);box-shadow:inset 3px 0 0 var(--accent)}.rn:focus-visible{outline:2px solid var(--accent)}
.rpanes{min-width:0;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px 20px}
.rph{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:10px}.rph h3{margin:0;font-size:16px}.rph .meta{font-size:12px;color:var(--mute)}
@media (max-width:760px){.rules{grid-template-columns:1fr}.rnav{position:static;flex-direction:row;overflow-x:auto;padding:6px}.rn{white-space:nowrap}.rn[aria-current=true]{box-shadow:inset 0 -3px 0 var(--accent)}}
.dt .meta{margin-left:auto;font-weight:400;font-size:12px;color:var(--mute)}
.dbody{padding:0 14px 14px 34px;display:flex;flex-direction:column;gap:12px;font-size:13px;line-height:1.5}
.mute{color:var(--mute)}blockquote{margin:0;padding-left:12px;border-left:2px solid var(--line)}
.rl{margin:0;padding-left:20px;display:flex;flex-direction:column;gap:4px}
.tw{overflow-x:auto}.rt{border-collapse:collapse;font-size:12.5px;width:100%}.rt th,.rt td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}.rt th{color:var(--mute);font-weight:500}
.logs{display:grid;grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:24px;align-items:start}
.lg{display:flex;flex-direction:column;gap:12px;min-width:0}
.lbox{border:1px solid var(--line);border-radius:16px;background:var(--panel);overflow:hidden}
.lrow{display:grid;grid-template-columns:118px 1fr;gap:4px 14px;padding:12px 18px;border-bottom:1px solid var(--line);font-size:13px}.lrow:last-child{border-bottom:0}
.ts{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--mute);padding-top:2px}
.mg{font-size:11px;padding:0 6px;border-radius:999px;background:color-mix(in oklab, var(--purple) 18%, transparent);color:var(--purple)}
.kc{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;padding:1px 6px;border-radius:4px;background:var(--panel2);color:var(--mute)}
.ftab{border:1px solid var(--line);border-radius:16px;background:var(--panel);overflow:hidden}
.fh,.frow{display:grid;grid-template-columns:40px 96px 44px minmax(0,1fr) 150px 90px 80px 130px;gap:14px;padding:12px 18px;font-size:13px;align-items:center}
.fh{padding:10px 18px;font-size:12px;color:var(--mute);background:var(--panel2);border-bottom:1px solid var(--line)}.fh span:last-child{text-align:right}
.frow{border-bottom:1px solid var(--line)}.frow:last-child{border-bottom:0}.frow.dragover{outline:1px solid var(--accent)}
.rk{font-size:12px;color:var(--mute);font-variant-numeric:tabular-nums}.fsum{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fstate{display:inline-flex;align-items:center;gap:8px;font-size:12px}
.rs{background:var(--panel2);border:1px solid var(--line);border-radius:999px;color:var(--mute);font:inherit;font-size:12px;padding:2px 10px;cursor:pointer}.rs:hover{color:var(--ink)}
@media (max-width:900px){.fh{display:none}.frow{grid-template-columns:40px 1fr;gap:4px 12px}.frow .fsum{grid-column:1/-1;white-space:normal}.frow .fctl{grid-column:1/-1;justify-content:flex-start}.logs{grid-template-columns:1fr}}
"""

JS = r"""
(function(){
 var csrf=null,openSet={},dragKey=null;
 function qs(s,r){return (r||document).querySelector(s);}
 function msg(t,err){var m=qs('#qmsg');if(m){m.textContent=t;m.style.color=err?'var(--red)':'var(--ink)';}}
 function applyOpen(){document.querySelectorAll('.card').forEach(function(c){var k=c.dataset.key,d=qs('.detail',c),v=qs('.chev',c);if(!d)return;var o=!!openSet[k];d.hidden=!o;if(v)v.textContent=o?'▾':'▸';});}
 function getCsrf(){return fetch('/api/queue',{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.json();}).then(function(d){csrf=d.csrf||null;return csrf;});}
 function post(path,body,retry){
  var go=function(){return fetch('/api/'+path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRF':csrf||''},body:JSON.stringify(body)});};
  return (csrf?Promise.resolve():getCsrf()).then(go).then(function(r){
   if(r.status===403&&!retry){csrf=null;return getCsrf().then(function(){return post(path,body,true);});}
   return r.json().then(function(j){if(!r.ok)throw new Error(j.error||r.status);return j;});});
 }
 var curRule='directive';try{curRule=localStorage.getItem('board-rule')||curRule;}catch(e){}
 function showRule(id){var b=document.querySelector('.rn[data-rule="'+id+'"]');if(!b){b=document.querySelector('.rn');if(!b)return;id=b.dataset.rule;}curRule=id;
  document.querySelectorAll('.rn').forEach(function(x){x.setAttribute('aria-current',x===b?'true':'false');});
  document.querySelectorAll('.rp').forEach(function(p){p.hidden=(p.id!=='rule-'+id);});try{localStorage.setItem('board-rule',id);}catch(e){}}
 document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('.rn');if(b)showRule(b.dataset.rule);});
 showRule(curRule);
 var qv='board';try{qv=localStorage.getItem('board-qview')||'board';}catch(e){}
 function setQ(v){qv=v;document.querySelectorAll('.qpane').forEach(function(p){p.hidden=(p.dataset.pane!==v);});document.querySelectorAll('[data-qview]').forEach(function(b){b.setAttribute('aria-pressed',b.dataset.qview===v?'true':'false');});try{localStorage.setItem('board-qview',v);}catch(e){}}
 document.addEventListener('click',function(e){var b=e.target.closest&&e.target.closest('[data-qview]');if(b){setQ(b.dataset.qview);return;}var m=e.target.closest&&e.target.closest('.morelink');if(m){e.preventDefault();setQ('table');document.getElementById('queue-block').scrollIntoView();}});
 setQ(qv);
 var SWAP=['refreshed','stats','fleet-block','secondmates-block','throughput-block','queue-block','usage-block','rules-block','logs-block'];
 function swap(){
  return fetch(location.pathname+'?_='+Date.now(),{credentials:'same-origin',cache:'no-store'}).then(function(r){return r.text();}).then(function(t){
   var doc=new DOMParser().parseFromString(t,'text/html');
   SWAP.forEach(function(id){var n=doc.getElementById(id),o=document.getElementById(id);if(n&&o){
     if(id==='rules-block'){o.innerHTML=n.innerHTML;showRule(curRule);}else if(id==='queue-block'){o.innerHTML=n.innerHTML;setQ(qv);}
     else o.innerHTML=n.innerHTML;}});
   applyOpen();
  });
 }
 function act(b){
  var a=b.dataset.act,k=b.dataset.key;
  if(a==='remove'&&!confirm('Remove '+k+' from the queue?'))return;
  var body={key:k};if(a==='move')body.dir=b.dataset.dir;
  post(a,body).then(function(){msg(a==='move'?'order saved · '+k+' moved '+b.dataset.dir:a==='remove'?k+' removed':k+' restored');return swap();}).catch(function(e){msg('error: '+e.message,true);});
 }
 function saveOrder(newRanked){
  var ranked=(window.__ranked||[]),all=(window.__all||[]).slice(),slots=[];
  all.forEach(function(k,i){if(ranked.indexOf(k)>=0)slots.push(i);});
  newRanked.forEach(function(k,j){all[slots[j]]=k;});
  post('order',{keys:all}).then(function(){msg('order saved · dragged');return swap();}).catch(function(e){msg('error: '+e.message,true);});
 }
 document.addEventListener('click',function(e){
  var t=e.target.closest('[data-toggle]');if(t){var k=t.dataset.toggle;openSet[k]=!openSet[k];applyOpen();return;}
  var a=e.target.closest('[data-act]');if(a){act(a);return;}
  var th=e.target.closest('[data-theme-set]');if(th){setTheme(th.dataset.themeSet);}
 });
 document.addEventListener('dragstart',function(e){var c=e.target.closest&&e.target.closest('.card[draggable=true]');if(c){dragKey=c.dataset.key;e.dataTransfer.effectAllowed='move';}});
 document.addEventListener('dragover',function(e){var c=e.target.closest&&e.target.closest('.card[draggable=true]');if(c&&dragKey){e.preventDefault();c.classList.add('dragover');}});
 document.addEventListener('dragleave',function(e){var c=e.target.closest&&e.target.closest('.card');if(c)c.classList.remove('dragover');});
 document.addEventListener('drop',function(e){var c=e.target.closest&&e.target.closest('.card[draggable=true]');if(!c||!dragKey)return;e.preventDefault();c.classList.remove('dragover');
  var ranked=(window.__ranked||[]).slice(),from=ranked.indexOf(dragKey),to=ranked.indexOf(c.dataset.key);if(from<0||to<0||from===to){dragKey=null;return;}
  ranked.splice(from,1);ranked.splice(to,0,dragKey);dragKey=null;saveOrder(ranked);});
 function setTheme(v){try{localStorage.setItem('board-theme',v);}catch(x){}
  if(v==='auto')document.documentElement.removeAttribute('data-theme');else document.documentElement.setAttribute('data-theme',v);
  document.querySelectorAll('[data-theme-set]').forEach(function(b){b.classList.toggle('on',b.dataset.themeSet===v);});}
 var saved='auto';try{saved=localStorage.getItem('board-theme')||'auto';}catch(x){}setTheme(saved);
 getCsrf().catch(function(){});
 setInterval(function(){if(!document.hidden)swap().catch(function(){});},15000);
 function ago(){var r=document.getElementById('refreshed');if(!r)return;var a=r.querySelector('.ago'),t=+r.dataset.at;if(!a||!t)return;var d=Math.max(0,Math.round(Date.now()/1000-t));a.textContent='('+(d<60?d+'s':Math.round(d/60)+'m')+' ago)';}
 setInterval(ago,1000);ago();
})();
"""

repo_links = " \u00b7 ".join(f'<a href="https://github.com/{r_}" target="_blank" rel="noopener">{r_}</a>' for r_ in REPOS)
repo_links = ("repos " if len(REPOS) > 1 else "repo ") + repo_links
signout = "" if MAIN else '<a class="signout" href="/api/signout">sign out</a>'
nav_items = [("stats", "Now", True), ("fleet-block", "Fleet", True), ("secondmates-block", "Second mates", SHOW_SM), ("throughput-block", "Throughput", True),
             ("queue-block", "Queue", SHOW_QUEUE), ("usage-block", "Usage", True),
             ("logs-block", "History", not MAIN or SHOW_TICKETS or SHOW_EVENTS), ("rules-block", "Rules", SHOW_RULES)]
nav_html = '<nav class="secnav" aria-label="Sections">' + "".join(f'<a href="#{i_}">{n_}</a>' for i_, n_, on_ in nav_items if on_) + "</nav>"
secondmates_section = (f'<section id="secondmates-block"><div class="sh"><h2>Second mates</h2><div class="sn">from the fleet snapshot'
                       f'{" \u00b7 a name links to that second mate\'s own board" if BOARDS else ""}</div></div>{secondmate_html()}</section>') if SHOW_SM else ""
queue_section = f'''<section id="queue-block"><div class="sh"><h2>Queue</h2><div class="sn">Top of <em>Ranked next</em> is picked when a slot frees \u00b7 order saved <span class="mono">{esc(order_saved)}</span> \u00b7 <span id="qmsg" style="color:var(--ink)">changes save immediately</span></div></div>
<div class="qview" role="tablist"><button type="button" data-qview="board" aria-pressed="true">Board</button><button type="button" data-qview="table" aria-pressed="false">Table \u00b7 {len(full_rows)}</button></div>
<div class="lanes qpane" data-pane="board">{''.join(lane_html)}</div><div class="qpane" data-pane="table" hidden><div class="ftab"><div class="fh"><span>#</span><span>Ticket</span><span>MS</span><span>Summary</span><span>State</span><span>PR</span><span>Tokens</span><span>Reorder</span></div>{''.join(full_rows)}</div>{removed_html}</div>
<script>window.__ranked={json.dumps(ranked_keys)};window.__all={json.dumps(all_keys)};</script></section>''' if SHOW_QUEUE else ""
logs_section = ('<section id="logs-block"><div class="logs">'
                + (f'<div class="lg"><h2>Tickets handled by this team</h2><div class="lbox">{handled}</div></div>' if SHOW_TICKETS else "")
                + (f'<div class="lg"><h2>Queue events</h2><div class="lbox">{qev}</div></div>' if SHOW_EVENTS else "")
                + "</div></section>") if (SHOW_TICKETS or SHOW_EVENTS) else ""
rules_section = f'<section id="rules-block"><h2>Rules</h2>{rules_html}</section>' if SHOW_RULES else ""

page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<noscript><meta http-equiv="refresh" content="300"></noscript>
<title>{esc(C.get('title'))}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
<script>try{{var t=localStorage.getItem('board-theme');if(t&&t!=='auto')document.documentElement.setAttribute('data-theme',t);}}catch(e){{}}</script>
<style>{CSS}</style></head><body>
<div class="page">
<header><div class="hl"><div class="eyebrow"><i></i>{esc(C.get('title'))}</div><h1>{esc(C.get('subtitle'))}</h1>
<div class="hm"><span>{repo_links}</span><span>merges are human-only</span>
<span id="refreshed" class="liveind" data-at="{int(now.timestamp())}"><i></i>live · refreshed <span class="ts2">{now.strftime('%H:%M:%S')} UTC</span> <span class="ago"></span></span>
<span class="th">theme <button data-theme-set="auto">auto</button><button data-theme-set="dark">dark</button><button data-theme-set="light">light</button></span>{signout}</div></div>
<div class="stats" id="stats">{now_band()}</div></header>
{problem}
{nav_html}
<section id="fleet-block"><div class="sh"><h2>Fleet</h2><div class="sn">{stale_state}</div></div>{orch_strip}<div class="fleet">{fleet_html}</div>{stopped_html}</section>
{secondmates_section}
<section id="throughput-block"><div class="sh"><h2>Throughput</h2><div class="sn">from GitHub, last 14 days</div></div>{throughput_html()}</section>
{queue_section}
<section id="usage-block"><div class="sh"><h2>Usage</h2><div class="sn">tokens per day, stacked by provider</div></div>{usage_block}</section>
{logs_section}
{rules_section}
</div><script>{JS}</script></body></html>"""

tmp = OUT + ".tmp"
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(tmp, "w", encoding="utf-8") as f:
    f.write(page)
os.replace(tmp, OUT)
