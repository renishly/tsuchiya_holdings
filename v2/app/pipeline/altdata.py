"""Alternative data (phase P7): company-run listings, manual portal listings, group homepages
(current + Wayback), MLIT area price benchmark. Addendum A10.
"""
from __future__ import annotations

import csv
import json
import os
import re
import urllib.parse

from lib import (ROOT, BudgetStop, CsvStore, FetchError, GuardrailError, base_row, district_only,
                 fetch, iso, log, now, pii_scrub, record_id, run_ctx, unresolved)

GROUP_ENTITIES = ["土屋ホールディングス", "土屋ホーム不動産販売", "土屋ホーム不動産", "土屋ホームトピア",
                  "土屋ホームレジデンス", "土屋ホーム", "土屋ソーラーファクトリー"]

# Revenue-stream rules (order matters; first match wins). Changing them requires approval.
STREAMS = ["住宅事業: 注文住宅", "住宅事業: 分譲", "リフォーム事業", "不動産事業: 仲介", "不動産事業: 売買", "賃貸事業", "Unmapped"]

def categorize_listing(r: dict) -> tuple[str, str]:
    t = (r.get("取引態様") or "").replace(" ", "")
    ent = r.get("listing_entity") or ""
    rent = bool(r.get("rent_jpy"))
    if not t:
        return "Unmapped", "取引態様 not stated in the source; not inferred."
    if rent:
        if "貸主" in t:
            return "賃貸事業", f"取引態様 = {t} (group entity {ent} is lessor) → 賃貸事業"
        if re.search(r"仲介|媒介|代理", t):
            return "不動産事業: 仲介", f"取引態様 = {t} on a rental listing by {ent} → 不動産事業: 仲介"
        return "Unmapped", f"Rental listing with 取引態様 = {t}; no rule matches."
    if "売主" in t:
        if ent in ("土屋ホーム",):
            return "住宅事業: 分譲", f"取引態様 = 売主 and entity = {ent} (住宅事業 segment) → 住宅事業: 分譲"
        return "不動産事業: 売買", f"取引態様 = 売主 and entity = {ent} (不動産事業 segment) → 不動産事業: 売買"
    if re.search(r"媒介|仲介|代理", t):
        return "不動産事業: 仲介", f"取引態様 = {t} and entity = {ent} → 不動産事業: 仲介"
    return "Unmapped", f"取引態様 = {t}; no rule matches."

def _f(s):
    s = (s or "").replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return m.group(0) if m else ""

def _common(row: dict, *, url, title, location, source_type="company website", method="HTML"):
    row.update(source_url=url, source_title=title, location_in_source=location, source_type=source_type,
               extraction_method=method, as_reported_in="", unit=row.get("unit", ""), scale=row.get("scale", "1"))
    return row

# ---------------------------------------------------------------- IEST. (土屋ホーム不動産販売) new-build listings
IEST = "https://iestdot.com/property/"

