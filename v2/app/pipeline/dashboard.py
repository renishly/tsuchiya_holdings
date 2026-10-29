"""Connector to the Fukuhara Research Seminar Dashboard (outputs, monitors, activity, Start/Pause, buffer).

The agent appears on the dashboard as several dashboard agents, one per API key (Addendum A35):
  main       DASHBOARD_API_KEY            pipeline, models (M1-Md), values, reports, changelog
  ca         DASHBOARD_API_KEY_CA         consensus and arbitration (decision models)
  debater_a  DASHBOARD_API_KEY_DEBATER_A  Debater A's positions
  debater_b  DASHBOARD_API_KEY_DEBATER_B  Debater B's positions
All share DASHBOARD_URL. Settings come only from env (fallback: a git-ignored v2/app/.dashboard.env with
KEY=value lines). Keys are never printed, logged or written anywhere else. Every public function is safe to
call without settings and never raises: without a dashboard the agent just runs as before.

Outputs, monitor updates and reports that can't be sent (offline, sleeping Render site, 429/5xx, or the agent
is paused) go to an offline buffer per dashboard agent (main: v2/app/state/dashboard_buffer/, others:
state/dashboard_buffer_<profile>/; one JSON line per item, files copied), which survives restarts and is
committed with the run. flush() resends oldest first.

Agent Skills market (A46): post_skill, skills, read_skill, skill_details, download_skill, delete_skill, set_skill_tree,
read_skill_tree and skill_categories go straight to the dashboard (not held while paused, not buffered).

Module-level functions (output, set_monitor, report, check_in, ...) act on `main`; client(profile) returns the
same API for another dashboard agent.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

from lib import ROOT

PROFILES = {"main": "DASHBOARD_API_KEY", "ca": "DASHBOARD_API_KEY_CA",
            "debater_a": "DASHBOARD_API_KEY_DEBATER_A", "debater_b": "DASHBOARD_API_KEY_DEBATER_B"}
LABELS = {"main": "Tsuchibot Main", "ca": "Tsuchibot C&A", "debater_a": "Tsuchibot Debater A", "debater_b": "Tsuchibot Debater B"}
MAX_ITEMS, MAX_BYTES, MAX_UPLOAD = 1000, 100 * 1024 * 1024, 5 * 1024 * 1024
SHORT, LONG = 10, 30
_last_event = [0.0]  # shared: ≤ about one event per second across all dashboard agents


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _is_file(content):
    return isinstance(content, (bytes, bytearray)) or hasattr(content, "read") or \
        (isinstance(content, Path)) or (isinstance(content, str) and len(content) < 400 and "\n" not in content
                                         and not content.startswith(("http://", "https://")) and Path(content).is_file())

def _jsonable(content):
    if isinstance(content, (int, float, str, list, dict)) or content is None:
        return content
    return str(content)

def _hint(code) -> str:
    return {401: "check the dashboard API key", 404: "check DASHBOARD_URL", 413: "file too big",
            422: "the dashboard rejected the input"}.get(code, "")

def _temporary(code) -> bool:
    return code == 0 or code == 429 or code >= 500


class Client:
    def __init__(self, profile: str = "main"):
        if profile not in PROFILES:
            raise ValueError(f"unknown dashboard profile {profile!r}")
        self.profile, self.key_var = profile, PROFILES[profile]
        self.buf_dir = ROOT / "state" / ("dashboard_buffer" if profile == "main" else f"dashboard_buffer_{profile}")
        self.buf_file = self.buf_dir / "pending.jsonl"
        self._lock = threading.Lock()
        self._state = {"paused": False, "every": None, "accepts_messages": False, "accepts_inbox": False,
                       "on_message": None, "on_command": None, "on_file": None, "warned": set(), "thread": None}

    # ------------------------------------------------------------ settings
    def _settings(self):
        url, key = os.environ.get("DASHBOARD_URL", ""), os.environ.get(self.key_var, "")
        cfg = ROOT / ".dashboard.env"
        if (not url or not key) and cfg.exists():
            for line in cfg.read_text(encoding="utf-8").splitlines():
                k, _, v = line.partition("=")
                v = v.strip().strip("'\"")
                if k.strip() == "DASHBOARD_URL" and not url:
                    url = v
                if k.strip() == self.key_var and not key:
                    key = v
        url = url.strip()
        for page in ("/classes", "/login", "/dashboard"):
            if url.endswith(page):
                url = url[: -len(page)]
        url = url.rstrip("/")
        if not url or not key or "<" in url or "<" in key or key.lower() in ("changeme", "your-key"):
            self._warn_once("settings", f"{LABELS[self.profile]} not configured: set DASHBOARD_URL and {self.key_var} "
                                        "(agent runs without it).")
            return None, None
        if not url.startswith(("http://", "https://")):
            self._warn_once("scheme", f"DASHBOARD_URL must start with https:// (got {url[:40]!r}); dashboard disabled.")
            return None, None
        host = urllib.parse.urlsplit(url).hostname or ""
        if url.startswith("http://") and host not in ("localhost", "127.0.0.1"):
            self._warn_once("http", "DASHBOARD_URL uses plain http://; the key is sent unencrypted. Use https://.")
        return url, key

    def enabled(self) -> bool:
        return self._settings()[0] is not None

    def _warn_once(self, tag, msg):
        if tag not in self._state["warned"]:
            self._state["warned"].add(tag)
            print(f"[dashboard:{self.profile}] {msg}")

    # ------------------------------------------------------------ HTTP
    def _call(self, method, path, *, json_body=None, data=None, files=None, params=None, timeout=SHORT, stream=False):
        """Returns (status, payload|response). status 0 = network failure/timeout. Never follows redirects."""
        url, key = self._settings()
        if not url:
            return -1, None
        import requests
        try:
            r = requests.request(method, url + path, headers={"X-API-Key": key}, json=json_body, data=data, files=files,
                                 params=params, timeout=timeout, allow_redirects=False, stream=stream)
        except requests.Timeout:
            self._warn_once("timeout", "Dashboard timed out (a sleeping Render site takes ~1 min to wake); will retry.")
            return 0, None
        except requests.RequestException as e:
            self._warn_once("net", f"Dashboard unreachable ({type(e).__name__}); items are buffered.")
            return 0, None
        if 300 <= r.status_code < 400:
            self._warn_once("redirect", f"Dashboard redirected to {r.headers.get('Location', '?')}; set DASHBOARD_URL to that address.")
            return r.status_code, None
        if stream:
            return r.status_code, r
        try:
            payload = r.json()
        except ValueError:
            payload = None
        if r.status_code in (401, 403, 404, 409, 413, 422):
            detail = payload.get("detail") if isinstance(payload, dict) else None
            self._warn_once(f"{r.status_code}{path.split('/')[2] if path.count('/') > 1 else path}",
                            f"Dashboard {r.status_code} on {path}: {detail or _hint(r.status_code)}")
        return r.status_code, payload

    # ------------------------------------------------------------ offline buffer
    def _load(self):
        if not self.buf_file.exists():
            return []
        out = []
        for line in self.buf_file.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def _save(self, items):
        self.buf_dir.mkdir(parents=True, exist_ok=True)
        # one monitor value per name (newest wins); cap the buffer, oldest dropped
        seen, kept = set(), []
        for it in reversed(items):
            if it["kind"] in ("monitor", "monitor_file"):
                if it["name"] in seen:
                    self._drop_file(it)
                    continue
                seen.add(it["name"])
            kept.append(it)
        kept.reverse()
        while len(kept) > MAX_ITEMS or sum(it.get("size", 0) for it in kept) > MAX_BYTES:
            print(f"[dashboard:{self.profile}] buffer full; dropping the oldest item")
            self._drop_file(kept.pop(0))
        self.buf_file.write_text("".join(json.dumps(it, ensure_ascii=False) + "\n" for it in kept), encoding="utf-8")

    def _drop_file(self, it):
        if it.get("file"):
            try:
                (self.buf_dir / it["file"]).unlink()
            except OSError:
                pass

    def _enqueue(self, item):
        with self._lock:
            items = self._load()
            items.append(item)
            self._save(items)

    def pending(self) -> int:
        return len(self._load())

    def _send(self, it) -> int:
        """Send one buffered/new item. Returns HTTP status (0 = network)."""
        k = it["kind"]
        if k == "output":
            return self._call("POST", "/api/outputs", json_body={"category": it["category"], "content": it["content"],
                                                                  "created_at": it["created_at"]})[0]
        if k == "monitor":
            return self._call("PUT", "/api/monitors", json_body={"name": it["name"], "content": it["content"],
                                                                  "created_at": it["created_at"]})[0]
        if k == "event":
            return self._call("POST", "/api/events", json_body={"message": it["message"], "type": it["type"],
                                                                 "data": it.get("data"), "created_at": it["created_at"]})[0]
        if k in ("output_file", "monitor_file"):
            p = self.buf_dir / it["file"]
            if not p.exists():
                return 422
            field = ("category", it["category"]) if k == "output_file" else ("name", it["name"])
            with open(p, "rb") as f:
                return self._call("POST", "/api/outputs/file" if k == "output_file" else "/api/monitors/file",
                                  data={field[0]: field[1], "created_at": it["created_at"]},
                                  files={"file": (it["filename"], f, mimetypes.guess_type(it["filename"])[0] or "application/octet-stream")},
                                  timeout=LONG)[0]
        return 422

    def flush(self) -> int:
        """Send everything waiting, oldest first; stop at the first temporary failure. Returns items still waiting."""
        if not self.enabled():
            return self.pending()
        with self._lock:
            items = self._load()
            while items:
                it = items[0]
                if self._state["paused"] and it["kind"] != "event":
                    break  # outputs and monitors are held while paused
                code = self._send(it)
                if _temporary(code):
                    break
                if code >= 400 or code < 0:
                    print(f"[dashboard:{self.profile}] dropped a {it['kind']} that cannot be sent ({code} {_hint(code)})")
                self._drop_file(it)
                items.pop(0)
            self._save(items)
            return len(items)

    def _deliver(self, item) -> bool:
        """Send now if nothing is waiting ahead of it (and not paused); otherwise queue behind the buffer."""
        if not self.enabled():
            self._enqueue(item)
            return False
        held = self._state["paused"] and item["kind"] != "event"
        if held or self.pending():
            self._enqueue(item)
            if not held:
                self.flush()
            return False
        code = self._send(item)
        if _temporary(code):
            self._enqueue(item)
            return False
        if item.get("file"):
            self._drop_file(item)
        return 200 <= code < 300

    def _file_item(self, kind, src, key, val):
        """Copy a file (path, open file or bytes) into the buffer folder; None if missing or > 5 MB."""
        self.buf_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(src, (bytes, bytearray)):
            data, name = bytes(src), f"{key}.bin"
        elif hasattr(src, "read"):
            data, name = src.read(), Path(getattr(src, "name", "file.bin")).name
        else:
            p = Path(src)
            if not p.is_file():
                return None
            if p.stat().st_size > MAX_UPLOAD:
                print(f"[dashboard:{self.profile}] {p.name} is over 5 MB; not sent")
                return None
            data, name = p.read_bytes(), p.name
        if len(data) > MAX_UPLOAD:
            print(f"[dashboard:{self.profile}] {name} is over 5 MB; not sent")
            return None
        stored = f"{int(time.time() * 1000)}_{Path(name).name}"
        (self.buf_dir / stored).write_bytes(data)
        return {"kind": kind, key: val, "file": stored, "filename": Path(name).name, "size": len(data), "created_at": _now()}

    # ------------------------------------------------------------ public API: outputs, monitors, reports
    def output(self, content, category="results") -> bool:
        """Like print(): send a one-off result to the dashboard's Outputs. Never raises."""
        try:
            if _is_file(content):
                it = self._file_item("output_file", content, "category", category)
                return self._deliver(it) if it else False
            if isinstance(content, str):
                content = content[:20000]
            return self._deliver({"kind": "output", "category": category[:50], "content": _jsonable(content), "created_at": _now()})
        except Exception as e:  # never crash the agent
            print(f"[dashboard:{self.profile}] output failed: {type(e).__name__}")
            return False

    def set_monitor(self, content, name="Monitor") -> bool:
        """Replace the value of a named monitor (None removes it). Never raises."""
        try:
            if content is not None and _is_file(content):
                it = self._file_item("monitor_file", content, "name", name[:50])
                return self._deliver(it) if it else False
            if isinstance(content, str):
                content = content[:20000]
            return self._deliver({"kind": "monitor", "name": name[:50], "content": _jsonable(content), "created_at": _now()})
        except Exception as e:
            print(f"[dashboard:{self.profile}] set_monitor failed: {type(e).__name__}")
            return False

    def report(self, message, type="log", data=None) -> bool:
        """One-line activity entry (log / status / error). Not held while paused. Never raises."""
        try:
            wait = 1.0 - (time.time() - _last_event[0])
            if wait > 0:
                time.sleep(wait)  # ≤ about one event per second
            _last_event[0] = time.time()
            return self._deliver({"kind": "event", "message": str(message)[:500],
                                  "type": type if type in ("log", "status", "error") else "log",
                                  "data": data, "created_at": _now()})
        except Exception as e:
            print(f"[dashboard:{self.profile}] report failed: {e.__class__.__name__}")
            return False

    # ------------------------------------------------------------ check-ins, Start/Pause, messages
    def check_in(self, every=None) -> dict:
        """One check-in: POST /api/state, then handle waiting commands. Returns the state reply ({} if unreachable)."""
        if every:
            self._state["every"] = int(max(1, min(604800, every)))
        if not self.enabled():
            return {}
        body = {"accepts_messages": self._state["accepts_messages"], "accepts_inbox": self._state["accepts_inbox"]}
        if self._state["every"]:
            body["check_in_every"] = self._state["every"]
        code, st = self._call("POST", "/api/state", json_body=body, timeout=LONG)
        if code != 200 or not isinstance(st, dict):
            return {}
        self._set_paused(bool(st.get("paused")))
        self._handle_commands()
        if st.get("inbox_waiting") and self._state["on_file"]:
            self._take_inbox()
        if not self._state["paused"]:
            self.flush()
        return st

    def _set_paused(self, p: bool):
        if p != self._state["paused"]:
            self._state["paused"] = p
            self.report("Paused" if p else "Started", "status")

    def _handle_commands(self):
        code, cmds = self._call("GET", "/api/commands", timeout=LONG)
        if code != 200 or not isinstance(cmds, list):
            return
        for c in cmds:
            try:
                kind = c.get("command")
                if kind in ("start", "pause"):
                    self._set_paused(kind == "pause")
                    if self._state["on_command"]:
                        self._state["on_command"](kind)
                elif kind == "message" and self._state["on_message"]:
                    self._state["on_message"]((c.get("payload") or {}).get("text", ""))
            except Exception as e:
                self.report(f"Command {c.get('command')} failed: {type(e).__name__}", "error")
            finally:
                self._call("PATCH", f"/api/commands/{c.get('id')}", json_body={"status": "done"})

    def start_check_ins(self, every=60):
        """Background check-ins (daemon thread) for long-running use."""
        self._state["every"] = every
        if self._state["thread"]:
            return
        def loop():
            while True:
                self.check_in()
                time.sleep(max(5, min(every, 300)))
        self._state["thread"] = threading.Thread(target=loop, daemon=True)
        self._state["thread"].start()

    def is_paused(self) -> bool:
        return self._state["paused"]

    def wait_if_paused(self, poll=5, max_wait=None):
        t0 = time.time()
        while self._state["paused"] and (max_wait is None or time.time() - t0 < max_wait):
            time.sleep(poll)
            self.check_in()

    def accept_messages(self, on_message):
        self._state["on_message"], self._state["accepts_messages"] = on_message, True
        self.check_in()

    def on_command(self, handler):
        self._state["on_command"] = handler

    # ------------------------------------------------------------ Inbox (opt-in)
    def accept_inbox(self, on_file):
        self._state["on_file"], self._state["accepts_inbox"] = on_file, True
        self.check_in()

    def inbox(self) -> list:
        code, items = self._call("GET", "/api/inbox", timeout=LONG)
        return items if code == 200 and isinstance(items, list) else []

    def download_inbox(self, item, save_to=None):
        code, r = self._call("GET", f"/api/inbox/{item['id']}/file", timeout=LONG, stream=True)
        if code != 200 or r is None:
            return None
        name = Path(item.get("filename") or "upload.bin").name
        dest = Path(save_to) / name if save_to else Path(tempfile.mkdtemp()) / name
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
        return dest

    def confirm_inbox(self, item) -> bool:
        return self._call("POST", f"/api/inbox/{item['id']}/received")[0] == 200

    def _take_inbox(self):
        for it in self.inbox():
            path = self.download_inbox(it)
            if not path:
                continue
            try:
                if self._state["on_file"](it, str(path)) is not False:
                    self.confirm_inbox(it)
            except Exception as e:
                self.report(f"Inbox file {it.get('filename')} not stored: {type(e).__name__}", "error")
            finally:
                shutil.rmtree(path.parent, ignore_errors=True)

    # ------------------------------------------------------------ reading and deleting (same class only)
    @staticmethod
    def _agent_path(agent):
        return "me" if agent in (None, "me") else urllib.parse.quote(str(agent), safe="")

    def agents(self) -> list:
        code, r = self._call("GET", "/api/agents")
        return r if code == 200 else []

    def read_agent(self, agent="me"):
        code, r = self._call("GET", f"/api/agents/{self._agent_path(agent)}")
        return r if code == 200 else None

    def read_outputs(self, agent="me", category=None, after=None, before=None, limit=50, offset=0) -> list:
        params = {k: v for k, v in {"category": category, "after": after, "before": before, "limit": limit, "offset": offset}.items() if v is not None}
        code, r = self._call("GET", f"/api/agents/{self._agent_path(agent)}/outputs", params=params)
        return r if code == 200 else []

    def read_activity(self, agent="me", kind=None, after=None, before=None, limit=50, offset=0) -> list:
        params = {k: v for k, v in {"type": kind, "after": after, "before": before, "limit": limit, "offset": offset}.items() if v is not None}
        code, r = self._call("GET", f"/api/agents/{self._agent_path(agent)}/events", params=params)
        return r if code == 200 else []

    def read_monitors(self, agent="me", names=None) -> list:
        code, r = self._call("GET", f"/api/agents/{self._agent_path(agent)}/monitors", params={"name": names} if names else None)
        return r if code == 200 else []

    def read_monitor(self, agent="me", name="Monitor"):
        ms = self.read_monitors(agent, [name])
        return ms[0] if ms else None

    def download(self, item, save_to=None):
        url = (item.get("file") or {}).get("url")
        if not url:
            return None
        path = urllib.parse.urlsplit(url).path if url.startswith("http") else url
        code, r = self._call("GET", path, timeout=LONG, stream=True)
        if code != 200 or r is None:
            return None
        if save_to:
            dest = Path(save_to) / Path(item["file"].get("filename") or "file.bin").name
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
            return dest
        return r.content

    def delete_output(self, item) -> bool:
        return self._call("DELETE", f"/api/outputs/{item['id'] if isinstance(item, dict) else item}")[0] == 200

    def delete_outputs(self, category=None, before=None, everything=False) -> int:
        if not (category or before or everything):
            print(f"[dashboard:{self.profile}] refusing to delete outputs without a filter (pass everything=True)")
            return 0
        params = {"all": "true"} if everything else {k: v for k, v in {"category": category, "before": before}.items() if v}
        code, r = self._call("DELETE", "/api/outputs", params=params)
        return (r or {}).get("deleted", 0) if code == 200 else 0

    def delete_event(self, item) -> bool:
        return self._call("DELETE", f"/api/events/{item['id'] if isinstance(item, dict) else item}")[0] == 200

    def delete_activity(self, kind=None, before=None, everything=False) -> int:
        if not (kind or before or everything):
            print(f"[dashboard:{self.profile}] refusing to delete activity without a filter (pass everything=True)")
            return 0
        params = {"all": "true"} if everything else {k: v for k, v in {"type": kind, "before": before}.items() if v}
        code, r = self._call("DELETE", "/api/events", params=params)
        return (r or {}).get("deleted", 0) if code == 200 else 0

    def delete_monitors(self, name=None, everything=False) -> int:
        if not (name or everything):
            print(f"[dashboard:{self.profile}] refusing to delete monitors without a name (pass everything=True)")
            return 0
        code, r = self._call("DELETE", "/api/monitors", params={"all": "true"} if everything else {"name": name})
        return (r or {}).get("deleted", 0) if code == 200 else 0

    # ------------------------------------------------------------ Agent Skills market (straight to the dashboard; never raise)
    def post_skill(self, content, name=None, summary=None, share="class"):
        """Posts a skill; posting the same name again updates it (next version). Returns the skill or None."""
        body = {k: v for k, v in {"content": content, "name": name, "summary": summary, "share": share}.items() if v is not None}
        code, r = self._call("POST", "/api/skills", json_body=body, timeout=LONG)
        return r if code in (200, 201) else None

    def skills(self, scope="class", query=None, category=None, sort=None, limit=None) -> list:
        params = {k: v for k, v in {"scope": scope, "q": query, "category": category, "sort": sort, "limit": limit}.items() if v is not None}
        code, r = self._call("GET", "/api/skills", params=params)
        return r if code == 200 and isinstance(r, list) else []

    def read_skill(self, ref):
        """The latest text of a skill (read it each time it is needed, so updates arrive). None on failure."""
        code, r = self._call("GET", f"/api/skills/{urllib.parse.quote(str(ref), safe='')}/content", stream=True)
        return r.text if code == 200 and r is not None else None

    def skill_details(self, ref):
        code, r = self._call("GET", f"/api/skills/{urllib.parse.quote(str(ref), safe='')}")
        return r if code == 200 else None

    def download_skill(self, ref, save_to=None):
        code, r = self._call("GET", f"/api/skills/{urllib.parse.quote(str(ref), safe='')}/download", stream=True)
        if code != 200 or r is None:
            return None
        if not save_to:
            return r.text
        name = re.sub(r"[^\w.-]", "_", str(ref)) + ".md"
        dest = Path(save_to) / name
        dest.write_text(r.text, encoding="utf-8")
        return dest

    def delete_skill(self, ref) -> bool:
        return self._call("DELETE", f"/api/skills/{urllib.parse.quote(str(ref), safe='')}")[0] == 200

    def set_skill_tree(self, content) -> bool:
        return self._call("PUT", "/api/skill-tree", json_body={"content": content}, timeout=LONG)[0] == 200

    def read_skill_tree(self, agent_id):
        code, r = self._call("GET", f"/api/skill-trees/{urllib.parse.quote(str(agent_id), safe='')}")
        return r if code == 200 else None

    def skill_categories(self) -> list:
        code, r = self._call("GET", "/api/skill-categories")
        return r if code == 200 else []


