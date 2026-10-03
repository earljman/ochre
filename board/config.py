"""Shared configuration for the board and its ops scripts.

Every deployment-specific value lives here and is read from, in order of precedence:
  1. environment variables (BOARD_*, FM_HOME),
  2. a TOML file named by BOARD_CONFIG (default: config.toml next to this repo's root),
  3. the defaults below, which point at the bundled fake sample home so the board renders
     out of the box.
See config.example.toml for every key.
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DEFAULTS = {
    # Firstmate home the board reads (state/, data/, config/).
    "fm_home": os.path.join(_ROOT, "sample", "home"),
    # Where the rendered board is written.
    "out": os.path.join(_ROOT, "out", "index.html"),
    # "team" renders every panel (the original single-team board); "main" is for a main
    # Firstmate home: the Queue, cap/tmp gate, Rules and History panels are hidden when their
    # source files are absent, no Jira/ops scripts run, and cards key on task id.
    "profile": "team",
    # Page header text.
    "title": "Ochre",
    "subtitle": "Status board for a Firstmate dev team",
    # Name shown for the supervising agent (in Jira comments and worker notes).
    "agent_name": "Board agent",
    # Human the agent works for (named in automated Jira comments).
    "owner_name": "the team lead",
    # Ticket key prefix, e.g. ABC for ABC-123.
    "key_prefix": "DEMO",
    # GitHub repository (owner/name) and the account that opens the agent's PRs.
    "gh_repo": "example-org/example-app",
    "gh_author": "example-bot",
    # Several repositories: comma-separated owner/name list (or a TOML array). Overrides gh_repo.
    "gh_repos": "",
    # Board URLs of second mates that keep their own board: comma-separated id=url pairs.
    "secondmate_boards": "",
    # Address and port of board/serve.py. Loopback by default; set a Tailnet address to opt in.
    "bind": "127.0.0.1",
    "port": "8780",
    # Jira site base URL (no trailing slash) used for ticket links and the REST API.
    "jira_site": "https://example.atlassian.net",
    # Jira transition ids used by ops/jira_pr_sync.py.
    "jira_transition_pr_submitted": "13",
    "jira_transition_merged": "12",
    # Milestones that count as current focus (the resource gate never pauses these).
    "focus_milestones": "M1,M2",
    # Directory names (under <fm_home>/data) for the synced team rules and their sync job.
    "rules_dir": "team-rules",
    "rules_sync_dir": "team-rules-sync",
    # localStorage key prefix for per-browser UI preferences.
    "storage_prefix": "ochre",
    # systemd unit that re-renders the board (started by the rules sync after a change).
    "board_service": "board.service",
    # Slack source for the rules sync: channel id and comma-separated name=file_id canvases.
    "slack_channel": "",
    "slack_canvases": "",
}


def _flat(v):
    """TOML arrays become comma-separated strings; everything else becomes a string."""
    if isinstance(v, (list, tuple)):
        return ",".join(str(x) for x in v)
    return str(v)


def _load_file():
    path = os.environ.get("BOARD_CONFIG", os.path.join(_ROOT, "config.toml"))
    if not os.path.exists(path):
        return {}
    try:
        import tomllib
    except ImportError:  # Python < 3.11: tiny key = "value" parser
        out = {}
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = _flat(v.strip().strip('"').strip("'"))
        return out
    with open(path, "rb") as f:
        return {k: _flat(v) for k, v in tomllib.load(f).items()}


_FILE = _load_file()


def get(key):
    env = "FM_HOME" if key == "fm_home" else "BOARD_" + key.upper()
    return os.environ.get(env) or _FILE.get(key) or _DEFAULTS[key]


FM_HOME = get("fm_home")
KEY = get("key_prefix")
KEY_RE = rf"{KEY}-\d+"
JIRA_SITE = get("jira_site").rstrip("/")
JIRA_BROWSE = JIRA_SITE + "/browse/"
PROFILE = get("profile").strip().lower()
if PROFILE not in ("team", "main"):
    raise ValueError(f'profile must be "team" or "main", not {PROFILE!r}')
GH_REPO = get("gh_repo")
GH_AUTHOR = get("gh_author")
FOCUS = [m.strip() for m in get("focus_milestones").split(",") if m.strip()]


def parse_repos(value):
    """Comma/space separated owner/name list -> unique repos in order. Raises on a malformed entry."""
    import re
    out = []
    for part in re.split(r"[,\s]+", str(value or "").strip()):
        if not part:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", part):
            raise ValueError(f"not an owner/name repository: {part!r}")
        if part not in out:
            out.append(part)
    return out


GH_REPOS = parse_repos(get("gh_repos")) or [GH_REPO]
GH_REPO = GH_REPOS[0]