def scan_iest(st, store: CsvStore, observed: set, detail_budget: int = 12) -> dict:
    from bs4 import BeautifulSoup
    run_id = run_ctx(st)["run_id"]
    today = iso()[:10]
    stats = {"pages": 0, "listings": 0, "details": 0, "complete": False}
    total = None
    page = 1
    seen_ids = []
    while True:
        url = IEST if page == 1 else f"{IEST}?page={page}"
        try:
            body, meta = fetch(st, url, purpose="company listings (IEST.)")
        except (FetchError, GuardrailError) as e:
            if page == 1:
                raise
            stats["error"] = f"page {page}: {e}"
            break
        stats["pages"] += 1
        soup = BeautifulSoup(body, "lxml")
        m = re.search(r"掲載物件数\s*/\s*(\d+)件", soup.get_text(" "))
        total = int(m.group(1)) if m else total
        cards = soup.select("a.property_list_link")
        for a in cards:
            lid = a["href"].rstrip("/").split("/")[-1]
            name = a.select_one(".property_name").get_text(strip=True) if a.select_one(".property_name") else ""
            from lib import injection_hits
            if injection_hits(name):
                continue  # listing card carrying instructions: logged by fetch, not stored
            cells = {th.get_text(strip=True): td.get_text(" ", strip=True) for th, td in
                     ((tr.find("th"), tr.find("td")) for tr in a.select("table.info_list tr")) if th and td}
            muni, dist = district_only(cells.get("所在地", ""))
            ptype = "新築戸建" if re.search(r"新築|戸建", name) else ("土地" if "土地" in name else "")
            seen_ids.append(lid)
            row = base_row(record_id=record_id("iestdot.com", lid), record_type="listing", listing_id=lid,
                           property_type=ptype, listing_entity="土屋ホーム不動産販売",
                           price_jpy=str(int(float(_f(cells.get("価格", ""))) * 10000)) if _f(cells.get("価格", "")) else "",
                           land_m2=_f(cells.get("土地面積", "")), floor_m2=_f(cells.get("建物面積", "")),
                           municipality=muni, district=dist, first_seen=today, last_seen=today, status="active",
                           value=str(int(float(_f(cells.get("価格", ""))) * 10000)) if _f(cells.get("価格", "")) else "",
                           unit="JPY", scale="1", period_label=f"observed {today}", period_end=today)
            old = store.get(row["record_id"])
            for keep in ("取引態様", "built_year"):
                if old and old.get(keep):
                    row[keep] = old[keep]
            if not row.get("取引態様") and stats["details"] < detail_budget:
                stats["details"] += 1
                try:
                    dbody, dmeta = fetch(st, urllib.parse.urljoin(url, a["href"]), purpose="listing detail (取引態様)")
                    dt_ = BeautifulSoup(dbody, "lxml")
                    kv = {}
                    for th in dt_.find_all(["th", "dt"]):
                        nxt = th.find_next_sibling(["td", "dd"])
                        if nxt:
                            kv[th.get_text(strip=True)] = nxt.get_text(" ", strip=True)
                    row["取引態様"] = kv.get("取引態様", "")
                    by = re.search(r"(19|20)\d\d", kv.get("築年月", ""))
                    row["built_year"] = by.group(0) if by else ""
                except (FetchError, GuardrailError) as e:
                    log(st, "listing_detail", {"id": lid}, str(e))
                except BudgetStop:
                    detail_budget = 0
            stream, reason = categorize_listing(row)
            row.update(revenue_stream=stream, category_reason=reason)
            _common(row, url=urllib.parse.urljoin(IEST, a["href"]), title=f"IEST. 物件 {name}",
                    location="物件一覧 card; 取引態様/築年月 from detail page")
            if not row["value"]:
                row = unresolved(row, "Price not shown on listing card; not imputed.")
            store.upsert(row, run_id)
            observed.add(row["record_id"])
            stats["listings"] += 1
        nxt = soup.find("a", href=re.compile(rf"\?page={page + 1}\b"))
        if not nxt or not cards:
            break
        page += 1
    stats["complete"] = total is not None and stats["listings"] >= total
    stats["total_reported"] = total
    return stats

# ---------------------------------------------------------------- 土屋ホーム不動産 rental list (realnetpro)
RNP = "https://mylist-v2.realnetpro.com/22359m1/"

