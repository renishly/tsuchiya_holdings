"""Shared runtime for the Tsuchiya Holdings (1840) research-data agent.

v2: these helpers were embedded in v1/SKILL.md; they now live in v2/pipeline/ and are
called by the baseline subagent's tools (v2/subagents/baseline.py) and the orchestrator
(v2/agent.py). Builder edits only; the agent never modifies them.

Every guardrail that can be enforced in code lives here:
  - all writes go through safe_path() (v2/ only, never agent code or prompts, never approvals.md)
  - all network access goes through fetch() (allowlist, robots.txt, rate limits,
    per-run domain budget, loop detection, snapshots with SHA-256, test-mode fixtures)
  - all CSV writes go through CsvStore.upsert() (record_id idempotence,
    locked student rows, restatements kept, 20% change guard)
  - all free text bound for CSVs goes through pii_scrub()
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9), "JST")
ROOT = Path(os.environ.get("TSUCHIYA_ROOT", Path(__file__).resolve().parent.parent)).resolve()
UA = "TsuchiyaResearchAgent/1.0 (academic research; Fukuhara Seminar; read-only, low rate)"

# ---------------------------------------------------------------- configuration
CONFIG = {
    "company_code": "1840",
    "edinet_code": "E00240",
    "company_name": "土屋ホールディングス",
    "fiscal_years": {  # fiscal_term -> period end (FY ends Oct 31)
        "第46期": "2021-10-31", "第47期": "2022-10-31", "第48期": "2023-10-31",
        "第49期": "2024-10-31", "第50期": "2025-10-31",
    },
    "peer_ir": {  # pages whose HTML links the 決算短信 / 業績予想の修正 PDFs directly (Addendum A30)
                "1419": "https://www.tamahome.jp/company/ir/library/library_02.html",
                "1873": "https://www.nihonhouse-hd.co.jp/company/irlist/?y={year}",  # one page per calendar year
                "8999": "https://www.grandy.co.jp/ir/library",
                "1840": "https://www.tsuchiya.co.jp/ir/news/"},
    "peers": [  # Addenda A30-A31 (replace A2; changing the set needs approval)
        {"ticker": "1419", "name": "タマホーム", "fy_end_month": 5},
        {"ticker": "1873", "name": "日本ハウスホールディングス", "fy_end_month": 4},
        {"ticker": "8999", "name": "グランディハウス", "fy_end_month": 3},
    ],
    "budgets": {"max_iterations": 150, "max_minutes": 60, "max_requests_per_domain": 25,
                "max_same_url": 2, "lock_stale_hours": 3, "change_guard_ratio": 0.20},
    "min_interval_s": {"api.edinet-fsa.go.jp": 1.0, "default": 3.0},
}

# Test-only budget overrides (evals shrink limits to exercise budget stops). Ignored outside test fixtures.
if os.environ.get("TSUCHIYA_FIXTURE_SET"):
    for _k, _env in (("max_iterations", "TSUCHIYA_BUDGET_ITER"), ("max_requests_per_domain", "TSUCHIYA_BUDGET_DOMAIN"),
                     ("max_minutes", "TSUCHIYA_BUDGET_MIN")):
        if os.environ.get(_env):
            CONFIG["budgets"][_k] = int(os.environ[_env])

# The only endpoint the agent may POST to: a public search form (no login, no inquiry, no 資料請求).
POST_ALLOW = {"https://www.mlit.go.jp/nega-inf/cgi-bin/search.cgi"}

# Addenda A6, A8, A9, A10. Path prefixes restrict a domain to part of a site.
ALLOWLIST = {
    # disclosure
    "api.edinet-fsa.go.jp": "disclosure", "disclosure2.edinet-fsa.go.jp": "disclosure",
    "disclosure2dl.edinet-fsa.go.jp": "disclosure", "www2.jpx.co.jp": "disclosure",
    # company (Tsuchiya group)
    "www.tsuchiya.co.jp": "company", "www.tsuchiyahome.jp": "company", "www.liznas.jp": "company",
    "www.hometopia.jp": "company", "tsuchiyafudosan.jp": "company", "tsuchiya-tokken.jp": "company",
    "iestdot.com": "company",
    "mylist-v2.realnetpro.com": "company:/22359m1/",
    # archive (group domains only; enforced in wayback helpers)
    "web.archive.org": "archive",
    # market data (Addendum A8): irbank daily close + cross-check; JPX reference pages only
    "irbank.net": "market", "www.jpx.co.jp": "reference",
    # peer IR sites, 決算短信 only (Addendum A9)
    "www.tamahome.jp": "peer-ir", "www.grandy.co.jp": "peer-ir",
    "www.nihonhouse-hd.co.jp": "peer-ir",
    # legal / government
    "www.mlit.go.jp": "legal", "etsuran2.mlit.go.jp": "legal", "laws.e-gov.go.jp": "legal",
    "www.retio.or.jp": "legal", "www.reinfolib.mlit.go.jp": "benchmark",
    # risk-free rate for the factor model: MOF JGB yield CSVs only (Addendum A28)
    "www.mof.go.jp": "rates:/jgbs/reference/interest_rate/",
}
GROUP_DOMAINS = {d for d, k in ALLOWLIST.items() if k.startswith("company")}
NEVER_FETCH = {"www.xj-storage.jp", "myhome.nifty.com", "www.fudousan.or.jp", "www.hatomarksite.com", "suumo.jp",
               "www.homes.co.jp", "www.athome.co.jp", "kabutan.jp", "finance.yahoo.co.jp",
               "minkabu.jp", "www.release.tdnet.info", "quote.jpx.co.jp"}

# ---------------------------------------------------------------- paths & time
def now() -> dt.datetime:
    fixed = os.environ.get("TSUCHIYA_NOW")  # test mode only: deterministic clock for evals
    if fixed:
        return dt.datetime.fromisoformat(fixed).astimezone(JST)
    return dt.datetime.now(JST)

def iso(t: dt.datetime | None = None) -> str:
    return (t or now()).isoformat(timespec="seconds")

# Agent code and standing instructions: the agent never modifies these (v2 replaces "never modify SKILL.md").
PROTECTED_DIRS = ("pipeline", "subagents", "prompts")
PROTECTED_FILES = ("agent.py", "llm.py", "prompt2.org", "requirements.txt", "list_models.py", "README.md")

def safe_path(rel: str | Path) -> Path:
    """Resolve a path under v2/ and refuse anything outside it or protected files."""
    p = (ROOT / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise GuardrailError(f"write outside v2/ refused: {p}")
    if (p.parent == ROOT and p.name in PROTECTED_FILES) or any(ROOT / d in (p, *p.parents) for d in PROTECTED_DIRS):
        raise GuardrailError(f"modifying agent code or instructions is prohibited: {p.relative_to(ROOT)}")
    if p == ROOT / "state" / "approvals.md":
        raise GuardrailError("state/approvals.md is read-only to the agent")
    return p

def write_text(rel, text: str, mode="w"):
    p = safe_path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, mode, encoding="utf-8") as f:
        f.write(text)
    return p

def write_bytes(rel, data: bytes):
    p = safe_path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p

def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

class GuardrailError(Exception):
    pass

class BudgetStop(Exception):
    pass

# ---------------------------------------------------------------- state & logging
STATE_FILE = "state/run_state.json"

def default_state() -> dict:
    return {
        "schema_version": 1,
        "phases": {p: {"status": "pending", "cursor": None, "last_success": None, "fail_streak": 0}
                   for p in ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]},
        "daily": {},            # task -> last JST date run
        "robots": {},           # domain -> {"date":..., "txt":...}
        "blocked_by_policy": {},  # domain -> reason
        "consumed_approvals": [],
        "approved": {},         # approval kind -> details (e.g. sheets_first_write)
        "documents": {},        # discovered documents (P1)
        "current_run": None,
        "last_price": {},
        "history": [],
    }

def load_state() -> dict:
    p = ROOT / STATE_FILE
    if p.exists():
        st = json.loads(p.read_text(encoding="utf-8"))
        base = default_state()
        for k, v in base.items():
            st.setdefault(k, v)
        for ph, v in base["phases"].items():
            st["phases"].setdefault(ph, v)
        return st
    return default_state()

def save_state(st: dict):
    write_text(STATE_FILE, json.dumps(st, ensure_ascii=False, indent=2))

def run_ctx(st: dict) -> dict:
    if not st.get("current_run"):
        raise SystemExit(json.dumps({"error": "no active run; call `agent.py start` first"}))
    return st["current_run"]

_SECRET_PAT = re.compile(r"(Subscription-Key=)[^&\s]+|(private_key[\"']?\s*:\s*[\"'])[^\"']+", re.I)

def redact(s: str) -> str:
    return _SECRET_PAT.sub(lambda m: (m.group(1) or m.group(2)) + "REDACTED", s)

def log(st: dict | None, tool: str, inputs, outcome, **extra):
    rec = {"ts": iso(), "run_id": (st or {}).get("current_run", {}) and st["current_run"].get("run_id"),
           "tool": tool, "inputs": inputs, "outcome": outcome}
    rec.update(extra)
    line = redact(json.dumps(rec, ensure_ascii=False))
    write_text("logs/run_log.jsonl", line + "\n", mode="a")

def tick(st: dict, n: int = 1):
    """Count an iteration and enforce the run's time and iteration budgets."""
    cr = run_ctx(st)
    cr["iterations"] = cr.get("iterations", 0) + n
    started = dt.datetime.fromisoformat(cr["started"])
    minutes = (now() - started).total_seconds() / 60
    b = CONFIG["budgets"]
    if cr["iterations"] >= b["max_iterations"]:
        raise BudgetStop(f"iteration budget reached ({cr['iterations']})")
    if minutes >= b["max_minutes"]:
        raise BudgetStop(f"time budget reached ({minutes:.0f} min)")

