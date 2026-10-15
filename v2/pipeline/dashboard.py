"""Connector to the Fukuhara Research Seminar Dashboard (outputs, monitors, activity, Start/Pause, buffer).

Settings come only from env DASHBOARD_URL and DASHBOARD_API_KEY (fallback: a git-ignored v2/.dashboard.env with
KEY=value lines). The key is never printed, logged or written anywhere else. Every public function is safe to
call without settings and never raises: without a dashboard the agent just runs as before.

Outputs, monitor updates and reports that can't be sent (offline, sleeping Render site, 429/5xx, or the agent
is paused) go to an offline buffer in v2/state/dashboard_buffer/ (one JSON line per item, files copied), which
survives restarts and, in the cloud routine, is committed with the run. flush() resends oldest first.
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

from lib import ROOT, iso, log

BUF_DIR = ROOT / "state" / "dashboard_buffer"
BUF_FILE = BUF_DIR / "pending.jsonl"
MAX_ITEMS, MAX_BYTES, MAX_UPLOAD = 1000, 100 * 1024 * 1024, 5 * 1024 * 1024
SHORT, LONG = 10, 30
_lock = threading.Lock()
_state = {"paused": False, "every": None, "accepts_messages": False, "accepts_inbox": False,
          "on_message": None, "on_command": None, "on_file": None, "warned": set(), "thread": None}

# ---------------------------------------------------------------- settings
def _settings():
    url, key = os.environ.get("DASHBOARD_URL", ""), os.environ.get("DASHBOARD_API_KEY", "")
    cfg = ROOT / ".dashboard.env"
    if (not url or not key) and cfg.exists():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            v = v.strip().strip("'\"")
            if k.strip() == "DASHBOARD_URL" and not url:
                url = v
            if k.strip() == "DASHBOARD_API_KEY" and not key:
                key = v
    url = url.strip()
    for page in ("/classes", "/login", "/dashboard"):
        if url.endswith(page):
            url = url[: -len(page)]
    url = url.rstrip("/")
    if not url or not key or "<" in url or "<" in key or key.lower() in ("changeme", "your-key"):
        _warn_once("settings", "Dashboard not configured: set DASHBOARD_URL and DASHBOARD_API_KEY (agent runs without it).")
        return None, None
    if not url.startswith(("http://", "https://")):
        _warn_once("scheme", f"DASHBOARD_URL must start with https:// (got {url[:40]!r}); dashboard disabled.")
        return None, None
    host = urllib.parse.urlsplit(url).hostname or ""
    if url.startswith("http://") and host not in ("localhost", "127.0.0.1"):
        _warn_once("http", "DASHBOARD_URL uses plain http://; the key is sent unencrypted. Use https://.")
    return url, key

def enabled() -> bool:
    return _settings()[0] is not None

def _warn_once(tag, msg):
    if tag not in _state["warned"]:
        _state["warned"].add(tag)
        print(f"[dashboard] {msg}")

def _hint(code) -> str:
    return {401: "check DASHBOARD_API_KEY", 404: "check DASHBOARD_URL", 413: "file too big",
            422: "the dashboard rejected the input"}.get(code, "")

# ---------------------------------------------------------------- HTTP
def _call(method, path, *, json_body=None, data=None, files=None, params=None, timeout=SHORT, stream=False):
    """Returns (status, payload|response). status 0 = network failure/timeout. Never follows redirects."""
    url, key = _settings()
    if not url:
        return -1, None
    import requests
    try:
        r = requests.request(method, url + path, headers={"X-API-Key": key}, json=json_body, data=data, files=files,
                             params=params, timeout=timeout, allow_redirects=False, stream=stream)
    except requests.Timeout:
        _warn_once("timeout", "Dashboard timed out (a sleeping Render site takes ~1 min to wake); will retry.")
        return 0, None
    except requests.RequestException as e:
        _warn_once("net", f"Dashboard unreachable ({type(e).__name__}); items are buffered.")
        return 0, None
    if 300 <= r.status_code < 400:
        _warn_once("redirect", f"Dashboard redirected to {r.headers.get('Location', '?')}; set DASHBOARD_URL to that address.")
        return r.status_code, None
    if stream:
        return r.status_code, r
    try:
        payload = r.json()
    except ValueError:
        payload = None
    if r.status_code in (401, 403, 404, 409, 413, 422):
        detail = payload.get("detail") if isinstance(payload, dict) else None
        _warn_once(f"{r.status_code}{path.split('/')[2] if path.count('/') > 1 else path}",
                   f"Dashboard {r.status_code} on {path}: {detail or _hint(r.status_code)}")
    return r.status_code, payload

def _temporary(code) -> bool:
    return code == 0 or code == 429 or code >= 500

# ---------------------------------------------------------------- offline buffer
def _load():
    if not BUF_FILE.exists():
        return []
    out = []
    for line in BUF_FILE.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out

def _save(items):
    BUF_DIR.mkdir(parents=True, exist_ok=True)
    # one monitor value per name (newest wins); cap the buffer, oldest dropped
    seen, kept = set(), []
    for it in reversed(items):
        if it["kind"] in ("monitor", "monitor_file"):
            if it["name"] in seen:
                _drop_file(it)
                continue
            seen.add(it["name"])
        kept.append(it)
    kept.reverse()
    while len(kept) > MAX_ITEMS or sum(it.get("size", 0) for it in kept) > MAX_BYTES:
        print("[dashboard] buffer full; dropping the oldest item")
        _drop_file(kept.pop(0))
    BUF_FILE.write_text("".join(json.dumps(it, ensure_ascii=False) + "\n" for it in kept), encoding="utf-8")

def _drop_file(it):
    if it.get("file"):
        try:
            (BUF_DIR / it["file"]).unlink()
        except OSError:
            pass

def _enqueue(item):
    with _lock:
        items = _load()
        items.append(item)
        _save(items)

def pending() -> int:
    return len(_load())

def _send(it) -> int:
    """Send one buffered/new item. Returns HTTP status (0 = network)."""
    k = it["kind"]
    if k == "output":
        return _call("POST", "/api/outputs", json_body={"category": it["category"], "content": it["content"], "created_at": it["created_at"]})[0]
    if k == "monitor":
        return _call("PUT", "/api/monitors", json_body={"name": it["name"], "content": it["content"], "created_at": it["created_at"]})[0]
    if k == "event":
        return _call("POST", "/api/events", json_body={"message": it["message"], "type": it["type"], "data": it.get("data"),
                                                        "created_at": it["created_at"]})[0]
    if k in ("output_file", "monitor_file"):
        p = BUF_DIR / it["file"]
        if not p.exists():
            return 422
        field = ("category", it["category"]) if k == "output_file" else ("name", it["name"])
        with open(p, "rb") as f:
            return _call("POST", "/api/outputs/file" if k == "output_file" else "/api/monitors/file",
                         data={field[0]: field[1], "created_at": it["created_at"]},
                         files={"file": (it["filename"], f, mimetypes.guess_type(it["filename"])[0] or "application/octet-stream")},
                         timeout=LONG)[0]
    return 422

def flush() -> int:
    """Send everything waiting, oldest first; stop at the first temporary failure. Returns items still waiting."""
    if not enabled():
        return pending()
    with _lock:
        items = _load()
        while items:
            it = items[0]
            if _state["paused"] and it["kind"] != "event":
                break  # outputs and monitors are held while paused
            code = _send(it)
            if _temporary(code):
                break
            if code >= 400 or code < 0:
                print(f"[dashboard] dropped a {it['kind']} that cannot be sent ({code} {_hint(code)})")
            _drop_file(it)
            items.pop(0)
        _save(items)
        return len(items)

def _deliver(item) -> bool:
    """Send now if nothing is waiting ahead of it (and not paused); otherwise queue behind the buffer."""
    if not enabled():
        _enqueue(item)
        return False
    held = _state["paused"] and item["kind"] != "event"
    if held or pending():
        _enqueue(item)
        if not held:
            flush()
        return False
    code = _send(item)
    if _temporary(code):
        _enqueue(item)
        return False
    if item.get("file"):
        _drop_file(item)
    return 200 <= code < 300

def _file_item(kind, src, key, val):
    """Copy a file (path, open file or bytes) into the buffer folder; None if missing or > 5 MB."""
    BUF_DIR.mkdir(parents=True, exist_ok=True)
    if isinstance(src, (bytes, bytearray)):
        data, name = bytes(src), f"{key}.bin"
    elif hasattr(src, "read"):
        data, name = src.read(), Path(getattr(src, "name", "file.bin")).name
    else:
        p = Path(src)
        if not p.is_file():
            return None
        if p.stat().st_size > MAX_UPLOAD:
            print(f"[dashboard] {p.name} is over 5 MB; not sent")
            return None
        data, name = p.read_bytes(), p.name
    if len(data) > MAX_UPLOAD:
        print(f"[dashboard] {name} is over 5 MB; not sent")
        return None
    stored = f"{int(time.time() * 1000)}_{Path(name).name}"
    (BUF_DIR / stored).write_bytes(data)
    return {"kind": kind, key: val, "file": stored, "filename": Path(name).name, "size": len(data), "created_at": _now()}

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

# ---------------------------------------------------------------- public API: outputs, monitors, reports
def output(content, category="results") -> bool:
    """Like print(): send a one-off result to the dashboard's Outputs. Never raises."""
    try:
        if _is_file(content):
            it = _file_item("output_file", content, "category", category)
            return _deliver(it) if it else False
        if isinstance(content, str):
            content = content[:20000]
        return _deliver({"kind": "output", "category": category[:50], "content": _jsonable(content), "created_at": _now()})
    except Exception as e:  # never crash the agent
        print(f"[dashboard] output failed: {type(e).__name__}")
        return False