def scan_rentals(st, store: CsvStore, observed: set, detail_budget: int = 10) -> dict:
    from bs4 import BeautifulSoup
    run_id = run_ctx(st)["run_id"]
    today = iso()[:10]
    stats = {"pages": 0, "listings": 0, "details": 0, "complete": False}
    page, ids_seen = 0, set()
    while page < 10:
        url = RNP if page == 0 else f"{RNP}?page={page}"
        try:
            body, meta = fetch(st, url, purpose="company rental listings")
        except (FetchError, GuardrailError) as e:
            if page == 0:
                raise
            stats["error"] = f"page {page}: {e}"
            stats["incomplete_pages"] = True
            break
        stats["pages"] += 1
        soup = BeautifulSoup(body, "lxml")
        new_here = 0
        for b in soup.select(".one_building"):
            info = b.select_one(".building_info_tr")
            parts = info.get_text("|", strip=True).split("|") if info else []
            kv = {parts[i]: parts[i + 1] for i in range(len(parts) - 1) if parts[i] in ("住所", "構造", "築年", "沿線")}
            bname = parts[0] if parts else ""
            muni, dist = district_only(kv.get("住所", ""))
            by = re.search(r"(19|20)\d\d", kv.get("築年", ""))
            for inp in b.select("input[class^=name_]"):
                rid_ = inp["class"][0].split("_", 1)[1]
                if rid_ in ids_seen:
                    continue
                ids_seen.add(rid_)
                new_here += 1
                g = lambda k: (b.select_one(f"input.{k}_{rid_}") or {}).get("value", "") if b.select_one(f"input.{k}_{rid_}") else ""
                rent = _f(g("cost"))
                row = base_row(record_id=record_id("realnetpro/22359m1", rid_), record_type="listing", listing_id=rid_,
                               property_type=f"賃貸 {g('layout')}".strip(), listing_entity="土屋ホーム不動産",
                               rent_jpy=rent, floor_m2=_f(g("meter")), built_year=by.group(0) if by else "",
                               municipality=muni, district=dist, first_seen=today, last_seen=today,
                               status="active", value=rent, unit="JPY/month", scale="1",
                               period_label=f"observed {today}", period_end=today)
                old = store.get(row["record_id"])
                if old and old.get("取引態様"):
                    row["取引態様"] = old["取引態様"]
                elif stats["details"] < detail_budget:
                    stats["details"] += 1
                    try:
                        dbody, _ = fetch(st, f"{RNP}rnp_plus02_detail.php?id={rid_}", purpose="rental detail (取引態様)")
                        m = re.search(r"取引態様\s*[|:：]?\s*([^\s|<]{2,12})", BeautifulSoup(dbody, "lxml").get_text("|", strip=True))
                        row["取引態様"] = m.group(1) if m else ""
                    except (FetchError, GuardrailError) as e:
                        log(st, "listing_detail", {"id": rid_}, str(e))
                    except BudgetStop:
                        detail_budget = 0
                stream, reason = categorize_listing(row)
                row.update(revenue_stream=stream, category_reason=reason)
                _common(row, url=f"{RNP}rnp_plus02_detail.php?id={rid_}", title=f"土屋ホーム不動産 賃貸 {bname}",
                        location=f"物件一覧 page {page}, building block '{bname}'")
                if not rent:
                    row = unresolved(row, "Rent not shown; not imputed.")
                store.upsert(row, run_id)
                observed.add(row["record_id"])
                stats["listings"] += 1
        if not new_here or not soup.find(attrs={"id": f"page={page + 1}"}):
            break
        page += 1
    stats["complete"] = bool(stats["listings"]) and not stats.get("incomplete_pages")
    return stats

# ---------------------------------------------------------------- tsuchiyafudosan.jp sale search (count only)
def scan_fudosan_count(st, store: CsvStore) -> dict:
    from bs4 import BeautifulSoup
    run_id = run_ctx(st)["run_id"]
    url = "https://tsuchiyafudosan.jp/property/"
    body, meta = fetch(st, url, purpose="company sale search count")
    m = re.search(r"全\s*(\d+)\s*件", BeautifulSoup(body, "lxml").get_text(" "))
    today = iso()[:10]
    row = base_row(record_id=record_id("tsuchiyafudosan.jp/property", "count", today), record_type="homepage",
                   page_type="売買物件検索", observed_item="掲載物件数 (sale listings on company search)",
                   snapshot_date=today, value=m.group(1) if m else "", unit="listings", scale="1",
                   revenue_stream="不動産事業: 仲介", category_reason="土屋ホーム不動産 own sale-listing search (brokerage branch network)",
                   period_label=f"observed {today}", period_end=today)
    _common(row, url=url, title="土屋ホーム不動産 物件を探す", location="search result count '全N件'")
    if not m:
        row = unresolved(row, "Result count not found on page; not imputed.")
    store.upsert(row, run_id)
    return {"count": row["value"]}

# ---------------------------------------------------------------- manual portal listings (student-entered)
MANUAL_COLS = ["observed_date", "portal", "branch", "listing_url", "screenshot_ref", "listing_id", "property_type",
               "取引態様", "listing_entity", "price_jpy", "rent_jpy", "land_m2", "floor_m2", "built_year",
               "municipality", "district", "notes"]