# ---------------------------------------------------------------- approvals
AR_PAT = re.compile(r"^(APPROVE|REJECT)\s+(AR-\d{8}-\d{2})\s*$", re.M)

def read_approvals() -> dict:
    """Decisions are read only from unindented lines below the '## Decisions' heading of state/approvals.md."""
    p = ROOT / "state" / "approvals.md"
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    m = re.search(r"(?m)^## Decisions\s*$", text)
    if not m:
        return {}
    return {x.group(2): x.group(1) for x in AR_PAT.finditer(text[m.end():])}

def pending_requests() -> list[dict]:
    p = ROOT / "state" / "pending_approvals.md"
    if not p.exists():
        return []
    out = []
    for block in re.split(r"(?m)^## ", p.read_text(encoding="utf-8"))[1:]:
        head = block.splitlines()[0].strip()
        m = re.match(r"(AR-\d{8}-\d{2})", head)
        if not m:
            continue
        kind = re.search(r"(?m)^- kind: (\S+)", block)
        params = re.search(r"(?m)^- params: (.+)$", block)
        out.append({"id": m.group(1), "kind": kind.group(1) if kind else "",
                    "params": json.loads(params.group(1)) if params else {}, "text": block})
    return out

def queue_approval(st: dict, kind: str, action: str, target: str, params: dict, reason: str) -> str:
    """Append an approval request unless an identical open one exists. Never waits."""
    for r in pending_requests():
        if r["kind"] == kind and r["params"] == params and r["id"] not in st["consumed_approvals"]:
            return r["id"]
    today = now().strftime("%Y%m%d")
    n = sum(1 for r in pending_requests() if r["id"].startswith(f"AR-{today}")) + 1
    ar = f"AR-{today}-{n:02d}"
    p = ROOT / "state" / "pending_approvals.md"
    head = "" if p.exists() else ("# Pending approval requests\n\nApprove by writing `APPROVE <id>` "
                                  "(or `REJECT <id>`) on its own line in state/approvals.md.\n\n")
    write_text("state/pending_approvals.md", head + (
        f"## {ar} — {action}\n- kind: {kind}\n- target: {target}\n"
        f"- params: {json.dumps(params, ensure_ascii=False)}\n- reason: {reason}\n"
        f"- requested: {iso()} (run {st.get('current_run', {}).get('run_id')})\n\n"), mode="a")
    log(st, "approval_request", {"id": ar, "kind": kind, "target": target}, "queued")
    return ar