def set_monitor(content, name="Monitor") -> bool:
    """Replace the value of a named monitor (None removes it). Never raises."""
    try:
        if content is not None and _is_file(content):
            it = _file_item("monitor_file", content, "name", name[:50])
            return _deliver(it) if it else False
        if isinstance(content, str):
            content = content[:20000]
        return _deliver({"kind": "monitor", "name": name[:50], "content": _jsonable(content), "created_at": _now()})
    except Exception as e:
        print(f"[dashboard] set_monitor failed: {type(e).__name__}")
        return False

_last_event = [0.0]

def report(message, type="log", data=None) -> bool:
    """One-line activity entry (log / status / error). Not held while paused. Never raises."""
    try:
        wait = 1.0 - (time.time() - _last_event[0])
        if wait > 0:
            time.sleep(wait)  # ≤ about one event per second
        _last_event[0] = time.time()
        return _deliver({"kind": "event", "message": str(message)[:500], "type": type if type in ("log", "status", "error") else "log",
                         "data": data, "created_at": _now()})
    except Exception as e:
        print(f"[dashboard] report failed: {e.__class__.__name__}")
        return False

# ---------------------------------------------------------------- check-ins, Start/Pause, messages
def check_in(every=None) -> dict:
    """One check-in: POST /api/state, then handle waiting commands. Returns the state reply ({} if unreachable)."""
    if every:
        _state["every"] = int(max(1, min(604800, every)))
    if not enabled():
        return {}
    body = {"accepts_messages": _state["accepts_messages"], "accepts_inbox": _state["accepts_inbox"]}
    if _state["every"]:
        body["check_in_every"] = _state["every"]
    code, st = _call("POST", "/api/state", json_body=body, timeout=LONG)
    if code != 200 or not isinstance(st, dict):
        return {}
    _set_paused(bool(st.get("paused")))
    _handle_commands()
    if st.get("inbox_waiting") and _state["on_file"]:
        _take_inbox()
    if not _state["paused"]:
        flush()
    return st

