#!/usr/bin/env python3
"""Find (and with --kill reap) leaked browser and dev-server process trees.

Deterministic, stdlib only. Run before starting any worker, and after a worker
finishes: leaked Chrome, chrome-devtools-mcp, Vite and
Uvicorn processes from finished workers and pipeline test steps filled memory and
swap.

A process tree is an ORPHAN when its working directory is
  - a treehouse worktree slot in which no live `pi` agent is running, or
  - a no-mistakes pipeline worktree and it is older than STALE_PIPELINE_S
    (the pipeline test agent's own budget is 30 minutes).
Trees under a slot with a live agent are never touched: that worker is
responsible for closing its own browser when it finishes (standing rule).

It also checks /tmp, a 3.8 GB RAM-backed tmpfs (workers' leftover Python
venvs and puppeteer Chrome profiles filled it to 80%). A top-level /tmp entry is a
LEFTOVER when it is a venv (contains pyvenv.cfg) or a puppeteer_dev_chrome_profile-*
directory AND no process references it (cmdline, cwd, environment, open files) AND it
has not been modified for TMP_IDLE_S AND its name does not carry the ticket number of a
task that still has state/<task>.meta. Leftovers are removed only with --kill.
/tmp usage is written to data/board/tmp.json for the board (warn above TMP_WARN_PCT).

Usage: orphan_check.py [--kill]     (default: report only, exit 0)
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "board"))
import config as C
import json, os, re, shutil, signal, sys, time

STALE_PIPELINE_S = 40 * 60
TMP_IDLE_S = 2 * 3600
TMP_WARN_PCT = 70
H = C.FM_HOME
TARGET = re.compile(r"vite/bin/vite\.js|uvicorn app\.main|chrome-devtools-mcp|chrome-devtools-axi|/opt/google/chrome/chrome|\bchrome\b.*--user-data-dir")
SLOT = re.compile(r"/\.treehouse/[^/]+/(\d+)/")
PIPE = re.compile(r"/\.no-mistakes/worktrees/")
ME = os.getpid()


def procs():
    out = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
            start = int(stat.rsplit(")", 1)[1].split()[19])
            cmd = open(f"/proc/{pid}/cmdline").read().replace("\0", " ").strip()
            cwd = os.readlink(f"/proc/{pid}/cwd")
            rss = 0
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) // 1024
            out[pid] = dict(pid=pid, ppid=ppid, cmd=cmd, cwd=cwd, start=start, rss=rss)
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
            continue
    return out


def newest_mtime(path):
    newest = 0
    for root, dirs, files in os.walk(path):
        for n in [root] + [os.path.join(root, f) for f in files]:
            try:
                newest = max(newest, os.lstat(n).st_mtime)
            except OSError:
                pass
    return newest


def tmp_check(kill):
    refs = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        bits = []
        for name in ("cmdline", "environ"):
            try:
                bits.append(open(f"/proc/{d}/{name}", "rb").read().decode("utf-8", "replace"))
            except OSError:
                pass
        try:
            bits.append(os.readlink(f"/proc/{d}/cwd"))
        except OSError:
            pass
        try:
            for fd in os.listdir(f"/proc/{d}/fd"):
                try:
                    bits.append(os.readlink(f"/proc/{d}/fd/{fd}"))
                except OSError:
                    pass
        except OSError:
            pass
        refs.append("\n".join(bits))
    blob = "\n".join(refs)
    live_nums = set()
    for m in os.listdir(f"{H}/state"):
        if m.endswith(".meta"):
            live_nums.update(re.findall(r"\d{3,5}", m))
    now = time.time()
    left, freed = [], 0
    for name in sorted(os.listdir("/tmp")):
        path = f"/tmp/{name}"
        if not os.path.isdir(path) or os.path.islink(path):
            continue
        kind = "browser profile" if name.startswith("puppeteer_dev_chrome_profile-") else "python venv" if os.path.exists(f"{path}/pyvenv.cfg") else None
        if not kind or path in blob:
            continue
        if set(re.findall(r"\d{3,5}", name)) & live_nums:
            continue
        if now - newest_mtime(path) < TMP_IDLE_S:
            continue
        size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs if not os.path.islink(os.path.join(r, f))) // 2**20
        left.append((path, kind, size))
    print(f"/tmp leftovers: {len(left)} using about {sum(x[2] for x in left)} MB")
    for path, kind, size in left:
        print(f"  {size:>6}MB {kind}: {path}")
        if kill:
            shutil.rmtree(path, ignore_errors=True)
            freed += size
    if kill and left:
        print(f"removed /tmp leftovers, about {freed} MB")
    st = os.statvfs("/tmp")
    total = st.f_blocks * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    pct = round(100 * used / total) if total else 0
    print(f"/tmp {pct}% used of {total // 2**20} MB")
    try:
        with open(f"{H}/data/board/tmp.json", "w") as f:
            json.dump({"pct": pct, "total_mb": total // 2**20, "warn": pct >= TMP_WARN_PCT, "checked": int(now)}, f)
    except OSError:
        pass


def main():
    kill = "--kill" in sys.argv
    hz = os.sysconf("SC_CLK_TCK")
    boot = 0
    for line in open("/proc/stat"):
        if line.startswith("btime"):
            boot = int(line.split()[1])
    P = procs()
    now = time.time()
    live_slots = set()
    for p in P.values():
        if re.fullmatch(r"pi( .*)?", p["cmd"]) or p["cmd"].split(" ")[0].endswith("/pi"):
            m = SLOT.search(p["cwd"] + "/")
            if m:
                live_slots.add(m.group(1))
    orphans = {}
    for p in P.values():
        if p["pid"] == ME or not TARGET.search(p["cmd"]):
            continue
        age = now - (boot + p["start"] / hz)
        m = SLOT.search(p["cwd"] + "/")
        if m and m.group(1) not in live_slots:
            orphans[p["pid"]] = (f"slot {m.group(1)} has no live agent", age)
        elif PIPE.search(p["cwd"]) and age > STALE_PIPELINE_S:
            orphans[p["pid"]] = (f"pipeline stack older than {STALE_PIPELINE_S // 60} min", age)
    # Chrome processes are children of the mcp/bridge; add every descendant of an orphan root.
    kids = {}
    for p in P.values():
        kids.setdefault(p["ppid"], []).append(p["pid"])

    def desc(pid):
        for c in kids.get(pid, []):
            yield c
            yield from desc(c)

    allp = dict(orphans)
    for pid in list(orphans):
        for c in desc(pid):
            allp.setdefault(c, (orphans[pid][0], orphans[pid][1]))
    total = sum(P[p]["rss"] for p in allp if p in P)
    print(f"live agent slots: {sorted(live_slots) or 'none'}")
    print(f"orphan processes: {len(allp)} using about {total} MB")
    for pid, (why, age) in sorted(allp.items()):
        if pid in P:
            print(f"  {pid:>8} {int(age):>6}s {P[pid]['rss']:>5}MB {why}: {P[pid]['cmd'][:70]}")
    with open("/proc/meminfo") as f:
        mem = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in f}
    print(f"memory available {mem.get('MemAvailable')} MB, swap used {mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)} MB, chrome processes {sum(1 for p in P.values() if p['cmd'].startswith('/opt/google/chrome/chrome'))}")
    if kill and allp:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in allp:
                if pid != ME:
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
            time.sleep(3)
        print("killed")
    tmp_check(kill)
    return 0


if __name__ == "__main__":
    sys.exit(main())