def approved(st: dict, kind: str, params: dict | None = None) -> str | None:
    """Return an approval id that grants this action (one use, then consumed)."""
    decisions = read_approvals()
    for r in pending_requests():
        if r["kind"] != kind or r["id"] in st["consumed_approvals"]:
            continue
        if params is not None and r["params"] != params:
            continue
        if decisions.get(r["id"]) == "APPROVE":
            return r["id"]
    return None

def consume(st: dict, ar: str):
    st["consumed_approvals"].append(ar)
    log(st, "approval_decision", {"id": ar}, "consumed")

# ---------------------------------------------------------------- network
class FetchError(Exception):
    def __init__(self, code, detail):
        super().__init__(f"{code}: {detail}")
        self.code, self.detail = code, detail

def _domain_ok(st, url: str):
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("https", "http"):
        raise GuardrailError(f"scheme refused: {url}")
    host = u.hostname or ""
    if host in NEVER_FETCH:
        raise GuardrailError(f"{host} forbids automated access (terms/robots); never fetched")
    kind = ALLOWLIST.get(host)
    if not kind:
        raise GuardrailError(f"domain not on allowlist: {host} (queue an approval instead)")
    if ":" in kind:
        prefix = kind.split(":", 1)[1]
        if not u.path.startswith(prefix):
            raise GuardrailError(f"path not allowed on {host}: {u.path}")
    if host == "web.archive.org":
        m = re.search(r"/web/\d+[a-z_]*/(?:https?://)?([^/]+)", u.path) or \
            re.search(r"url=(?:https?://)?([^/&%]+)", urllib.parse.unquote(u.query))
        target = m.group(1) if m else ""
        if target not in GROUP_DOMAINS and target.replace("www.", "") not in {d.replace("www.", "") for d in GROUP_DOMAINS}:
            raise GuardrailError(f"Wayback used only for Tsuchiya group domains, not {target!r}")
    for key, reason in st.get("blocked_by_policy", {}).items():
        if key == host or url.split("?")[0].startswith(key):
            raise GuardrailError(f"{key} is blocked_by_policy: {reason}")
    return host