def _set_paused(p: bool):
    if p != _state["paused"]:
        _state["paused"] = p
        report("Paused" if p else "Started", "status")

def _handle_commands():
    code, cmds = _call("GET", "/api/commands", timeout=LONG)
    if code != 200 or not isinstance(cmds, list):
        return
    for c in cmds:
        try:
            kind = c.get("command")
            if kind in ("start", "pause"):
                _set_paused(kind == "pause")
                if _state["on_command"]:
                    _state["on_command"](kind)
            elif kind == "message" and _state["on_message"]:
                _state["on_message"]((c.get("payload") or {}).get("text", ""))
        except Exception as e:
            report(f"Command {c.get('command')} failed: {type(e).__name__}", "error")
        finally:
            _call("PATCH", f"/api/commands/{c.get('id')}", json_body={"status": "done"})

def start_check_ins(every=60):
    """Background check-ins (daemon thread) for long-running use."""
    _state["every"] = every
    if _state["thread"]:
        return
    def loop():
        while True:
            check_in()
            time.sleep(max(5, min(every, 300)))
    _state["thread"] = threading.Thread(target=loop, daemon=True)
    _state["thread"].start()

def is_paused() -> bool:
    return _state["paused"]

def wait_if_paused(poll=5, max_wait=None):
    t0 = time.time()
    while _state["paused"] and (max_wait is None or time.time() - t0 < max_wait):
        time.sleep(poll)
        check_in()

def accept_messages(on_message):
    _state["on_message"], _state["accepts_messages"] = on_message, True
    check_in()

def on_command(handler):
    _state["on_command"] = handler

# ---------------------------------------------------------------- Inbox (opt-in)
def accept_inbox(on_file):
    _state["on_file"], _state["accepts_inbox"] = on_file, True
    check_in()

