"""Render the synthetic main home end to end and check what the main profile draws."""
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "board"))


def render(profile_env, home):
    out = os.path.join(home, "out.html")
    cache = os.path.join(home, "data", "board", ".sources-cache.json")  # --fast would reuse the previous render's snapshot
    if os.path.exists(cache):
        os.remove(cache)
    env = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_") and k != "FM_HOME"}
    env.update(profile_env, FM_HOME=home, BOARD_CONFIG=os.path.join(home, "none.toml"))
    p = subprocess.run([sys.executable, os.path.join(ROOT, "board", "generate.py"), "--fast", "--out", out],
                       capture_output=True, text=True, env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    with open(out, encoding="utf-8") as f:
        return f.read()


class MainProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        home = os.path.join(cls.tmp.name, "home")
        subprocess.run([sys.executable, os.path.join(ROOT, "sample", "make_sample_main.py"), home], check=True, capture_output=True)
        cls.home = home
        cls.env = {"BOARD_PROFILE": "main", "BOARD_GH_REPOS": "example-org/app-one,example-org/app-two",
                   "BOARD_SECONDMATE_BOARDS": "demo-team=https://board.example.com/demo"}
        cls.page = render(cls.env, home)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def sections(self, page=None):
        return set(re.findall(r'<section id="([a-z-]+)"', page or self.page))

    def test_panels_without_a_source_are_not_drawn(self):
        self.assertEqual(self.sections(), {"fleet-block", "secondmates-block", "throughput-block", "usage-block"})
        for gone in ("Ranked next", "Rules", "sign out", "ship slots", 'href="#queue-block"', 'href="#rules-block"', 'href="#logs-block"'):
            self.assertNotIn(gone, self.page)

    def test_panel_returns_when_its_source_appears(self):
        with open(os.path.join(self.home, "data", "board", "queue.json"), "w") as f:
            f.write('{"updated": null, "items": []}')
        with open(os.path.join(self.home, "data", "prioritization.md"), "w") as f:
            f.write("# Directive\n\n- one thing\n")
        try:
            page = render(self.env, self.home)
        finally:
            os.remove(os.path.join(self.home, "data", "board", "queue.json"))
            os.remove(os.path.join(self.home, "data", "prioritization.md"))
        self.assertIn("queue-block", self.sections(page))
        self.assertIn("rules-block", self.sections(page))

    def test_cards_key_on_task_id_and_pr_follows_the_branch(self):
        self.assertIn("docs-refresh", self.page)
        self.assertIn("PR #31", self.page)                    # matched by branch; the title has no ticket key
        self.assertNotIn("DEMO-", self.page)

    def test_secondmate_rows(self):
        sm = re.search(r'<section id="secondmates-block">.*?</section>', self.page, re.S).group(0)
        self.assertIn('href="https://board.example.com/demo"', sm)     # from config
        self.assertEqual(sm.count('class="smrow"'), 2)
        self.assertEqual(sm.count("no data"), 4)                       # the unreadable mate: four counts
        self.assertIn("fresh", sm)
        self.assertIn("1 more second mate not listed", sm)

    def test_no_secondmate_data(self):
        import json
        path = os.path.join(self.home, "data", "board", "fleet-snapshot.json")
        with open(path) as f:
            snap = json.load(f)
        saved = dict(snap)
        snap.pop("secondmate_current")
        with open(path, "w") as f:
            json.dump(snap, f)
        try:
            page = render(self.env, self.home)
        finally:
            with open(path, "w") as f:
                json.dump(saved, f)
        self.assertIn("No data: the fleet snapshot lists no second mates", page)

    def test_repos_in_header(self):
        self.assertIn("example-org/app-one", self.page)
        self.assertIn("example-org/app-two", self.page)


class Config(unittest.TestCase):
    def run_cfg(self, env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("BOARD_")}
        e.update(env, BOARD_CONFIG="/nonexistent.toml")
        return subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'board'); import config as C; print(C.PROFILE, C.GH_REPOS)"],
                              capture_output=True, text=True, env=e, cwd=ROOT)

    def test_defaults_are_the_team_profile(self):
        self.assertEqual(self.run_cfg({}).stdout.split(" ", 1)[0], "team")

    def test_repo_list(self):
        self.assertIn("['a/b', 'c/d']", self.run_cfg({"BOARD_GH_REPOS": "a/b, c/d a/b"}).stdout)

    def test_bad_values_fail_loudly(self):
        self.assertNotEqual(self.run_cfg({"BOARD_PROFILE": "mini"}).returncode, 0)
        self.assertNotEqual(self.run_cfg({"BOARD_GH_REPOS": "not-a-repo"}).returncode, 0)

    def test_serve_refuses_wildcard_bind(self):
        import serve
        self.assertEqual(serve.check_bind("127.0.0.1"), "127.0.0.1")
        self.assertEqual(serve.check_bind("192.0.2.10"), "192.0.2.10")
        for bad in ("0.0.0.0", "::", "localhost"):
            with self.assertRaises(ValueError):
                serve.check_bind(bad)


if __name__ == "__main__":
    unittest.main()
