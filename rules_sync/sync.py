#!/usr/bin/env python3
"""Team rules sync - deterministic, stdlib only, no model calls.

Pulls a set of Slack canvases (the team's working rules), converts each from Slack's
HTML to Markdown (html.parser, no model), and writes the Markdown into
data/<rules_dir>/ only when the converted content changed; the raw HTML of the last
change is kept beside it as <name>.html for audit. Records each document's
"Version x.y" (read from the converted text) plus the fetch time in
data/<rules_dir>/VERSIONS.json, reads the channel for "<announce prefix>: version x.y"
announcements and records the announced version, and on any change appends one line to
the parent status channel and re-renders the board.

Config (board/config.py): slack_channel = channel id; slack_canvases =
"FILEID:1-rules.md:Rules,FILEID:2-blockers.md:Blockers,..."; rules_announce = the
announcement prefix (default "Team rules").
Token: SLACK_TOKEN in <FM_HOME>/.env or the environment (never logged). Without it the
script prints "token not configured" and exits 0 so the timer stays quiet.
On any API failure the last good copy of every file is kept untouched.

Slack calls (Web API, JSON over HTTPS, Authorization: Bearer <token>):
  files.info?file=<id>            -> title, filetype, url_private_download
  GET <url_private_download>      -> canvas body (HTML, quip-canvas-content divs)
  conversations.history?channel=  -> announcement messages since last run
Minimal scopes for a bot token (bot invited to the channel): files:read +
channels:history (groups:history instead for a private channel).
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "board"))
import config as C
import json, os, re, sys, time, urllib.request, urllib.parse, urllib.error, subprocess, datetime
from html.parser import HTMLParser

HOME = C.FM_HOME
RULES = f"{HOME}/data/{C.get('rules_dir')}"
VERSIONS = f"{RULES}/VERSIONS.json"
STATE_LOG = f"{HOME}/state/rules-sync.log"
PARENT = f"{HOME}/state/parent-replies.status"
CHANNEL = C.get("slack_channel")
CANVASES = [tuple(x.split(":", 2)) for x in C.get("slack_canvases").split(",") if x.count(":") >= 2]  # (file id, target file name, label)
VERSION_RE = re.compile(r"[Vv]ersion\s+(\d+\.\d+)")
ANNOUNCE_RE = re.compile(re.escape(os.environ.get("BOARD_RULES_ANNOUNCE", "Team rules")) + r":\s*version\s+(\d+\.\d+)", re.I)


# ---------------------------------------------------------------- HTML -> Markdown
class CanvasToMarkdown(HTMLParser):
    """Slack canvas HTML (quip-canvas-content) to Markdown.

    Headings, paragraphs, bold/italic/code, links, bullet / numbered / checklist
    lists (nesting from the div indent), blockquotes and tables (pipe tables).
    All element ids (temp:C:...) and styles are dropped.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []          # finished block strings
        self.buf = []          # inline fragments of the current block
        self.href = []         # open link hrefs (None = plain text link)
        self.quote = 0
        self.heading = None
        self.list_style = []   # stack of ("ul"|"ol"|"check", counter list)
        self.div_style = []    # stack of (section-style, indent)
        self.li = []           # stack of dicts for open list items
        self.table = None      # list of rows; row = list of cells (strings)
        self.cell = None
        self.in_pre = False

    # -- helpers
    def _text(self):
        t = "".join(self.buf)
        self.buf = []
        return re.sub(r"[ \t\r\f\v\xa0]+", " ", t).replace(" \n", "\n").strip()

    def _emit(self, s, quote=True):
        if s is None or not str(s).strip():
            return
        s = str(s).rstrip()
        if quote and self.quote:
            s = "\n".join(("> " * self.quote) + ln if ln else ("> " * self.quote).rstrip() for ln in s.split("\n"))
        self.out.append(s)

    def _flush_para(self):
        t = self._text()
        if t:
            self._emit(t)

    def _list_kind(self):
        if self.div_style:
            style = self.div_style[-1][0]
            if style == "6":
                return "ol"
            if style == "7":
                return "check"
            if style == "5":
                return "ul"
        return None

    def _indent(self):
        if self.div_style and self.div_style[-1][1] is not None:
            return self.div_style[-1][1]
        return max(len(self.list_style) - 1, 0)

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_para()
            self.heading = int(tag[1])
        elif tag == "p":
            if self.cell is None and not self.li:
                self._flush_para()
            elif self.buf and not "".join(self.buf).endswith((" ", "\n")):
                self.buf.append(" ")
        elif tag == "br":
            self.buf.append(" " if (self.cell is not None or self.li or self.heading) else "\n")
        elif tag in ("b", "strong"):
            self.buf.append("**")
        elif tag in ("i", "em"):
            self.buf.append("*")
        elif tag == "code":
            self.buf.append("`")
        elif tag in ("a", "lnk"):
            href = a.get("href")
            self.href.append(href)
            if href:
                self.buf.append("[")
        elif tag == "hr":
            self._flush_para()
            self._emit("---", quote=False)
        elif tag == "blockquote":
            self._flush_para()
            self.quote += 1
        elif tag == "div":
            style = a.get("data-section-style")
            m = re.search(r"--indent0:\s*(\d+)", a.get("style") or "")
            self.div_style.append((style, int(m.group(1)) if m else None))
            if style is None and not self.li and self.cell is None:
                self._flush_para()
        elif tag in ("ul", "ol"):
            if self.li and self.li[-1]["buf_started"]:
                self._end_item_text()
            kind = self._list_kind() or ("ol" if tag == "ol" else "ul")
            self.list_style.append([kind, 0])
        elif tag == "li":
            self._flush_para() if not self.li else None
            kind, counter = self.list_style[-1] if self.list_style else ("ul", 0)
            try:
                self.list_style[-1][1] = int(a["value"]) if a.get("value") else self.list_style[-1][1] + 1
            except (KeyError, ValueError, IndexError):
                pass
            checked = "checked" in (a.get("class") or "")
            self.li.append({"kind": kind, "n": self.list_style[-1][1] if self.list_style else 1,
                            "indent": self._indent(), "checked": checked, "buf_started": True, "emitted": False})
            self.buf = []
        elif tag == "table":
            self._flush_para()
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.table.append([])
        elif tag in ("td", "th") and self.table is not None:
            self.cell = True
            self.buf = []

    def _end_item_text(self):
        it = self.li[-1]
        t = self._text()
        if t and not it["emitted"]:
            pad = "  " * it["indent"]
            if it["kind"] == "ol":
                marker = f"{it['n']}. "
            elif it["kind"] == "check":
                marker = "- [x] " if it["checked"] else "- [ ] "
            else:
                marker = "- "
            self._emit(pad + marker + t)
            it["emitted"] = True
        it["buf_started"] = False

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            t = self._text()
            if t:
                self._emit("#" * int(tag[1]) + " " + t)
            self.heading = None
        elif tag == "p":
            if self.cell is None and not self.li:
                self._flush_para()
        elif tag in ("b", "strong"):
            self._close_inline("**")
        elif tag in ("i", "em"):
            self._close_inline("*")
        elif tag == "code":
            self.buf.append("`")
        elif tag in ("a", "lnk"):
            href = self.href.pop() if self.href else None
            if href:
                self.buf.append(f"]({href})")
        elif tag == "blockquote":
            self._flush_para()
            self.quote = max(self.quote - 1, 0)
        elif tag == "div":
            if self.div_style:
                style = self.div_style.pop()[0]
                if style is None and not self.li and self.cell is None:
                    self._flush_para()
        elif tag in ("ul", "ol"):
            if self.list_style:
                self.list_style.pop()
        elif tag == "li":
            if self.li:
                if self.li[-1]["buf_started"]:
                    self._end_item_text()
                self.li.pop()
        elif tag in ("td", "th") and self.table is not None:
            t = self._text().replace("|", "\\|").replace("\n", " ")
            if self.table:
                self.table[-1].append(t)
            self.cell = None
        elif tag == "table":
            self._emit_table()
            self.table = None

    def _close_inline(self, mark):
        # avoid "** **" or "****" for empty emphasis, and keep the marker tight to the text
        if self.buf and self.buf[-1] == mark:
            self.buf.pop()
            return
        self.buf.append(mark)

    def _emit_table(self):
        rows = [r for r in (self.table or []) if any(c.strip() for c in r)]
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        self._emit("\n".join(lines))

    def handle_data(self, data):
        self.buf.append(data)

    def markdown(self):
        self._flush_para()
        # join list items of the same run tightly, everything else with a blank line
        text, prev = [], None
        for blk in self.out:
            is_item = bool(re.match(r"^\s*(?:> )*(?:- |\d+\. )", blk)) and "\n" not in blk
            if text and is_item and prev == "item":
                text[-1] += "\n" + blk
            else:
                text.append(blk)
            prev = "item" if is_item else "block"
        return "\n\n".join(text).strip() + "\n"


