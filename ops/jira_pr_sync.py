#!/usr/bin/env python3
"""Keep Jira in step with the agent's pull requests - deterministic, no model calls.

1. For every OPEN, non-draft PR in the configured repo authored by the configured agent
   account whose title or branch names exactly one ticket key: if the ticket is still
   In Development (and not held in data/ops/jira-hold.json - multi-part tickets that must
   wait for their last PR), move it to PR Submitted with a short plain-language AI-tagged
   comment linking the PR. Also rewrites a PR title so it starts with "(KEY)".
2. For PRs merged 10+ minutes ago whose ticket still sits at PR Submitted (no other open
   PR for the key, not held): move it to Merged with a comment - covers a merge bot that
   skipped the ticket.
Every move is logged to data/ops/jira-pr-sync.jsonl and the parent status. Idempotent.
Credentials: JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN in <FM_HOME>/.env (or the environment).
Usage: jira_pr_sync.py [--dry-run]
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "board"))
import config as C
import base64, json, os, re, subprocess, sys, time, urllib.request

H = C.FM_HOME
REPO, AUTHOR = C.GH_REPO, C.GH_AUTHOR
HOLD, LOG = f"{H}/data/ops/jira-hold.json", f"{H}/data/ops/jira-pr-sync.jsonl"
DRY = "--dry-run" in sys.argv
AGENT, OWNER = C.get("agent_name"), C.get("owner_name")


def env():
    out = {k: v for k, v in os.environ.items() if k.startswith("JIRA_")}
    if not os.path.exists(f"{H}/.env"):
        return out
    for line in open(f"{H}/.env"):
        k, _, v = line.strip().partition("=")
        if k.startswith("JIRA_"):
            out[k] = v.strip().strip('"')
    return out


E = env()
SITE = "https://" + E["JIRA_SITE"].replace("https://", "").rstrip("/")
AUTH = "Basic " + base64.b64encode(f'{E["JIRA_EMAIL"]}:{E["JIRA_API_TOKEN"]}'.encode()).decode()


def jira(method, path, body=None):
    req = urllib.request.Request(SITE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": AUTH, "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
        return json.loads(data) if data else {}


def main():
    try:
        held = set(json.load(open(HOLD)).get("hold", []))
    except (OSError, ValueError):
        held = set()
    p = subprocess.run(["gh", "pr", "list", "-R", REPO, "--state", "open", "--author", AUTHOR, "--limit", "50",
                        "--json", "number,title,headRefName,isDraft,url"], capture_output=True, text=True, timeout=60)
    prs = json.loads(p.stdout or "[]")
    for pr in prs:
        if pr.get("isDraft"):
            continue
        keys = set(re.findall(C.KEY_RE, pr["title"] + " " + pr["headRefName"].upper().replace("FIX-" + C.KEY + "-", C.KEY + "-")))
        if len(keys) != 1:
            continue
        key = keys.pop()
        if not pr["title"].startswith(f"({key})"):  # key-first title rule; the pipeline often drops it
            new = f"({key}) " + re.sub(r"\s*\(?" + key + r"\)?\s*", " ", pr["title"]).strip()
            print(f"{key}: retitle PR {pr['number']} -> {new}" + (" [dry run]" if DRY else ""))
            if not DRY:
                subprocess.run(["gh", "pr", "edit", str(pr["number"]), "-R", REPO, "--title", new], capture_output=True, timeout=60)
        if key in held:
            continue
        try:
            status = jira("GET", f"/rest/api/3/issue/{key}?fields=status")["fields"]["status"]["name"]
        except Exception as e:  # noqa: BLE001
            print(f"{key}: could not read status ({e})")
            continue
        if status != "In Development":
            continue
        text = (f"[AI] Summary: A pull request for this ticket is open and ready for review: {pr['url']} . "
                f"Status: In Development -> PR Submitted. Moved by: {AGENT} (AI agent for {OWNER}), automatically when the pull request opened.")
        print(f"{key}: In Development -> PR Submitted ({pr['url']})" + (" [dry run]" if DRY else ""))
        if DRY:
            continue
        try:
            jira("POST", f"/rest/api/3/issue/{key}/comment", {"body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}})
            jira("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": C.get("jira_transition_pr_submitted")}})
            ok = True
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"{key}: move failed ({e})")
        now = int(time.time())
        with open(LOG, "a") as f:
            f.write(json.dumps({"at": now, "key": key, "pr": pr["url"], "ok": ok}) + "\n")
        with open(f"{H}/state/parent-replies.status", "a") as f:
            f.write(f"working [key=jira-pr-sync] [at={now}]: {key} {'moved' if ok else 'could NOT be moved'} In Development -> PR Submitted automatically because its PR is open: {pr['url']}\n")


def merged_sweep(held):
    """PR merged >10 min ago but the ticket still sits at PR Submitted: the merge bot skipped it; move it to Merged."""
    p = subprocess.run(["gh", "pr", "list", "-R", REPO, "--state", "merged", "--author", AUTHOR, "--limit", "30",
                        "--json", "number,title,headRefName,url,mergedAt"], capture_output=True, text=True, timeout=60)
    from datetime import datetime
    o = subprocess.run(["gh", "pr", "list", "-R", REPO, "--state", "open", "--author", AUTHOR, "--limit", "50", "--json", "title,headRefName"],
                       capture_output=True, text=True, timeout=60)
    still_open = set(re.findall(C.KEY_RE, " ".join(x["title"] + " " + x["headRefName"].upper().replace("FIX-" + C.KEY + "-", C.KEY + "-") for x in json.loads(o.stdout or "[]"))))
    done = set()
    for pr in json.loads(p.stdout or "[]"):
        try:
            age = time.time() - datetime.strptime(pr["mergedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=__import__("datetime").timezone.utc).timestamp()
        except (TypeError, ValueError):
            continue
        if age < 600 or age > 3 * 86400:
            continue
        keys = set(re.findall(C.KEY_RE, pr["title"] + " " + pr["headRefName"].upper().replace("FIX-" + C.KEY + "-", C.KEY + "-")))
        if len(keys) != 1:
            continue
        key = keys.pop()
        if key in held or key in still_open or key in done:  # another PR for it still open, or already handled this pass
            continue
        done.add(key)
        try:
            status = jira("GET", f"/rest/api/3/issue/{key}?fields=status")["fields"]["status"]["name"]
        except Exception:  # noqa: BLE001
            continue
        if status != "PR Submitted":
            continue
        text = (f"[AI] Summary: The fix for this ticket is now merged into the development build: {pr['url']} . "
                f"Status: PR Submitted -> Merged. Moved by: {AGENT} (AI agent for {OWNER}), automatically because the merge bot did not move it.")
        print(f"{key}: PR Submitted -> Merged ({pr['url']})" + (" [dry run]" if DRY else ""))
        if DRY:
            continue
        try:
            jira("POST", f"/rest/api/3/issue/{key}/comment", {"body": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}})
            jira("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": C.get("jira_transition_merged")}})
            ok = True
        except Exception:  # noqa: BLE001
            ok = False
        now = int(time.time())
        with open(LOG, "a") as f:
            f.write(json.dumps({"at": now, "key": key, "pr": pr["url"], "ok": ok, "to": "Merged"}) + "\n")
        with open(f"{H}/state/parent-replies.status", "a") as f:
            f.write(f"working [key=jira-pr-sync] [at={now}]: {key} {'moved' if ok else 'could NOT be moved'} PR Submitted -> Merged automatically (merge bot skipped it): {pr['url']}\n")


if __name__ == "__main__":
    main()
    try:
        merged_sweep(set(json.load(open(HOLD)).get("hold", [])))
    except (OSError, ValueError):
        merged_sweep(set())