# ---------------------------------------------------------------- profiles
_clients: dict[str, Client] = {}

def client(profile: str = "main") -> Client:
    if profile not in _clients:
        _clients[profile] = Client(profile)
    return _clients[profile]

def configured() -> list[str]:
    """Profiles whose key is set (main first)."""
    return [p for p in PROFILES if client(p).enabled()]


# ---------------------------------------------------------------- module-level API = the main dashboard agent
_main = client("main")
enabled, pending, flush = _main.enabled, _main.pending, _main.flush
output, set_monitor, report = _main.output, _main.set_monitor, _main.report
check_in, start_check_ins, is_paused, wait_if_paused = _main.check_in, _main.start_check_ins, _main.is_paused, _main.wait_if_paused
accept_messages, on_command, accept_inbox = _main.accept_messages, _main.on_command, _main.accept_inbox
inbox, download_inbox, confirm_inbox = _main.inbox, _main.download_inbox, _main.confirm_inbox
agents, read_agent, read_outputs, read_activity = _main.agents, _main.read_agent, _main.read_outputs, _main.read_activity
read_monitors, read_monitor, download = _main.read_monitors, _main.read_monitor, _main.download
delete_output, delete_outputs, delete_event = _main.delete_output, _main.delete_outputs, _main.delete_event
delete_activity, delete_monitors = _main.delete_activity, _main.delete_monitors
post_skill, skills, read_skill, skill_details = _main.post_skill, _main.skills, _main.read_skill, _main.skill_details
download_skill, delete_skill, set_skill_tree, read_skill_tree, skill_categories = (
    _main.download_skill, _main.delete_skill, _main.set_skill_tree, _main.read_skill_tree, _main.skill_categories)
