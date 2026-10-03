"""Pure helpers for the board generator: live-worker detection, task repos and the secondmate rows.

Kept free of side effects at import time so tests can exercise them directly.
"""
import os
import re
import subprocess

# Command names a worker agent shows up as. `node` is how an agent launched through a node wrapper
# appears; it only counts when its working folder is a recorded worktree.
WORKER_COMMANDS = ("claude", "pi", "node")
LSOF_CMD = ["lsof", "-a", "-d", "cwd", "-Fpcn"]


def parse_lsof(text):
    """Parse `lsof -Fpcn` field output into [(command, cwd)], one entry per cwd record."""
    out, pid, cmd = [], None, ""
    for line in (text or "").splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            pid, cmd = val, ""
        elif tag == "c":
            cmd = val
        elif tag == "n" and pid is not None:
            out.append((cmd, val))
    return out


def _proc_agents(proc_root):
    for d in os.listdir(proc_root):
        if not d.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, d, "comm")) as f:
                comm = f.read().strip()
            yield comm, os.readlink(os.path.join(proc_root, d, "cwd"))
        except OSError:
            continue


def _lsof_agents(runner):
    p = runner(LSOF_CMD, capture_output=True, text=True, timeout=20)
    if not p.stdout.strip() and p.returncode != 0 and p.stderr.strip():
        raise OSError("lsof failed: " + p.stderr.strip().splitlines()[0])
    return parse_lsof(p.stdout)


def live_worktrees(worktrees, proc_root="/proc", runner=subprocess.run):
    """Recorded worktrees that have a running worker agent in them.

    Reads /proc where it exists (Linux) and falls back to lsof elsewhere (macOS). A process counts
    only when its command is a worker name and its working folder is one of `worktrees`; the
    recorded spelling is returned so callers can test membership directly.
    """
    by_real = {os.path.realpath(w): w for w in worktrees if w}
    if not by_real:
        return set()
    agents = _proc_agents(proc_root) if os.path.isdir(proc_root) else _lsof_agents(runner)
    live = set()
    for comm, cwd in agents:
        if comm.lower() not in WORKER_COMMANDS:
            continue
        rec = by_real.get(os.path.realpath(cwd))
        if rec:
            live.add(rec)
    return live


_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/\d+")


def task_repo(task, meta_repo="", known=()):
    """owner/name a task belongs to, from its own record; "" when the record does not say.

    Order: the snapshot record's `repo`, the task meta `repo=`, a `project` that names one of the
    configured repos, then the repository in its recorded PR URL.
    """
    task = task if isinstance(task, dict) else {}
    for cand in (task.get("repo"), meta_repo):
        cand = re.sub(r"^(https://github\.com/)", "", str(cand or "").strip()).rstrip("/")
        if _REPO_RE.fullmatch(cand):
            return cand
    project = str(task.get("project") or "").strip().lower()
    if project:
        for r in known:
            if project in (r.lower(), r.split("/", 1)[-1].lower()):
                return r
    pr = task.get("pr") if isinstance(task.get("pr"), dict) else {}
    m = _PR_URL_RE.match(str(pr.get("url") or ""))
    return m.group(1) if m else ""


def parse_boards(value):
    """`id=url,id=url` -> {lower-case id: url}; only http(s) URLs are kept."""
    out = {}
    for part in str(value or "").split(","):
        k, _, v = part.partition("=")
        k, v = k.strip().lower(), v.strip()
        if k and re.match(r"https?://", v, re.I):
            out[k] = v
    return out


def _count(rec, key):
    counts = rec.get("counts") if isinstance(rec.get("counts"), dict) else {}
    if isinstance(counts.get(key), int) and not isinstance(counts.get(key), bool):
        return counts[key]
    if isinstance(rec.get(key), list):
        return len(rec[key])
    return None


def secondmate_rows(snap, boards=None):
    """One row per registered second mate from the snapshot's `secondmate_current` block.

    Counts are None ("no data") for a second mate whose home could not be read, and the whole list
    is empty when the block is missing or has no records. Also returns how many records the
    snapshot left out.
    """
    boards = boards or {}
    cur = snap.get("secondmate_current") if isinstance(snap, dict) else None
    records = cur.get("records") if isinstance(cur, dict) else None
    rows = []
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        rid = str(rec["id"])
        prov = rec.get("provenance") if isinstance(rec.get("provenance"), dict) else {}
        fresh = rec.get("freshness") if isinstance(rec.get("freshness"), dict) else {}
        readable = prov.get("selected") == "structured-home"
        age = fresh.get("age_seconds")
        rows.append({
            "id": rid,
            "state": str((rec.get("current") or {}).get("state") or "unknown"),
            "url": boards.get(rid.lower(), ""),
            "readable": readable,
            "partial": readable and prov.get("trust") == "partial-structured",
            "active": _count(rec, "active_children") if readable else None,
            "queued": _count(rec, "queued") if readable else None,
            "decisions": _count(rec, "decisions_open") if readable else None,
            "landed": _count(rec, "landed") if readable else None,
            "freshness": str(fresh.get("status") or "unknown"),
            "age": age if isinstance(age, int) and not isinstance(age, bool) else None,
        })
    omitted = cur.get("truncated") if isinstance(cur, dict) and isinstance(cur.get("truncated"), int) else 0
    return rows, max(0, omitted)