def ingest_manual(st, store: CsvStore) -> dict:
    """inputs/manual_listings.csv -> listing rows. The agent never fetches the portal URLs."""
    run_id = run_ctx(st)["run_id"]
    p = ROOT / "inputs" / "manual_listings.csv"
    stats = {"rows": 0, "rejected": 0, "pii_removed": 0, "duplicates": 0}
    if not p.exists():
        return stats
    with open(p, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    by_key: dict[str, list[dict]] = {}
    batches: dict[tuple, set] = {}
    for r in rows:
        r = {k: (v or "").strip() for k, v in r.items()}
        if not re.fullmatch(r"20\d\d-\d\d-\d\d", r.get("observed_date", "")) or not r.get("listing_url", "").startswith("http") \
                or not r.get("listing_id"):
            stats["rejected"] += 1
            log(st, "manual_listing", {"listing_id": r.get("listing_id")}, "rejected: missing date/url/id")
            continue
        key = record_id("manual", r.get("portal"), r["listing_id"])
        by_key.setdefault(key, []).append(r)
        batches.setdefault((r.get("portal"), r.get("branch")), set()).add(r["observed_date"])
    for key, obs in by_key.items():
        obs.sort(key=lambda x: x["observed_date"])
        first, last = obs[0], obs[-1]
        later_batches = [d for d in batches[(last.get("portal"), last.get("branch"))] if d > last["observed_date"]]
        notes, hit = pii_scrub(last.get("notes", ""))
        stats["pii_removed"] += int(hit)
        muni, dist = (last.get("municipality", ""), last.get("district", ""))
        if re.search(r"\d+番|\d+-\d+", dist):
            muni2, dist = district_only(muni + dist)
            stats["pii_removed"] += 1
        row = base_row(record_id=key, record_type="listing", listing_id=last["listing_id"],
                       property_type=last.get("property_type", ""), 取引態様=last.get("取引態様", ""),
                       listing_entity=last.get("listing_entity", ""), price_jpy=_f(last.get("price_jpy")),
                       rent_jpy=_f(last.get("rent_jpy")), land_m2=_f(last.get("land_m2")), floor_m2=_f(last.get("floor_m2")),
                       built_year=_f(last.get("built_year")), municipality=muni, district=dist,
                       first_seen=first["observed_date"], last_seen=last["observed_date"],
                       status="removed" if later_batches else "active",
                       value=_f(last.get("price_jpy")) or _f(last.get("rent_jpy")),
                       unit="JPY/month" if _f(last.get("rent_jpy")) and not _f(last.get("price_jpy")) else "JPY",
                       scale="1", period_label=f"observed {first['observed_date']}..{last['observed_date']}",
                       period_start=first["observed_date"], period_end=last["observed_date"])
        stream, reason = categorize_listing(row)
        row.update(revenue_stream=stream, category_reason=reason)
        _common(row, url=first["listing_url"], title=f"{last.get('portal')} listing (manual, branch {last.get('branch')})",
                location=f"student screenshot {first.get('screenshot_ref')} (first seen)", source_type="manual (student)",
                method="manual")
        if later_batches:
            row["correction_note"] = (f"Absent from the {last.get('portal')}/{last.get('branch')} snapshot of {min(later_batches)}; "
                                      "disappearance is not proof of a sale.")
        if notes:
            row["correction_note"] = (row["correction_note"] + " | " if row["correction_note"] else "") + f"student note: {notes[:200]}"
        store.upsert(row, run_id)
        stats["rows"] += 1
    stats["duplicates"] = dedupe_listings(store)
    return stats

def dedupe_listings(store: CsvStore) -> int:
    """Flag the same property listed under several IDs on different sources (same place, type, price/rent, area).

    Distinct IDs on one site (e.g. adjacent lots with equal price and area) are separate properties, not duplicates.
    """
    import urllib.parse as up
    groups: dict[tuple, list[tuple[str, str]]] = {}
    for rid, r in store.rows.items():
        if r["record_type"] != "listing" or r["status"] == "removed":
            continue
        k = (r["municipality"], r["district"], r["property_type"], r["price_jpy"] or r["rent_jpy"], r["land_m2"], r["floor_m2"])
        if not any(k[3:]):
            continue
        groups.setdefault(k, []).append((rid, up.urlsplit(r["source_url"]).netloc))
    n = 0
    for k, members in groups.items():
        if len({dom for _, dom in members}) < 2:
            continue
        members.sort(key=lambda m: (m[1] not in GROUP_SITES, m[0]))  # keep the company-run listing as primary
        primary = members[0][0]
        for rid, dom in members[1:]:
            store.set_validation(rid, "failed:listing_duplicate", f"Possible duplicate of {primary} (same place/type/price/area on another source).")
            n += 1
    return n

GROUP_SITES = {"iestdot.com", "mylist-v2.realnetpro.com", "tsuchiyafudosan.jp"}

def mark_removed(store: CsvStore, observed: set, source_prefix: str, scan_complete: bool) -> int:
    """Listings from a fully scanned source that were not observed today become status=removed."""
    if not scan_complete:
        return 0
    n = 0
    for rid, r in store.rows.items():
        if r["record_type"] == "listing" and r["source_url"].startswith(source_prefix) and rid not in observed \
                and r["status"] == "active" and r["agent_status"] not in ("student-verified", "student-corrected"):
            r["status"] = "removed"
            r["correction_note"] = (r["correction_note"] + " | " if r["correction_note"] else "") + \
                f"Not on the complete scan of {iso()[:10]}; removal is not proof of a sale."
            store.changed.append(rid)
            n += 1
    return n

# ---------------------------------------------------------------- group homepages (current and Wayback)
HOMEPAGES = [  # (url, page_type, revenue_stream, reason)
    ("https://www.tsuchiyahome.jp/modelhouse/", "展示場", "住宅事業: 注文住宅", "土屋ホーム model-house list (custom-home sales channel)"),
    ("https://www.tsuchiyahome.jp/realestate/", "分譲地", "住宅事業: 分譲", "土屋ホーム 分譲地 page (lots/homes for sale)"),
    ("https://www.tsuchiyahome.jp/event/", "キャンペーン", "住宅事業: 注文住宅", "土屋ホーム events/campaigns"),
    ("https://www.tsuchiyahome.jp/lineup/", "商品ラインナップ", "住宅事業: 注文住宅", "土屋ホーム product lineup"),
    ("https://www.hometopia.jp/", "リフォームメニュー", "リフォーム事業", "土屋ホームトピア (renovation subsidiary) site"),
    ("https://tsuchiyafudosan.jp/service", "サービス", "不動産事業: 仲介", "土屋ホーム不動産 services (brokerage, buy-back, rental management)"),
    ("https://tsuchiyafudosan.jp/store", "店舗", "不動産事業: 仲介", "土屋ホーム不動産 branch list"),
    ("https://iestdot.com/", "分譲ブランド", "不動産事業: 売買", "土屋ホーム不動産販売 IEST. brand site"),
]

def extract_items(html_s: str, page_url: str, limit: int = 60) -> list[str]:
    """Item labels on a list page: link texts to sub-pages under the same path, else h2/h3 headings."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_s, "lxml")
    for bad in soup(["script", "style", "header", "footer", "nav", "noscript"]):
        bad.decompose()
    base = urllib.parse.urlsplit(page_url)
    path = base.path if base.path.endswith("/") else base.path + "/"
    items = []
    for a in soup.find_all("a", href=True):
        u = urllib.parse.urlsplit(urllib.parse.urljoin(page_url, a["href"]))
        if u.netloc and u.netloc != base.netloc:
            continue
        if u.path.startswith(path) and u.path.rstrip("/") != path.rstrip("/"):
            t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
            if 2 <= len(t) <= 80 and t not in items:
                items.append(t)
    if not items:
        for h in soup.find_all(["h2", "h3"]):
            t = re.sub(r"\s+", " ", h.get_text(" ", strip=True))
            if 2 <= len(t) <= 80 and t not in items:
                items.append(t)
    from lib import injection_hits
    out = []
    for t in items[:limit]:
        if injection_hits(t):
            continue  # instruction-like text is never stored as an observed item
        t, _ = pii_scrub(t)
        out.append(t)
    return out

def snapshot_homepages(st, store: CsvStore) -> dict:
    run_id = run_ctx(st)["run_id"]
    today = iso()[:10]
    stats = {"pages": 0, "items": 0, "failed": []}
    for url, ptype, stream, reason in HOMEPAGES:
        try:
            body, meta = fetch(st, url, purpose=f"homepage snapshot ({ptype})")
        except (FetchError, GuardrailError) as e:
            stats["failed"].append({"url": url, "error": str(e)})
            continue
        except BudgetStop:
            break
        stats["pages"] += 1
        items = extract_items(body, url)
        observed = set()
        for it in items:
            row = base_row(record_id=record_id("homepage", url, it), record_type="homepage", page_type=ptype,
                           observed_item=it, snapshot_date=today, first_seen=today, last_seen=today, status="active",
                           value="present", unit="", scale="", revenue_stream=stream, category_reason=reason,
                           period_label=f"observed {today}", period_end=today)
            _common(row, url=url, title=f"{ptype} page", location=f"item link/heading; snapshot sha256 {meta['sha256'][:12]}")
            store.upsert(row, run_id)
            observed.add(row["record_id"])
            stats["items"] += 1
        for rid, r in store.rows.items():
            if r["record_type"] == "homepage" and r["source_url"] == url and r["status"] == "active" \
                    and rid not in observed and r["page_type"] == ptype and items:
                r["status"] = "removed"
                r["correction_note"] = f"No longer on page as of {today}."
                store.changed.append(rid)
        cnt = base_row(record_id=record_id("homepage-count", url, today), record_type="homepage", page_type=ptype,
                       observed_item="item count", snapshot_date=today, value=str(len(items)), unit="items", scale="1",
                       revenue_stream=stream, category_reason=reason, period_label=f"observed {today}", period_end=today)
        _common(cnt, url=url, title=f"{ptype} page", location="count of item links/headings")
        store.upsert(cnt, run_id)
    return stats

def wayback(st, store: CsvStore, years=(2021, 2022, 2023, 2024, 2025), max_snapshots: int = 8) -> dict:
    """One archived snapshot per page per year (cursor in state['daily']['wayback_done'])."""
    run_id = run_ctx(st)["run_id"]
    done = st["daily"].setdefault("wayback_done", [])
    stats = {"snapshots": 0, "items": 0, "missing": []}
    for url, ptype, stream, reason in HOMEPAGES:
        for y in years:
            key = f"{url}|{y}"
            if key in done or stats["snapshots"] >= max_snapshots:
                continue
            try:
                body, _ = fetch(st, "https://web.archive.org/cdx/search/cdx",
                                params={"url": url, "from": str(y), "to": str(y), "output": "json",
                                        "filter": "statuscode:200", "collapse": "timestamp:4", "limit": "1"},
                                purpose="Wayback CDX lookup", snapshot=False)
                rows = json.loads(body or "[]")
            except (FetchError, GuardrailError, ValueError) as e:
                stats["missing"].append({"url": url, "year": y, "error": str(e)[:100]})
                continue
            except BudgetStop:
                return stats
            if len(rows) < 2:
                done.append(key)
                row = base_row(record_id=record_id("wayback", url, y, "none"), record_type="homepage", page_type=ptype,
                               observed_item=f"no archived snapshot in {y}", snapshot_date="", value="",
                               revenue_stream=stream, category_reason=reason, period_label=str(y))
                _common(row, url=f"https://web.archive.org/web/{y}*/{url}", title="Wayback Machine", location="CDX query",
                        source_type="web archive")
                store.upsert(unresolved(row, f"Wayback Machine has no 200 snapshot of this page in {y}."), run_id)
                continue
            ts = rows[1][1]
            snap = f"https://web.archive.org/web/{ts}/{url}"
            try:
                body, meta = fetch(st, snap, purpose="Wayback snapshot")
            except (FetchError, GuardrailError) as e:
                stats["missing"].append({"url": snap, "error": str(e)[:100]})
                continue
            except BudgetStop:
                return stats
            sdate = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
            items = extract_items(body, url)
            for it in items:
                row = base_row(record_id=record_id("wayback", url, ts, it), record_type="homepage", page_type=ptype,
                               observed_item=it, snapshot_date=sdate, value="present", status="archived",
                               revenue_stream=stream, category_reason=reason, period_label=f"snapshot {sdate}", period_end=sdate)
                _common(row, url=snap, title=f"{ptype} page (archived)", location=f"Wayback snapshot {ts}",
                        source_type="web archive")
                store.upsert(row, run_id)
                stats["items"] += 1
            cnt = base_row(record_id=record_id("wayback-count", url, ts), record_type="homepage", page_type=ptype,
                           observed_item="item count", snapshot_date=sdate, value=str(len(items)), unit="items", scale="1",
                           status="archived", revenue_stream=stream, category_reason=reason,
                           period_label=f"snapshot {sdate}", period_end=sdate)
            _common(cnt, url=snap, title=f"{ptype} page (archived)", location=f"Wayback snapshot {ts}", source_type="web archive")
            store.upsert(cnt, run_id)
            stats["snapshots"] += 1
            done.append(key)
    return stats

# ---------------------------------------------------------------- MLIT 不動産情報ライブラリ benchmark
REINFOLIB = "https://www.reinfolib.mlit.go.jp/ex-api/external/XIT001"
BENCH_CITIES = {"01101": "札幌市中央区", "01102": "札幌市北区", "01103": "札幌市東区", "01104": "札幌市白石区",
                "01105": "札幌市豊平区", "01106": "札幌市南区", "01107": "札幌市西区", "01108": "札幌市厚別区",
                "01109": "札幌市手稲区", "01110": "札幌市清田区", "01202": "函館市", "01217": "江別市",
                "01224": "千歳市", "01236": "北斗市"}

def benchmark(st, store: CsvStore, max_calls: int = 8) -> dict:
    """Median trade price per city and property type for the most recent published quarters."""
    import statistics
    run_id = run_ctx(st)["run_id"]
    key = os.environ.get("REINFOLIB_API_KEY", "")
    if not key and run_ctx(st).get("mode") != "test":
        return {"skipped": "REINFOLIB_API_KEY not set"}
    cur = st["daily"].setdefault("benchmark_cursor", [])
    t = now()
    q = (t.month - 1) // 3  # last completed quarter (data is published with a lag; try two back)
    year, quarter = (t.year, q - 1) if q >= 2 else (t.year - 1, q + 3)
    stats = {"calls": 0, "rows": 0}
    for code, name in BENCH_CITIES.items():
        k = f"{year}Q{quarter}|{code}"
        if k in cur or stats["calls"] >= max_calls:
            continue
        try:
            body, meta = fetch(st, REINFOLIB, params={"year": year, "quarter": quarter, "city": code},
                               headers={"Ocp-Apim-Subscription-Key": key}, purpose="MLIT area price benchmark")
            stats["calls"] += 1
            data = json.loads(body).get("data", [])
        except (FetchError, GuardrailError, ValueError) as e:
            log(st, "benchmark", {"city": code}, str(e)[:120])
            continue
        except BudgetStop:
            break
        by_type: dict[str, list[float]] = {}
        for d in data:
            try:
                by_type.setdefault(d.get("Type", ""), []).append(float(d["TradePrice"]))
            except (KeyError, ValueError, TypeError):
                continue
        for typ, prices in by_type.items():
            row = base_row(record_id=record_id("reinfolib", code, year, quarter, typ), record_type="benchmark",
                           property_type=typ, municipality=name, district="", value=str(int(statistics.median(prices))),
                           unit="JPY", scale="1", period_label=f"{year} Q{quarter}", revenue_stream="Unmapped",
                           category_reason=f"Area benchmark (n={len(prices)} transactions); not a Tsuchiya transaction.",
                           correction_note=f"Median of {len(prices)} anonymised transactions; benchmark only.")
            _common(row, url=f"{REINFOLIB}?year={year}&quarter={quarter}&city={code}",
                    title="国土交通省 不動産情報ライブラリ 不動産価格（取引価格）情報", location="API XIT001",
                    source_type="government API", method="API")
            store.upsert(row, run_id)
            stats["rows"] += 1
        cur.append(k)
    return stats
