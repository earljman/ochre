"""Read a worker's no-mistakes pipeline state for the board - deterministic, no model calls.

pipeline_state(worktree) runs `no-mistakes axi status` in the worker's copy (about 0.2s)
and parses its plain-text output into:
  {run, branch, status, head, findings_summary,
   steps: [{step, status, findings, duration_ms}],            # the 9 pipeline steps in order
   active: {step, status, active_for, round_active_for, last_activity, round} | None,
   findings: [{id, severity, action, description}],
   phase, pushed}                                              # branch_sync.pipeline
Returns None when there is no run, the command fails, or it times out.
"""
import csv, io, re, subprocess


def _int(v):
    try:
        return int(str(v).strip() or 0)
    except ValueError:
        return 0

STEP_ORDER = ["intent", "rebase", "review", "test", "document", "lint", "push", "pr", "ci"]


def _rows(lines, i):
    """Collect the indented CSV rows that follow a `name[n]{...}:` header at index i."""
    out, j = [], i + 1
    base = len(lines[i]) - len(lines[i].lstrip())
    while j < len(lines):
        ln = lines[j]
        if not ln.strip() or len(ln) - len(ln.lstrip()) <= base:
            break
        out.append(next(csv.reader(io.StringIO(ln.strip())), []))
        j += 1
    return out


def pipeline_state(worktree, timeout=1.5):
    try:
        p = subprocess.run(["no-mistakes", "axi", "status"], cwd=worktree, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = p.stdout or ""
    if not text.startswith("run:") or "steps[" not in text:
        return None
    lines = text.splitlines()
    st = {"steps": [], "active": None, "findings": [], "phase": "", "pushed": False}
    section = ""
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not ln.startswith(" ") and s.endswith(":"):
            section = s[:-1]
        if section == "run" and ln.startswith("  ") and not ln.startswith("    ") and ":" in s and "[" not in s:
            k, _, v = s.partition(":")
            v = v.strip().strip('"')
            if k in ("id", "branch", "status", "head"):
                st["run" if k == "id" else k] = v
            elif k == "findings":
                st["findings_summary"] = v
        if s.startswith("steps[") and section == "run":
            for r in _rows(lines, i):
                if len(r) >= 4:
                    st["steps"].append({"step": r[0], "status": r[1], "findings": _int(r[2]), "duration_ms": _int(r[3])})
        elif s.startswith("active_steps[") and section == "run":
            rows = _rows(lines, i)
            if rows and len(rows[0]) >= 7:
                r = rows[0]
                st["active"] = {"step": r[0], "status": r[1], "active_for": r[2], "round_active_for": r[3], "last_activity": r[4], "round": r[6]}
        elif s.startswith("findings[") and "{id" in s:
            for r in _rows(lines, i):
                if len(r) >= 5:
                    st["findings"].append({"id": r[0], "severity": r[1], "action": r[3], "description": r[4]})
        elif section == "branch_sync":
            m = re.match(r"\s+phase:\s*(\S+)", ln)
            if m:
                st["phase"] = m.group(1)
            m = re.match(r"\s+pushed_head:\s*\"?([0-9a-f]*)", ln)
            if m and m.group(1):
                st["pushed"] = True
    order = {n: k for k, n in enumerate(STEP_ORDER)}
    st["steps"].sort(key=lambda x: order.get(x["step"], 99))
    return st if st["steps"] else None


def progress(st):
    """(position 1-9, total, label) for the card face, e.g. (4, 9, 'test · fixing')."""
    steps = st["steps"]
    cur = st.get("active") or next((x for x in steps if x["status"] not in ("completed", "skipped")), None)
    if cur is None:
        return len(steps), len(steps), "all steps done"
    if cur["step"] == "ci" and "checks passed" in str((st.get("active") or {}).get("last_activity", "")):
        return len(steps), len(steps), "ci · passed, awaiting merge"
    if st.get("status") and st["status"] != "running":
        return next((k + 1 for k, x in enumerate(steps) if x["step"] == cur["step"]), len(steps)), len(steps), f'stopped at {cur["step"]} · {st["status"]}'
    pos = next((k + 1 for k, x in enumerate(steps) if x["step"] == cur["step"]), len(steps))
    return pos, len(steps), f'{cur["step"]} · {cur["status"]}'