def html_to_markdown(html_text):
    p = CanvasToMarkdown()
    p.feed(html_text)
    p.close()
    return p.markdown()


# ---------------------------------------------------------------- plumbing
def log(msg):
    os.makedirs(os.path.dirname(STATE_LOG), exist_ok=True)
    with open(STATE_LOG, "a") as f:
        f.write(f"{datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}\n")


def read_token():
    if os.environ.get("SLACK_TOKEN"):
        return os.environ["SLACK_TOKEN"]
    try:
        with open(f"{HOME}/.env") as f:
            for line in f:
                line = line.strip()
                if line.startswith("SLACK_TOKEN=") and len(line) > len("SLACK_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return ""


def api(token, method, params):
    url = f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('error')}")
    return data


def download(token, url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def load_versions():
    try:
        with open(VERSIONS) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"documents": {}, "announced": {}, "last_run": None, "last_status": "never run"}


def save_versions(v):
    tmp = VERSIONS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(v, f, indent=1, sort_keys=True)
    os.replace(tmp, VERSIONS)


def file_version(text):
    m = VERSION_RE.search(text or "")
    return m.group(1) if m else None


def parent_line(state, text):
    with open(PARENT, "a") as f:
        f.write(f"{state} [key=rules-sync] [at={int(time.time())}]: {text}\n")


