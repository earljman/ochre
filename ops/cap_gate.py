#!/usr/bin/env python3
"""Effective ship-slot cap with automatic memory fallback - deterministic, no model calls.

Policy: run up to the configured number of workers, dropping back automatically under memory pressure.
Configured cap: data/board/fleet.json ship_slots (3). While effective cap is 3 and
MemAvailable < 1 GB, drop to 2 (no new third worker; a running third finishes).
While dropped, restore to 3 only once MemAvailable > 1.5 GB (hysteresis).
The orphan check (data/ops/orphan-check.py --kill) runs first on every evaluation.
State: data/board/cap.json {configured, effective, reason, since, mem_available_mb}.
Each drop/restore appends one line to state/parent-replies.status and data/ops/cap-log.jsonl.
Run before every ship spawn and on every board refresh. Prints the effective cap.

Policy: under low memory or storage, pause a worker on non-focus work first.
When live ship workers exceed the effective cap, or /tmp is at least TMP_DROP_PCT full, one
running worker whose ticket milestone (data/board/queue.json) is outside the focus milestones (config focus_milestones) is paused with
bin/fm-control.sh exit (its branch and copy are kept) and the pause is logged. Focus-milestone workers
are never paused by this gate.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "board"))
import config as C
import json, os, subprocess, sys, time
from datetime import datetime, timezone

H = C.FM_HOME
CAP, LOG, FLEET = f"{H}/data/board/cap.json", f"{H}/data/ops/cap-log.jsonl", f"{H}/data/board/fleet.json"
DROP_MB, RESTORE_MB = 1024, 1536
TMP_DROP_PCT = 85


def mem_available_mb():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def jread(p, d):
    try:
        with open(p) as f: return json.load(f)
    except (OSError, ValueError): return d


def live_ship_workers():
    out = []
    import glob, re as _re
    pis = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                if open(f"/proc/{d}/comm").read().strip() == "pi":
                    pis[os.path.realpath(os.readlink(f"/proc/{d}/cwd"))] = d
            except OSError:
                pass
    for m in glob.glob(f"{H}/state/*.meta"):
        kv = {}
        for line in open(m):
            k, _, v = line.rstrip("\n").partition("=")
            kv[k] = v
        if kv.get("kind") == "ship" and os.path.realpath(kv.get("worktree", "-")) in pis:
            out.append(os.path.basename(m)[:-5])
    return out


def milestone_of(task):
    keys = {i.get("key", "").lower(): i.get("milestone") for i in jread(f"{H}/data/board/queue.json", {}).get("items", [])}
    import re as _re
    m = _re.search(C.KEY.lower() + r"-(\d+)", task)
    return keys.get(f"{C.KEY.lower()}-{m.group(1)}") if m else None


def shed_m3plus(eff, why):
    live = live_ship_workers()
    tmp_pct = jread(f"{H}/data/board/tmp.json", {}).get("pct", 0)
    if len(live) <= eff and tmp_pct < TMP_DROP_PCT:
        return
    reason = why if len(live) > eff else f"/tmp {tmp_pct}% full"
    victims = [t for t in live if milestone_of(t) and milestone_of(t) not in C.FOCUS]  # unknown milestone is never shed
    if not victims:
        return
    v = victims[-1]
    r = subprocess.run([f"{H}/bin/fm-control.sh", v, "exit"], env=dict(os.environ, FM_HOME=H), capture_output=True, text=True, timeout=60)
    now = int(time.time())
    ok = r.returncode == 0
    with open(f"{H}/state/{v}.status", "a") as f:
        if ok:
            f.write(f"paused [at={now}]: paused automatically by the resource gate ({reason}); non-focus work yields to focus milestones - branch and copy kept, resume by relaunch\n")
    with open(f"{H}/state/parent-replies.status", "a") as f:
        f.write(f"working [key=ship-cap] [at={now}]: resource gate {'paused' if ok else 'could not pause'} non-focus worker {v} ({reason}); focus-milestone workers untouched\n")
    with open(LOG, "a") as f:
        f.write(json.dumps({"at": now, "event": "shed", "task": v, "ok": ok, "reason": reason}) + "\n")


def main():
    if "--no-orphan-check" not in sys.argv:
        subprocess.run([sys.executable, f"{H}/data/ops/orphan-check.py", "--kill"], capture_output=True, timeout=60, check=False)
    configured = int(jread(FLEET, {}).get("ship_slots") or 2)
    prev = jread(CAP, {})
    eff = int(prev.get("effective") or configured)
    eff = min(eff, configured)
    mem = mem_available_mb()
    reason = prev.get("reason") or "configured cap"
    changed = None
    if configured >= 3 and eff >= 3 and mem < DROP_MB:
        eff, reason, changed = 2, f"dropped to 2: MemAvailable {mem} MB < {DROP_MB} MB", "drop"
    elif configured >= 3 and eff < 3 and mem > RESTORE_MB:
        eff, reason, changed = configured, f"restored to {configured}: MemAvailable {mem} MB > {RESTORE_MB} MB", "restore"
    elif eff < configured and configured < 3:
        eff = configured
    if not prev:
        reason = f"configured cap {configured}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {"configured": configured, "effective": eff, "reason": reason,
           "since": now if changed or not prev else prev.get("since", now), "mem_available_mb": mem, "checked": now}
    tmp = CAP + ".tmp"
    with open(tmp, "w") as f: json.dump(out, f, indent=1)
    os.replace(tmp, CAP)
    if changed:
        with open(LOG, "a") as f: f.write(json.dumps({"at": now, "event": changed, "effective": eff, "mem_available_mb": mem}) + "\n")
        with open(f"{H}/state/parent-replies.status", "a") as f:
            f.write(f"working [key=ship-cap] [at={int(time.time())}]: ship-slot cap {reason}\n")
    shed_m3plus(eff, reason)
    print(eff)


if __name__ == "__main__":
    main()
