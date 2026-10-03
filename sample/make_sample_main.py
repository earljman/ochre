#!/usr/bin/env python3
"""Write a fake *main* Firstmate home under sample/home-main (use with BOARD_PROFILE=main).

Unlike sample/home it has no queue, cap/tmp gate, rules or ticket keys; tasks are keyed on their task id,
two repos are involved, and the fleet snapshot carries a `secondmate_current` block.
Everything is invented. Render it with:

    python3 sample/make_sample_main.py            # optional argument: a directory to write to
    BOARD_PROFILE=main BOARD_GH_REPOS=example-org/app-one,example-org/app-two \
    BOARD_SECONDMATE_BOARDS=demo-team=https://board.example.com FM_HOME=sample/home-main python3 board/generate.py --fast
"""
import json, os, sys, time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, "home-main")
now = time.time()
iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

for d in ("state", "data/board"):
    os.makedirs(os.path.join(HOME, d), exist_ok=True)


def w(rel, obj):
    with open(os.path.join(HOME, rel), "w") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj, indent=1))


TASKS = [  # id, repo, status lines, PR number
    ("tidy-api-errors", "example-org/app-one", ["working [at={t}]: mapping error codes to one enum"], None),
    ("docs-refresh", "example-org/app-two", ["working [at={t}]: drafting the install page",
                                             "done [at={t}]: PR https://github.com/example-org/app-two/pull/31 opened"], 31),
]
tasks = []
for i, (tid, repo, lines, pr) in enumerate(TASKS):
    meta = [f"worktree=/srv/work/{tid}", "harness=claude", "kind=ship", f"branch=fm/{tid}", "model=example/small-model", "effort=low", f"repo={repo}"]
    if pr:
        meta.append(f"pr=https://github.com/{repo}/pull/{pr}")
    w(f"state/{tid}.meta", "\n".join(meta) + "\n")
    w(f"state/{tid}.status", "\n".join(l.format(t=int(now - 3600 + 600 * j - 300 * i)) for j, l in enumerate(lines)) + "\n")
    st, _, note = lines[-1].format(t=int(now - 600)).partition(": ")
    tasks.append({"id": tid, "kind": "ship", "branch": f"fm/{tid}", "project": repo.split("/")[1],
                  "pr": {"url": f"https://github.com/{repo}/pull/{pr}" if pr else None},
                  "paths": {"status_log": {"last_event": {"state": st.split(" ")[0], "note": note}}}})

records = [
    {"id": "demo-team", "current": {"state": "active"}, "provenance": {"selected": "structured-home", "trust": "complete"},
     "freshness": {"status": "fresh", "age_seconds": 95}, "active_children": [{"id": "x"}] * 2, "decisions_open": [{"id": "d1"}],
     "queued": [], "landed": [],
     "counts": {"active_children": 2, "decisions_open": 1, "holds": 0, "queued": 4, "landed": 7}},
    {"id": "docs-mate", "current": {"state": "unknown"}, "provenance": {"selected": "unknown"},
     "freshness": {"status": "unknown", "age_seconds": None}, "active_children": [], "decisions_open": [], "queued": [], "landed": [],
     "counts": {"active_children": 0, "decisions_open": 0, "holds": 0, "queued": 0, "landed": 0}},
]
w("data/board/fleet-snapshot.json", {"schema": "sample", "generated": iso(now), "tasks": tasks,
                                      "secondmate_current": {"records": records, "total": 3, "shown": 2, "truncated": 1}})
w("data/board/backlog.txt", "tasks[0]{id,state,kind,repo,title}:\n")
w("data/board/prs.json", {"updated": iso(now), "prs": [
    {"number": 31, "title": "docs: refresh the install page", "state": "OPEN", "isDraft": False, "repo": "example-org/app-two",
     "url": "https://github.com/example-org/app-two/pull/31", "headRefName": "fm/docs-refresh", "createdAt": iso(now - 5400), "mergedAt": None},
    {"number": 30, "title": "chore: bump the lint config", "state": "MERGED", "isDraft": False, "repo": "example-org/app-one",
     "url": "https://github.com/example-org/app-one/pull/30", "headRefName": "fm/lint-bump", "createdAt": iso(now - 40000), "mergedAt": iso(now - 20000)}]})
print("sample main home written to", HOME)
