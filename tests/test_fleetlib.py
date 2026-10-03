import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "board"))
import fleetlib as FL  # noqa: E402


class Done:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


# lsof -a -d cwd -Fpcn: one p/c/n group per process (all names are synthetic)
FAKE_LSOF = "\n".join([
    "p101", "cclaude", "n/work/tasks/fix-a",             # worker in a recorded worktree
    "p102", "cpi", "n/work/tasks/fix-b",                 # worker in a recorded worktree
    "p103", "cnode", "n/work/tasks/fix-c",               # node inside a recorded worktree: counts
    "p104", "cnode", "n/elsewhere/dev-server",           # bare node: dropped
    "p105", "cclaude", "n/elsewhere/scratch",            # claude outside any worktree: dropped
    "p106", "cvim", "n/work/tasks/fix-d",                # not a worker command
    "p107", "cclaude", "n/work/tasks/not-recorded",      # claude in an unrecorded folder: dropped
    "",
])
WORKTREES = ["/work/tasks/fix-a", "/work/tasks/fix-b", "/work/tasks/fix-c", "/work/tasks/fix-d", "/work/tasks/fix-e"]


class Lsof(unittest.TestCase):
    def test_parse(self):
        got = FL.parse_lsof(FAKE_LSOF)
        self.assertEqual(got[0], ("claude", "/work/tasks/fix-a"))
        self.assertEqual(len(got), 7)

    def test_fallback_counts_only_worktrees(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            return Done(FAKE_LSOF)

        with tempfile.TemporaryDirectory() as empty:
            live = FL.live_worktrees(WORKTREES, proc_root=os.path.join(empty, "no-proc"), runner=runner)
        self.assertEqual(live, {"/work/tasks/fix-a", "/work/tasks/fix-b", "/work/tasks/fix-c"})
        self.assertEqual(calls[0][0], "lsof")

    def test_returns_recorded_spelling_through_a_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            link = os.path.join(d, "link")
            os.mkdir(real)
            os.symlink(real, link)
            runner = lambda cmd, **kw: Done(f"p1\ncclaude\nn{os.path.realpath(real)}\n")
            live = FL.live_worktrees([link], proc_root=os.path.join(d, "no-proc"), runner=runner)
        self.assertEqual(live, {link})

    def test_lsof_failure_raises(self):
        runner = lambda cmd, **kw: Done("", 1, "lsof: boom\n")
        with tempfile.TemporaryDirectory() as d, self.assertRaises(OSError):
            FL.live_worktrees(WORKTREES, proc_root=os.path.join(d, "no-proc"), runner=runner)

    def test_no_worktrees_skips_the_scan(self):
        def runner(cmd, **kw):
            raise AssertionError("lsof should not run")

        self.assertEqual(FL.live_worktrees([], proc_root="/nonexistent", runner=runner), set())

    def test_proc_is_used_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            wt = os.path.join(d, "wt")
            os.mkdir(wt)
            for pid, comm, cwd in (("11", "claude", wt), ("12", "node", d)):
                os.makedirs(os.path.join(d, "proc", pid))
                with open(os.path.join(d, "proc", pid, "comm"), "w") as f:
                    f.write(comm + "\n")
                os.symlink(cwd, os.path.join(d, "proc", pid, "cwd"))

            def runner(cmd, **kw):
                raise AssertionError("lsof should not run when /proc exists")

            self.assertEqual(FL.live_worktrees([wt], proc_root=os.path.join(d, "proc"), runner=runner), {wt})


class TaskRepo(unittest.TestCase):
    KNOWN = ["example-org/app-one", "example-org/app-two"]

    def test_record_fields(self):
        self.assertEqual(FL.task_repo({"repo": "example-org/app-two"}), "example-org/app-two")
        self.assertEqual(FL.task_repo({}, "https://github.com/example-org/app-one/"), "example-org/app-one")

    def test_project_must_name_a_configured_repo(self):
        self.assertEqual(FL.task_repo({"project": "App-Two"}, known=self.KNOWN), "example-org/app-two")
        self.assertEqual(FL.task_repo({"project": "/Users/x/other"}, known=self.KNOWN), "")

    def test_pr_url(self):
        t = {"pr": {"url": "https://github.com/example-org/app-one/pull/9"}}
        self.assertEqual(FL.task_repo(t), "example-org/app-one")

    def test_unknown(self):
        self.assertEqual(FL.task_repo({"pr": {"url": None}}), "")


class Secondmates(unittest.TestCase):
    def snap(self, **kw):
        rec = {"id": "alpha-team", "current": {"state": "active"},
               "provenance": {"selected": "structured-home", "trust": "complete"},
               "freshness": {"status": "fresh", "age_seconds": 40},
               "counts": {"active_children": 2, "decisions_open": 1, "queued": 3, "landed": 5}}
        rec.update(kw)
        return {"secondmate_current": {"records": [rec], "truncated": 2}}

    def test_row(self):
        rows, omitted = FL.secondmate_rows(self.snap(), {"alpha-team": "https://board.example.com/alpha"})
        self.assertEqual(omitted, 2)
        r = rows[0]
        self.assertEqual((r["active"], r["queued"], r["decisions"], r["landed"]), (2, 3, 1, 5))
        self.assertEqual((r["freshness"], r["age"], r["url"]), ("fresh", 40, "https://board.example.com/alpha"))

    def test_unreadable_home_is_no_data(self):
        rows, _ = FL.secondmate_rows(self.snap(provenance={"selected": "unknown"}, freshness={"status": "unknown", "age_seconds": None}))
        r = rows[0]
        self.assertEqual((r["active"], r["queued"], r["decisions"], r["landed"], r["age"]), (None, None, None, None, None))

    def test_empty_sources(self):
        for snap in ({}, {"secondmate_current": None}, {"secondmate_current": {"records": []}}, {"secondmate_current": {"records": "x"}}):
            self.assertEqual(FL.secondmate_rows(snap), ([], 0))

    def test_counts_fall_back_to_list_lengths(self):
        rows, _ = FL.secondmate_rows(self.snap(counts={}, active_children=[1, 2, 3], queued=[]))
        self.assertEqual((rows[0]["active"], rows[0]["queued"], rows[0]["decisions"]), (3, 0, None))

    def test_boards_config(self):
        got = FL.parse_boards("Alpha-Team=https://board.example.com/a?x=1, bad=javascript:alert(1),=https://x.test,solo")
        self.assertEqual(got, {"alpha-team": "https://board.example.com/a?x=1"})


if __name__ == "__main__":
    unittest.main()