def _robots_allowed(st, host: str, url: str, session) -> bool:
    if host.startswith("api.") or host == "www.reinfolib.mlit.go.jp":
        return True  # documented APIs
    today = now().date().isoformat()
    rc = st["robots"].get(host)
    if not rc or rc.get("date") != today:
        txt = ""
        try:
            r = session.get(f"https://{host}/robots.txt", timeout=20)
            ctype = r.headers.get("content-type", "")
            txt = r.text if (r.status_code == 200 and "html" not in ctype) else ""
        except Exception:
            txt = ""
        rc = st["robots"][host] = {"date": today, "txt": txt}
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(rc["txt"].splitlines())
    return rp.can_fetch(UA, url) and rp.can_fetch("*", url)

_session = None
_last_hit: dict[str, float] = {}

def _sess():
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
        _session.headers["User-Agent"] = UA
    return _session

def fixture_dir() -> Path:
    """Frozen test sources. v2 reuses v1's fixtures until it has its own evals/ folder."""
    base = ROOT / "evals" / "fixtures"
    if not base.exists():
        base = ROOT.parent / "v1" / "evals" / "fixtures"
    return base / os.environ.get("TSUCHIYA_FIXTURE_SET", "default")

def fixture_index() -> dict:
    p = fixture_dir() / "index.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