def rerender_board():
    subprocess.run(["sudo", "-n", "systemctl", "start", C.get("board_service")], check=False, timeout=60)


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def vt(s):
    return tuple(int(x) for x in s.split("."))


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--convert":  # offline: convert one HTML file to stdout
        sys.stdout.write(html_to_markdown(open(sys.argv[2]).read()))
        return 0
    now_s = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    v = load_versions()
    for fid, name, label in CANVASES:
        rec = v["documents"].setdefault(name, {"label": label, "file_id": fid})
        rec["label"], rec["file_id"] = label, fid
        try:
            with open(f"{RULES}/{name}") as f:
                rec["version"] = file_version(f.read())
        except FileNotFoundError:
            rec.setdefault("version", None)
    token = read_token()
    if not token:
        v["last_run"], v["last_status"] = now_s, "token not configured"
        save_versions(v)
        print("token not configured")
        return 0
    changed, errors = [], []
    for fid, name, label in CANVASES:
        rec = v["documents"][name]
        try:
            info = api(token, "files.info", {"file": fid})["file"]
            rec["title"] = info.get("title")
            rec["slack_updated"] = info.get("updated") or info.get("timestamp")
            url = info.get("url_private_download") or info.get("url_private")
            if not url:
                raise RuntimeError("files.info returned no download url")
            raw = download(token, url)
            if not raw.strip():
                raise RuntimeError("empty canvas body")
            is_html = "quip-canvas-content" in raw[:400] or raw.lstrip().startswith("<")
            md = html_to_markdown(raw) if is_html else raw
            if not md.strip():
                raise RuntimeError("conversion produced no text")
            path = f"{RULES}/{name}"
            html_path = path[:-3] + ".html"
            old = open(path).read() if os.path.exists(path) else ""
            rec["last_fetched"] = now_s
            rec["format"] = "html converted to markdown" if is_html else "markdown as served"
            rec["markdown_bytes"] = len(md.encode("utf-8"))
            rec["html_bytes"] = len(raw.encode("utf-8")) if is_html else 0
            new_version = file_version(md)
            if md != old:
                write_atomic(path, md)
                if is_html:
                    write_atomic(html_path, raw)   # raw audit copy of the last change
                changed.append((label, rec.get("version"), new_version))
                rec["last_changed"] = now_s
            rec["version"] = new_version
            rec["status"] = "ok"
        except (urllib.error.URLError, RuntimeError, OSError, KeyError, ValueError) as e:
            rec["status"] = f"error: {e}"
            errors.append(f"{label}: {e}")
    try:
        params = {"channel": CHANNEL, "limit": 200}
        oldest = v["announced"].get("cursor_ts")
        if oldest:
            params["oldest"] = oldest
        hist = api(token, "conversations.history", params)
        newest_ts = oldest
        for m in hist.get("messages", []):
            ts = m.get("ts")
            if ts and (newest_ts is None or float(ts) > float(newest_ts)):
                newest_ts = ts
            am = ANNOUNCE_RE.search(m.get("text") or "")
            if am:
                ver = am.group(1)
                prev = v["announced"].get("version")
                if (prev is None or vt(ver) >= vt(prev)) and ver != prev:
                    v["announced"].update({"version": ver, "seen": now_s, "message_ts": ts})
                    changed.append(("announcement", prev, ver))
        if newest_ts:
            v["announced"]["cursor_ts"] = newest_ts
        v["announced"]["status"] = "ok"
    except (urllib.error.URLError, RuntimeError, OSError, KeyError, ValueError) as e:
        v["announced"]["status"] = f"error: {e}"
        errors.append(f"announcements: {e}")
    ann = v["announced"].get("version")
    lag = []
    if ann:
        for name, rec in v["documents"].items():
            fv = rec.get("version")
            if fv and name in ("1-jira-rules.md", "2-blockers.md", "3-ai-agent-rules.md", "4-decisions.md") and vt(fv) < vt(ann):
                lag.append(f"{rec['label']} v{fv} < announced v{ann}")
    v["lagging"] = lag
    v["last_run"] = now_s
    v["last_status"] = "ok" if not errors else "partial: " + "; ".join(errors)[:300]
    save_versions(v)
    log(f"run status={v['last_status']} changed={len(changed)} lag={len(lag)}")
    if changed:
        parts = [f"{l}: v{o or '?'} -> v{n or '?'}" for l, o, n in changed]
        parent_line("working", "Rules sync: change detected - " + "; ".join(parts)
                    + (" | LAG: " + "; ".join(lag) if lag else "") + " (rules updated, board re-rendered)")
        rerender_board()
    elif lag:
        rerender_board()
    if errors:
        log("errors: " + " | ".join(errors))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # never crash the timer; keep last good copies
        log(f"fatal: {e}")
        print(f"error: {e}", file=sys.stderr)
        sys.exit(0)
