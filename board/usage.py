#!/usr/bin/env python3
"""Token usage collector for the board - deterministic, no model calls.

Reads the same local logs open-usage reads, plus Pi's (open-usage has no Pi/Z.AI
provider and no machine-readable output, so it stays the captain's terminal view):
  Anthropic  ~/.claude/projects/**/*.jsonl   message.usage, de-duplicated by message.id
  Z.AI       ~/.pi/agent/sessions/*/*.jsonl  assistant message usage (provider from model_change)
Writes data/board/usage.json:
  days    {YYYY-MM-DD (UTC): {provider: {input, output, cache_read, cache_write}}}
  tickets {task-id: {tokens, sessions, by_provider}}   worker sessions only
  providers {name: {measured, source, note}}
Per-ticket attribution is exact or absent: a session belongs to a task only when its
working folder equals the task's recorded worktree and it started after that task's
recorded spawn and before the next spawn recorded in the same folder. Spawns are
learned from state/*.meta (spawn_gen carries the spawn epoch) and kept in
data/board/task-slots.json, so tasks torn down before the first collection are simply
not attributable.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import config as C
import glob, json, os, re, time
from datetime import datetime, timezone

H = C.FM_HOME
D = f"{H}/data/board"
OUT, SLOTS = f"{D}/usage.json", f"{D}/task-slots.json"
HOME = os.path.expanduser("~")
KINDS = ("input", "output", "cache_read", "cache_write")


def iso_epoch(s):
    try: return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError): return None


def jread(p, d):
    try:
        with open(p) as f: return json.load(f)
    except (OSError, ValueError): return d


def learn_slots():
    slots = jread(SLOTS, {})
    for m in glob.glob(f"{H}/state/*.meta"):
        kv = {}
        try:
            for line in open(m):
                k, _, v = line.rstrip("\n").partition("=")
                kv[k] = v
        except OSError: continue
        tid, wt, sg = os.path.basename(m)[:-5], kv.get("worktree"), kv.get("spawn_gen", "")
        mm = re.match(r"s(\d+)", sg)
        if not wt or not mm: continue
        slots[f"{tid}@{mm.group(1)}"] = {"task": tid, "worktree": os.path.realpath(wt), "spawn": int(mm.group(1))}
    tmp = SLOTS + ".tmp"
    with open(tmp, "w") as f: json.dump(slots, f, indent=1)
    os.replace(tmp, SLOTS)
    return list(slots.values())


def owner(slots, cwd, start):
    if not cwd or start is None: return None
    cwd = os.path.realpath(cwd)
    best = None
    for s in slots:
        if s["worktree"] == cwd and s["spawn"] <= start + 5 and (best is None or s["spawn"] > best["spawn"]):
            best = s
    return best["task"] if best else None


def add(days, day, prov, u):
    d = days.setdefault(day, {}).setdefault(prov, dict.fromkeys(KINDS, 0))
    for k in KINDS: d[k] += int(u.get(k) or 0)


def collect():
    slots = learn_slots()
    days, tickets, providers = {}, {}, {}

    def credit(task, prov, u):
        if not task: return
        t = tickets.setdefault(task, {"tokens": 0, "sessions": 0, "by_provider": {}})
        n = sum(int(u.get(k) or 0) for k in KINDS)
        t["tokens"] += n
        t["by_provider"][prov] = t["by_provider"].get(prov, 0) + n

    # Anthropic (Claude Code, including no-mistakes pipeline agents and Claude workers)
    files = glob.glob(f"{HOME}/.claude/projects/*/*.jsonl") + glob.glob(f"{HOME}/.claude/projects/*/*/*.jsonl")
    seen = set()
    for fp in files:
        task, first = None, True
        try: fh = open(fp, errors="replace")
        except OSError: continue
        with fh:
            for line in fh:
                if '"usage"' not in line and not first: continue
                try: r = json.loads(line)
                except ValueError: continue
                if first and r.get("cwd") and r.get("timestamp"):
                    task, first = owner(slots, r["cwd"], iso_epoch(r["timestamp"])), False
                    if task: tickets.setdefault(task, {"tokens": 0, "sessions": 0, "by_provider": {}})["sessions"] += 1
                msg = r.get("message") if isinstance(r.get("message"), dict) else {}
                us, mid, ts = msg.get("usage"), msg.get("id"), r.get("timestamp")
                if not us or not ts or (mid and mid in seen): continue
                if mid: seen.add(mid)
                u = {"input": us.get("input_tokens"), "output": us.get("output_tokens"),
                     "cache_read": us.get("cache_read_input_tokens"), "cache_write": us.get("cache_creation_input_tokens")}
                add(days, ts[:10], "Anthropic", u); credit(task, "Anthropic", u)
    providers["Anthropic"] = {"measured": bool(files), "source": "Claude Code transcripts (~/.claude/projects)",
                              "note": "" if files else "no Claude Code logs found"}

    # Pi (Z.AI GLM and any other Pi provider)
    pfiles = glob.glob(f"{HOME}/.pi/agent/sessions/*/*.jsonl")
    for fp in pfiles:
        prov, task = "Pi (unknown provider)", None
        try: fh = open(fp, errors="replace")
        except OSError: continue
        with fh:
            for line in fh:
                try: r = json.loads(line)
                except ValueError: continue
                t = r.get("type")
                if t == "session":
                    task = owner(slots, r.get("cwd"), iso_epoch(r.get("timestamp", "")))
                    if task: tickets.setdefault(task, {"tokens": 0, "sessions": 0, "by_provider": {}})["sessions"] += 1
                elif t == "model_change":
                    p = (r.get("provider") or "").lower()
                    prov = {"zai": "Z.AI"}.get(p, f"Pi ({p})" if p else prov)
                elif t == "message" and (r.get("message") or {}).get("role") == "assistant":
                    us = r.get("usage") or (r.get("message") or {}).get("usage")
                    if not us or not r.get("timestamp"): continue
                    u = {"input": us.get("input"), "output": us.get("output"),
                         "cache_read": us.get("cacheRead"), "cache_write": us.get("cacheWrite")}
                    add(days, r["timestamp"][:10], prov, u); credit(task, prov, u)
    providers["Z.AI"] = {"measured": bool(pfiles), "source": "Pi session logs (~/.pi/agent/sessions)",
                         "note": "" if pfiles else "no Pi logs found"}

    # Present on the box but not measurable here: say so rather than show zero.
    if os.path.exists(f"{HOME}/.local/share/opencode/opencode.db"):
        providers.setdefault("OpenCode", {"measured": False, "source": "opencode.db",
                                          "note": "installed but not used by this team; not counted"})
    if os.path.isdir(f"{HOME}/.codex"):
        providers.setdefault("Codex", {"measured": False, "source": "~/.codex",
                                       "note": "no session logs on this server"})

    out = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "providers": providers,
           "days": dict(sorted(days.items())), "tickets": tickets, "slot_ledger_since": min((s["spawn"] for s in slots), default=None)}
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f: json.dump(out, f, indent=1)
    os.replace(tmp, OUT)
    return out


if __name__ == "__main__":
    t0 = time.time(); o = collect()
    last = list(o["days"].items())[-3:]
    print(f"usage: {len(o['days'])} days, {len(o['tickets'])} attributable tasks, {time.time() - t0:.1f}s")
    for d, v in last: print(d, {p: sum(x.values()) for p, x in v.items()})