def fetch(st: dict, url: str, *, params: dict | None = None, data: dict | None = None,
          purpose: str = "", binary: bool = False, snapshot: bool = True,
          headers: dict | None = None, secret_params: dict | None = None):
    """The only network entry point. Returns (bytes|str, meta). Raises FetchError/GuardrailError."""
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    host = _domain_ok(st, full)
    if data is not None and full not in POST_ALLOW:
        raise GuardrailError(f"form submission refused: POST to {full} (only public search endpoints are allowed)")
    cr = run_ctx(st)
    # Same page already fetched in this run (by another helper or phase): reuse the saved copy.
    cache_key = full if data is None else full + "#POST:" + urllib.parse.urlencode(data)
    cached = cr.setdefault("page_cache", {}).get(cache_key)
    if cached and (ROOT / cached["path"]).exists():
        body = (ROOT / cached["path"]).read_bytes()
        log(st, "fetch", {"url": full, "purpose": purpose}, "reused (already fetched this run)")
        return (body if binary else body.decode(cached.get("encoding") or "utf-8", errors="replace")), dict(cached["meta"])
    counts = cr.setdefault("domain_requests", {})
    url_counts = cr.setdefault("url_requests", {})
    skipped = cr.setdefault("skipped_domains", [])
    if host in skipped:
        raise FetchError("skipped", f"{host} skipped for this run after 3 consecutive failures")
    if url_counts.get(full, 0) >= CONFIG["budgets"]["max_same_url"]:
        log(st, "fetch", {"url": full}, "loop detected: same URL fetched more than twice")
        raise BudgetStop(f"loop detected: {full}")
    if counts.get(host, 0) >= CONFIG["budgets"]["max_requests_per_domain"]:
        raise BudgetStop(f"domain budget reached for {host}")
    tick(st)
    url_counts[full] = url_counts.get(full, 0) + 1
    counts[host] = counts.get(host, 0) + 1

    if cr.get("mode") == "test":
        fx = fixture_index()
        key = full if data is None else full + "#POST:" + urllib.parse.urlencode(data)
        entry = fx.get(key) or (fx.get(url) if data is None else None)
        if entry is None:
            log(st, "fetch", {"url": full, "purpose": purpose}, "fixture missing")
            raise FetchError(404, "no fixture for URL (test mode never uses the network)")
        if entry.get("status", 200) != 200:
            code = entry["status"]
            if code == 429 or (isinstance(code, int) and code >= 500):
                log(st, "fetch", {"url": full}, f"retry after 30s ({code}) [test mode: no wait]")
            _fail(st, host, full, code, entry.get("detail", "fixture error"))
        body = (fixture_dir() / entry["file"]).read_bytes()
        if entry.get("robots_disallow"):
            st["blocked_by_policy"][full.split("?")[0]] = "robots.txt disallow (fixture)"
            log(st, "fetch", {"url": full}, "blocked_by_policy")
            raise GuardrailError(f"robots.txt disallows {full}")
        meta = {"url": full, "status": 200, "fetched": iso(), "sha256": sha256(body), "fixture": entry["file"]}
        cr.setdefault("consecutive_fail", {})[host] = 0
        log(st, "fetch", {"url": full, "purpose": purpose}, "ok (fixture)", sha256=meta["sha256"])
        text = None if binary else body.decode(entry.get("encoding", "utf-8"), errors="replace")
        if text is not None:
            flag_injection(st, full, text)
        _remember(cr, cache_key, body, meta, entry.get("encoding", "utf-8"))
        return (body if binary else text), meta

    s = _sess()
    if not _robots_allowed(st, host, full, s):
        st["blocked_by_policy"][full.split("?")[0]] = f"robots.txt disallow ({iso()[:10]})"
        log(st, "fetch", {"url": full}, "blocked_by_policy: robots.txt disallow")
        raise GuardrailError(f"robots.txt disallows {full}")
    wait = CONFIG["min_interval_s"].get(host, CONFIG["min_interval_s"]["default"])
    last = _last_hit.get(host)
    if last and time.time() - last < wait:
        time.sleep(wait - (time.time() - last))
    real = full
    if secret_params:  # e.g. EDINET Subscription-Key: never logged
        sep = "&" if "?" in real else "?"
        real = real + sep + urllib.parse.urlencode(secret_params)
    for attempt in (1, 2):
        try:
            _last_hit[host] = time.time()
            if data is not None:
                r = s.post(real, data=data, timeout=60, headers=headers or {})
            else:
                r = s.get(real, timeout=60, headers=headers or {})
            if r.status_code == 429 or r.status_code >= 500:
                raise FetchError(r.status_code, "server busy or error")
            if r.status_code != 200:
                _fail(st, host, full, r.status_code, r.reason)
            body = r.content
            if any(k in body[:4000].lower() for k in (b"captcha", b"recaptcha")) and not binary:
                raise GuardrailError(f"CAPTCHA encountered at {full}; source abandoned")
            break
        except GuardrailError:
            raise
        except FetchError as e:
            if e.code not in (429,) and not (isinstance(e.code, int) and e.code >= 500):
                raise
            if attempt == 2:
                _fail(st, host, full, e.code, e.detail)
            log(st, "fetch", {"url": full}, f"retry after 30s ({e.code})")
            time.sleep(30)
        except Exception as e:  # network error
            if attempt == 2:
                _fail(st, host, full, "network", str(e)[:200])
            log(st, "fetch", {"url": full}, f"retry after 30s (network: {str(e)[:80]})")
            time.sleep(30)
    meta = {"url": full, "status": 200, "fetched": iso(), "sha256": sha256(body)}
    cr.setdefault("consecutive_fail", {})[host] = 0
    if os.environ.get("TSUCHIYA_RECORD"):  # builder only: freeze live responses as test fixtures
        _record_fixture(full, body, data, r)
    if snapshot and not binary:
        write_bytes(f"raw/web/{host}/{meta['sha256']}.html", body)
        write_text(f"raw/web/{host}/{meta['sha256']}.json", json.dumps(meta, ensure_ascii=False))
    log(st, "fetch", {"url": full, "purpose": purpose}, "ok", sha256=meta["sha256"])
    enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else (r.apparent_encoding or "utf-8")
    _remember(cr, cache_key, body, meta, enc)
    if binary:
        return body, meta
    text = body.decode(enc, errors="replace")
    flag_injection(st, full, text)
    return text, meta