def inbox() -> list:
    code, items = _call("GET", "/api/inbox", timeout=LONG)
    return items if code == 200 and isinstance(items, list) else []

def download_inbox(item, save_to=None):
    code, r = _call("GET", f"/api/inbox/{item['id']}/file", timeout=LONG, stream=True)
    if code != 200 or r is None:
        return None
    name = Path(item.get("filename") or "upload.bin").name
    dest = Path(save_to) / name if save_to else Path(tempfile.mkdtemp()) / name
    with open(dest, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    return dest

def confirm_inbox(item) -> bool:
    return _call("POST", f"/api/inbox/{item['id']}/received")[0] == 200

def _take_inbox():
    for it in inbox():
        path = download_inbox(it)
        if not path:
            continue
        try:
            if _state["on_file"](it, str(path)) is not False:
                confirm_inbox(it)
        except Exception as e:
            report(f"Inbox file {it.get('filename')} not stored: {type(e).__name__}", "error")
        finally:
            shutil.rmtree(path.parent, ignore_errors=True)

# ---------------------------------------------------------------- reading and deleting (same class only)
def _agent_path(agent):
    return "me" if agent in (None, "me") else urllib.parse.quote(str(agent), safe="")

def agents() -> list:
    code, r = _call("GET", "/api/agents")
    return r if code == 200 else []

def read_agent(agent="me"):
    code, r = _call("GET", f"/api/agents/{_agent_path(agent)}")
    return r if code == 200 else None

def read_outputs(agent="me", category=None, after=None, before=None, limit=50, offset=0) -> list:
    params = {k: v for k, v in {"category": category, "after": after, "before": before, "limit": limit, "offset": offset}.items() if v is not None}
    code, r = _call("GET", f"/api/agents/{_agent_path(agent)}/outputs", params=params)
    return r if code == 200 else []

def read_activity(agent="me", kind=None, after=None, before=None, limit=50, offset=0) -> list:
    params = {k: v for k, v in {"type": kind, "after": after, "before": before, "limit": limit, "offset": offset}.items() if v is not None}
    code, r = _call("GET", f"/api/agents/{_agent_path(agent)}/events", params=params)
    return r if code == 200 else []

def read_monitors(agent="me", names=None) -> list:
    code, r = _call("GET", f"/api/agents/{_agent_path(agent)}/monitors", params={"name": names} if names else None)
    return r if code == 200 else []

def read_monitor(agent="me", name="Monitor"):
    ms = read_monitors(agent, [name])
    return ms[0] if ms else None

def download(item, save_to=None):
    url = (item.get("file") or {}).get("url")
    if not url:
        return None
    path = urllib.parse.urlsplit(url).path if url.startswith("http") else url
    code, r = _call("GET", path, timeout=LONG, stream=True)
    if code != 200 or r is None:
        return None
    if save_to:
        dest = Path(save_to) / Path(item["file"].get("filename") or "file.bin").name
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
        return dest
    return r.content

def delete_output(item) -> bool:
    return _call("DELETE", f"/api/outputs/{item['id'] if isinstance(item, dict) else item}")[0] == 200

def delete_outputs(category=None, before=None, everything=False) -> int:
    if not (category or before or everything):
        print("[dashboard] refusing to delete outputs without a filter (pass everything=True)")
        return 0
    params = {"all": "true"} if everything else {k: v for k, v in {"category": category, "before": before}.items() if v}
    code, r = _call("DELETE", "/api/outputs", params=params)
    return (r or {}).get("deleted", 0) if code == 200 else 0

def delete_event(item) -> bool:
    return _call("DELETE", f"/api/events/{item['id'] if isinstance(item, dict) else item}")[0] == 200

def delete_activity(kind=None, before=None, everything=False) -> int:
    if not (kind or before or everything):
        print("[dashboard] refusing to delete activity without a filter (pass everything=True)")
        return 0
    params = {"all": "true"} if everything else {k: v for k, v in {"type": kind, "before": before}.items() if v}
    code, r = _call("DELETE", "/api/events", params=params)
    return (r or {}).get("deleted", 0) if code == 200 else 0

def delete_monitors(name=None, everything=False) -> int:
    if not (name or everything):
        print("[dashboard] refusing to delete monitors without a name (pass everything=True)")
        return 0
    code, r = _call("DELETE", "/api/monitors", params={"all": "true"} if everything else {"name": name})
    return (r or {}).get("deleted", 0) if code == 200 else 0
