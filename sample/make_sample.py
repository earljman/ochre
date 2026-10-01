#!/usr/bin/env python3
"""Write a fake Firstmate home under sample/home so the board renders out of the box.

Everything here is invented: ticket keys (DEMO-nnn), people, repo and PR numbers.
Re-run any time; it overwrites sample/home. Timestamps are relative to now.
"""
import json, os, random, time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.join(ROOT, "home")
REPO = "example-org/example-app"
now = time.time()
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
random.seed(7)

for d in ("state", "data/board", "data/ops", "data/team-rules", "data/team-rules-sync", "config"):
    os.makedirs(os.path.join(HOME, d), exist_ok=True)


def w(rel, obj):
    with open(os.path.join(HOME, rel), "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj, indent=1))


TICKETS = [
    ("DEMO-101", "[Orders] Save button stays disabled after editing a line quantity", "M1", "In Development", "working"),
    ("DEMO-102", "[Inventory] Stock list ignores the location filter on first load", "M1", "PR Submitted", "pr"),
    ("DEMO-103", "[Returns] Return saves with no warning when the location is empty", "M2", "Merged", "done"),
    ("DEMO-104", "[Reports] Export to CSV drops the last column", "M2", "Merged", "done"),
    ("DEMO-105", "[Settings] Number format preview does not update", "M2", "In Development", "parked"),
    ("DEMO-106", "[Builds] Build screen has no batch number entry", "M3", "Open", "candidate"),
    ("DEMO-107", "[Picking] Partial pick quantity cannot be entered", "M3", "Open", "candidate"),
    ("DEMO-108", "[Contacts] Phone field rejects international format", "M3", "Open", "candidate"),
    ("DEMO-109", "[Sales] Customer picker has no create-new option", "M4", "Open", "candidate"),
]
items = []
for k, s, m, st, state in TICKETS:
    items.append({"key": k, "summary": s, "status": st, "assignee": "Unassigned", "milestone": m, "required": "Yes",
                  "priority": random.choice(["P2 - High", "P3 - Medium"]), "url": f"https://example.atlassian.net/browse/{k}",
                  "reason": "Small, well-specified UI fix with clear acceptance criteria.", "risk": "Touches a shared form component.",
                  "state": state, "pick": state == "candidate" and k == "DEMO-106", "jira_status": st})
w("data/board/queue.json", {"updated": iso(now - 3600), "items": items})
w("data/board/fleet.json", {"orchestrator_name": "demo-supervisor", "orchestrator_model": "example-model (low effort)",
                            "ship_slots": 3, "worker_model_policy": "Workers run a small model at low effort."})
w("data/board/cap.json", {"configured": 3, "effective": 3, "reason": "configured cap 3", "since": iso(now - 86400),
                          "mem_available_mb": 5200, "checked": iso(now)})
w("data/board/tmp.json", {"pct": 14, "used_mb": 540, "size_mb": 3900, "checked": iso(now)})

TASKS = [
    ("fix-demo-101", "DEMO-101", ["working [at={t}]: reproduced on dev; fixing the form state hook",
                                  "working [at={t}]: fix implemented, 12 tests pass; starting live validation"], None),
    ("fix-demo-102", "DEMO-102", ["working [at={t}]: fix implemented",
                                  "done [at={t}]: PR https://github.com/%s/pull/412 checks green, screenshots verified" % REPO], 412),
    ("fix-demo-105", "DEMO-105", ["working [at={t}]: investigating number format settings",
                                  "paused [at={t}]: waiting on a product ruling about the preview format"], None),
]
notes = {}
for i, (tid, key, lines, pr) in enumerate(TASKS):
    meta = [f"worktree=/srv/work/{tid}", "harness=pi", "kind=ship", "mode=no-mistakes", "yolo=off",
            f"branch=fm/{tid}", "model=example/small-model", "effort=low"]
    if pr:
        meta.append(f"pr=https://github.com/{REPO}/pull/{pr}")
    w(f"state/{tid}.meta", "\n".join(meta) + "\n")
    w(f"state/{tid}.status", "\n".join(l.format(t=int(now - 3600 * (len(lines) - j) - 600 * i)) for j, l in enumerate(lines)) + "\n")
    notes[tid] = {"note": f"{key}: " + lines[-1].split(": ", 1)[1], "at": int(now - 900 * (i + 1))}
w("data/board/worker-notes.json", notes)
tasks = []
for i, (tid, key, lines, pr) in enumerate(TASKS):
    last = lines[-1].format(t=int(now - 600))
    st, _, note = last.partition(": ")
    tasks.append({"id": tid, "kind": "ship", "harness": "pi", "mode": "no-mistakes", "yolo": "off", "branch": f"fm/{tid}",
                  "paths": {"meta": {"path": f"{HOME}/state/{tid}.meta", "present": True},
                            "status_log": {"path": f"{HOME}/state/{tid}.status", "present": True, "kind": "event_history",
                                           "last_event": {"state": st.split(" ")[0], "note": note, "raw": last, "age_seconds": 600}},
                            "worktree": {"path": f"/srv/work/{tid}", "present": True}}})
w("data/board/fleet-snapshot.json", {"schema": "sample", "generated": iso(now), "tasks": tasks})
w("data/board/backlog.txt", "tasks[1]{id,state,kind,repo,title}:\n  fix-demo-109,queued,ship,example-app,Fix DEMO-109 (customer picker create-new)\n")
w("state/parent-replies.status", f"working [key=demo] [at={int(now - 1200)}]: DEMO-104 merged; slot refilled with DEMO-101\n")

prs = []
for n, k, state, age_h, merged_h in [(405, "DEMO-103", "MERGED", 50, 30), (408, "DEMO-104", "MERGED", 30, 6), (409, None, "MERGED", 26, 20),
                                     (412, "DEMO-102", "OPEN", 5, None), (399, "DEMO-099", "MERGED", 200, 150)]:
    title = f"({k}) fix: {next((s for kk, s, *_ in TICKETS if kk == k), 'earlier fix')}" if k else "chore(ci): cache test dependencies"
    prs.append({"number": n, "title": title, "state": state, "url": f"https://github.com/{REPO}/pull/{n}",
                "headRefName": f"fm/fix-{k.lower()}" if k else "fm/ci-cache", "isDraft": False,
                "createdAt": iso(now - age_h * 3600), "mergedAt": iso(now - merged_h * 3600) if merged_h else None})
w("data/board/prs.json", {"updated": iso(now), "prs": prs})

days = {}
for d in range(14, -1, -1):
    day = datetime.fromtimestamp(now - d * 86400, timezone.utc).strftime("%Y-%m-%d")
    if d < 5:
        days[day] = {"provider-a": {"example/large-model": random.randint(80, 300) * 10**6},
                     "provider-b": {"example/small-model": random.randint(40, 160) * 10**6}}
w("data/board/usage.json", {"updated": iso(now), "days": days,
                            "tickets": {"DEMO-101": 41_000_000, "DEMO-102": 28_000_000, "DEMO-103": 19_000_000, "DEMO-104": 9_000_000}})

w("data/team-rules/VERSIONS.json", {"last_run": iso(now - 600), "last_status": "ok",
                                     "announced": {"version": "1.2", "seen": iso(now - 86400), "status": "ok"},
                                     "documents": {"1-rules.md": {"label": "1 Team rules", "version": "1.2", "status": "ok",
                                                                  "format": "html converted to markdown", "last_fetched": iso(now - 600)}}})
w("data/team-rules/1-rules.md", "# Team rules\n\nVersion 1.2\n\n- One ticket per pull request.\n- Every PR carries screenshots.\n")
w("data/team-rules-sync/README.md", "Rules are synced from a chat canvas every 15 minutes (demo data).\n")
w("data/prioritization.md", "# Selection directive (demo)\n\nWork the focus milestones first, then the next milestone.\n")
w("data/ops/jira-hold.json", {"hold": [], "why": {}})
print("sample home written to", HOME)