def _remember(cr, key, body, meta, enc):
    """Keep this run's responses so a repeat request reuses them (no new request, no loop trip)."""
    rel = f"tmp/page_cache/{sha256(key.encode())[:24]}.bin"
    write_bytes(rel, body)
    cr.setdefault("page_cache", {})[key] = {"path": rel, "meta": meta, "encoding": enc}

def _record_fixture(full, body, data, resp):
    fdir = ROOT / "evals" / "fixtures" / os.environ["TSUCHIYA_RECORD"]
    ext = ".pdf" if body[:5] == b"%PDF-" else (".zip" if body[:2] == b"PK" else (".json" if body[:1] in (b"{", b"[") else ".html"))
    name = f"files/{sha256(body)[:16]}{ext}"
    write_bytes(fdir.relative_to(ROOT) / name, body)
    ip = fdir / "index.json"
    idx = json.loads(ip.read_text(encoding="utf-8")) if ip.exists() else {}
    enc = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else (resp.apparent_encoding or "utf-8")
    key = full if data is None else full + "#POST:" + urllib.parse.urlencode(data)
    idx[key] = {"file": name, "encoding": enc, "recorded": iso()}
    write_text(fdir.relative_to(ROOT) / "index.json", json.dumps(idx, ensure_ascii=False, indent=1))

def _fail(st, host, url, code, detail):
    cr = run_ctx(st)
    cf = cr.setdefault("consecutive_fail", {})
    cf[host] = cf.get(host, 0) + 1
    log(st, "fetch", {"url": url}, f"error {code}: {detail}")
    if cf[host] >= 3 and host not in cr.setdefault("skipped_domains", []):
        cr["skipped_domains"].append(host)
        log(st, "fetch", {"domain": host}, "domain skipped for rest of run (3 consecutive failures)")
    raise FetchError(code, detail)

# ---------------------------------------------------------------- prompt-injection screen
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+|the\s+|previous\s+|prior\s+)*(instructions|rules|skill\.md|guardrails)",
    r"skill\.md", r"(^|[\s>])system\s*[:：]", r"\bAPPROVE\s+AR-\d{8}-\d{2}\b",
    r"\b(place|submit|execute)\s+(a\s+)?(buy|sell)\s+order", r"\bbuy\s+order\b", r"(売買|買い|売り|購入)注文",
    r"(ai|AI|ＡＩ)\s*(エージェント|agent)\s*(は|へ|に告ぐ|must|should|,?\s*(please|ignore))", r"\b(this|the)\s+agent\s+(must|should|is\s+instructed)",
    r"(investment\s+)?thesis\s+for\s+the\s+(student|analyst)|draft\s+the\s+analyst'?s\s+thesis|write\s+the\s+student'?s",
    r"(福原)?先生より|from\s+the\s+instructor", r"you\s+are\s+now\b", r"new\s+instructions",
]
_INJ = re.compile("|".join(f"(?:{p})" for p in INJECTION_PATTERNS), re.I)

def injection_hits(text: str) -> list[str]:
    """Instruction-like text aimed at an AI agent. Matches are data to log and drop, never to follow."""
    import html as _h
    plain = re.sub(r"<[^>]+>", " ", _h.unescape(text or ""))
    return [m.group(0)[:60] for m in _INJ.finditer(plain)][:5]

def flag_injection(st, url: str, text: str) -> bool:
    hits = injection_hits(text)
    if hits and st.get("current_run"):
        cr = st["current_run"]
        cr.setdefault("injections", []).append({"url": url, "excerpt": hits[0]})
        cr.setdefault("attention", [])
        log(st, "injection_attempt", {"url": url, "excerpt": hits[0]}, "ignored (instruction-like text in fetched content)")
    return bool(hits)

# ---------------------------------------------------------------- PII filter
PII_PATTERNS = [
    (re.compile(r"0\d{1,4}[-‐－ー(（]\d{1,4}[-‐－ー)）]\d{3,4}"), "[phone removed]"),
    (re.compile(r"(?<!\d)0[5-9]0\d{8}(?!\d)|(?<!\d)0\d{9}(?!\d)"), "[phone removed]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email removed]"),
    (re.compile(r"〒?\s?\d{3}-\d{4}"), "[postcode removed]"),
]

def pii_scrub(text: str) -> tuple[str, bool]:
    if not text:
        return text, False
    out, hit = text, False
    for pat, rep in PII_PATTERNS:
        out, n = pat.subn(rep, out)
        hit = hit or n > 0
    return out, hit

def district_only(address: str) -> tuple[str, str]:
    """Reduce an address to (municipality, district) at 町丁目 level, dropping lot/house numbers."""
    a = re.sub(r"\s+", "", address or "")
    a = re.sub(r"^北海道", "", a)
    a = re.sub(r"(付近|周辺)$", "", a)
    m = re.match(r"(.+?(?:市|郡.+?町|町|村))(.+?区)?(.*)$", a)
    if not m:
        return "", ""
    muni = m.group(1) + (m.group(2) or "")
    rest = m.group(3) or ""
    # keep the town name and 丁目 (kanji or arabic), drop 番/号/digits that follow
    d = re.match(r"([^\d０-９\-－‐]+?(?:[一二三四五六七八九十０-９\d]+丁目)?)(?=[\d０-９\-－‐番]|$)", rest)
    district = d.group(1) if d else ""
    return muni, district

# ---------------------------------------------------------------- CSV store
COMMON = ["value", "unit", "scale", "period_label", "period_start", "period_end", "source_type",
          "source_title", "source_url", "location_in_source", "as_reported_in", "access_date_jst",
          "extraction_method", "agent_status", "validation_status", "correction_note", "run_id"]

SCHEMAS = {
    "Securities Reports": ["record_id", "company_code", "fiscal_term", "statement", "scope",
                           "line_item_ja", "line_item_en", "xbrl_element"] + COMMON,
    "Integrated Reports": ["record_id", "report_title", "section", "line_item_ja", "line_item_en"] + COMMON,
    "Baseline Metrics": ["record_id", "ticker", "company_name", "metric", "basis", "formula",
                         "input_record_ids", "quote_time_jst", "price_type"] + COMMON,
    "Alternative Data": ["record_id", "record_type", "revenue_stream", "category_reason",
                         "listing_id", "property_type", "取引態様", "listing_entity", "price_jpy",
                         "rent_jpy", "land_m2", "floor_m2", "built_year", "municipality", "district",
                         "first_seen", "last_seen", "status", "page_type", "observed_item",
                         "snapshot_date", "publisher", "published_date", "headline", "summary_en",
                         "topic", "mentions_tsuchiya"] + COMMON,
}
LOCKED = {"student-verified", "student-corrected"}
AGENT_STATUS = {"agent-extracted", "student-verified", "student-corrected", "unresolved"}
FREE_TEXT_OK = {"correction_note", "summary_en"}

def record_id(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:20]

class CsvStore:
    """Tidy CSV with UTF-8 BOM, keyed by record_id. The CSVs are the system of record."""

    def __init__(self, name: str):
        self.name = name
        self.cols = SCHEMAS[name]
        self.rel = f"data/{name}.csv"
        self.rows: dict[str, dict] = {}
        self.order: list[str] = []
        p = ROOT / self.rel
        if p.exists():
            with open(p, encoding="utf-8-sig", newline="") as f:
                rd = csv.DictReader(f)
                if rd.fieldnames and list(rd.fieldnames) != self.cols:
                    raise GuardrailError(f"{self.rel} columns differ from schema; schema changes need approval")
                for r in rd:
                    self.rows[r["record_id"]] = r
                    self.order.append(r["record_id"])
        self.initial = len(self.rows)
        # Columns whose change is expected every run (not a parsing regression) and rows that
        # are refreshed by design (current market quotes) do not count toward the 20% guard.
        self.volatile = {"access_date_jst", "run_id", "last_seen", "status", "validation_status", "correction_note"}
        self.guard_exempt = lambda r: r.get("basis") in ("market", "derived-current")
        self.material: list[str] = []
        self.added: list[str] = []
        self.changed: list[str] = []
        self.conflicts: list[str] = []

    def upsert(self, row: dict, run_id: str) -> str:
        unknown = set(row) - set(self.cols)
        if unknown:
            raise GuardrailError(f"unknown columns for {self.name}: {sorted(unknown)} (schema change needs approval)")
        row = {c: ("" if row.get(c) is None else str(row.get(c))) for c in self.cols}
        for c in self.cols:
            if c in FREE_TEXT_OK or c in ("source_url", "source_title"):
                continue
            row[c], _ = pii_scrub(row[c])
        for c in FREE_TEXT_OK:
            if c in row:
                row[c], _ = pii_scrub(row[c])
        if row["agent_status"] not in AGENT_STATUS:
            raise GuardrailError(f"bad agent_status {row['agent_status']!r}")
        if row["value"] == "" and row["agent_status"] != "unresolved" and row["value"] != "0":
            row["agent_status"] = "unresolved"
        if not row["source_url"]:
            raise GuardrailError("every row needs a source_url")
        row["run_id"] = run_id
        rid = row["record_id"]
        old = self.rows.get(rid)
        if old is None:
            self.rows[rid] = row
            self.order.append(rid)
            self.added.append(rid)
            return rid
        if old["agent_status"] in LOCKED:
            if old["value"] != row["value"]:
                crid = record_id(rid, "conflict", row["value"], row["access_date_jst"][:10])
                if crid not in self.rows:
                    c = dict(row, record_id=crid, agent_status="unresolved",
                             correction_note=f"New extraction {row['value']!r} conflicts with locked row {rid} "
                                             f"({old['agent_status']}, value {old['value']!r}); locked row kept.")
                    self.rows[crid] = c
                    self.order.append(crid)
                    self.added.append(crid)
                    self.conflicts.append(crid)
            return rid
        if old.get("first_seen") and row.get("first_seen") is not None and "first_seen" in self.cols:
            row["first_seen"] = old["first_seen"]  # first_seen never moves
        keep = ("access_date_jst", "run_id")
        if any(old[c] != row[c] for c in self.cols if c not in keep):
            self.rows[rid] = row
            self.changed.append(rid)
            # Filling a previously empty (unresolved) value is progress, not a parsing regression.
            if old["value"] != "" and not self.guard_exempt(old) and \
                    any(old[c] != row[c] for c in self.cols if c not in self.volatile):
                self.material.append(rid)
        return rid

    def get(self, rid):
        return self.rows.get(rid)

    def where(self, **kw):
        return [r for r in self.rows.values() if all(r.get(k) == v for k, v in kw.items())]

    def set_validation(self, rid: str, status: str, note: str | None = None):
        r = self.rows[rid]
        if r["agent_status"] in LOCKED:
            return
        if r["validation_status"] != status or (note and note not in r["correction_note"]):
            r["validation_status"] = status
            if note and note not in r["correction_note"]:
                r["correction_note"] = (r["correction_note"] + " | " if r["correction_note"] else "") + note
            if rid not in self.added and rid not in self.changed:
                self.changed.append(rid)

    def change_ratio(self) -> float:
        return (len(set(self.material)) / self.initial) if self.initial else 0.0

    def save(self, st: dict, force_guard_ok: bool = False) -> dict:
        ratio = self.change_ratio()
        if ratio > CONFIG["budgets"]["change_guard_ratio"] and not force_guard_ok:
            params = {"csv": self.name, "changed": len(set(self.material)), "existing": self.initial}
            ar = approved(st, "bulk_change", params)
            if not ar:
                ar = queue_approval(st, "bulk_change", f"Apply {len(set(self.material))} changes to {self.name}.csv",
                                    self.rel, params, f"{ratio:.0%} of existing rows would change (>20%); possible parsing regression")
                log(st, "csv_save", {"csv": self.name}, f"held: change guard ({ratio:.0%}); {ar}")
                staged = f"tmp/staged/{self.name}.{run_ctx(st)['run_id']}.csv"
                self._write(staged)
                return {"csv": self.name, "saved": False, "held_for": ar, "staged": staged}
            consume(st, ar)
        self._write(self.rel)
        return {"csv": self.name, "saved": True, "added": len(self.added),
                "changed": len(set(self.changed)), "conflicts": len(self.conflicts), "total": len(self.rows)}

    def _write(self, rel):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=self.cols, lineterminator="\n")
        w.writeheader()
        for rid in self.order:
            w.writerow(self.rows[rid])
        p = safe_path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("﻿" + buf.getvalue(), encoding="utf-8")

def base_row(**kw) -> dict:
    r = {"agent_status": "agent-extracted", "validation_status": "not_checked", "correction_note": "",
         "access_date_jst": iso()}
    r.update(kw)
    return r

def unresolved(row: dict, reason: str) -> dict:
    row = dict(row)
    row["value"] = ""
    row["agent_status"] = "unresolved"
    row["correction_note"] = reason
    return row

def out(obj):
    print(json.dumps(obj, ensure_ascii=False, default=str))

def sample(seq, frac: float, seed: str):
    seq = list(seq)
    if not seq:
        return []
    k = max(1, int(round(len(seq) * frac)))
    rnd = random.Random(seed)
    return rnd.sample(seq, min(k, len(seq)))
