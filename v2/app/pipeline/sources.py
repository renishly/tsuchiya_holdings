"""Evidence-board sources beyond the company IR pages (Addendum A48, v2.012). Code only; no model.

Each collector returns board items {date, ticker, title, url, source, topic} and never raises: a failure is
returned as {"error": ...} and shown on the C&A page. All fetches go through lib.fetch (allowlist, robots.txt,
rate limits). Robots checked 30 Oct 2026: boj.or.jp and mlit.go.jp have no robots.txt; simulation.jhf.go.jp
disallows only its income tools; api.gdeltproject.org is a documented API. TDnet (release.tdnet.info) disallows all
crawlers and is never fetched.
  boj      Bank of Japan "what's new" RSS, policy-related items only (decisions, outlook, Tankan, governor)
  mlit     MLIT press releases (monthly list), housing, land, real-estate and construction items only
  jhf      フラット35 monthly borrowing rate (one item per month, with the rate in the title)
  edinet   臨時報告書 and half-year reports of M1 universe members (from the daily EDINET list)
  gdelt    GDELT DOC API headlines on Japanese housing, property and rates: context only, never citable
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import re
import xml.etree.ElementTree as ET

from lib import BudgetStop, FetchError, GuardrailError, fetch, iso

SOURCES = {
    "boj": "Bank of Japan (news feed)",
    "mlit": "MLIT press releases",
    "jhf": "JHF フラット35 rates",
    "edinet": "EDINET (universe companies)",
    "gdelt": "GDELT news (context only)",
}
FETCH_ERRORS = (FetchError, GuardrailError, BudgetStop)

BOJ_RSS = "https://www.boj.or.jp/rss/whatsnew.xml"
BOJ_KEEP = re.compile(r"金融政策|金融市場調節方針|展望レポート|経済・物価情勢|短観|総裁.*(記者会見|講演|挨拶)|主な意見|議事要旨|地域経済報告|金利|国債買入")
BOJ_DROP = re.compile(r"バーゼル|ライブ配信|月報|退職|公募|中央銀行総裁会合|総裁会議")
MLIT_LIST = "https://www.mlit.go.jp/report/press/houdou{ym}.html"
MLIT_BUREAUS = ("house", "tochi_fudousan", "totikensangyo", "kensangyo")   # 住宅局, 不動産・建設経済局
MLIT_KEEP = re.compile(r"住宅|不動産|地価|土地|着工|建設資材|資材価格|省エネ|ZEH|リフォーム|空き家|宅地|マンション|木材")
MLIT_DROP = re.compile(r"議論します|試験の合格|漏洩|開催|検討会|部会|ワーキング|分科会|懇談会|表彰|募集|公募|説明会|シンポジウム|セミナー|意見募集|海外展開|人事")
JHF_RATES = "https://www.simulation.jhf.go.jp/flat35/kinri/index.php/rates/top"
GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_QUERY = ('("Japan housing" OR "Japanese housing" OR "Japan property" OR "Japan real estate" OR "Bank of Japan" '
               'OR "Japan mortgage" OR Hokkaido) sourcelang:english')
EDINET_TYPES = {"180": "臨時報告書", "160": "半期報告書", "120": "有価証券報告書"}


def _item(date, ticker, title, url, source, topic, **extra) -> dict:
    return {"date": date, "ticker": ticker, "title": " ".join(title.split())[:200], "url": url, "source": source,
            "topic": topic, **extra}


def boj(st) -> list[dict] | dict:
    try:
        body, _ = fetch(st, BOJ_RSS, binary=True, snapshot=False, purpose="evidence board: BOJ news feed")
        root = ET.fromstring(body)
    except FETCH_ERRORS as e:
        return {"error": str(e)[:160]}
    except ET.ParseError:
        return {"error": "feed could not be parsed"}
    out = []
    for it in root.iter("item"):
        title, link, pub = (it.findtext(k) or "" for k in ("title", "link", "pubDate"))
        if not BOJ_KEEP.search(title) or BOJ_DROP.search(title) or not link:
            continue
        try:
            date = email.utils.parsedate_to_datetime(pub).date().isoformat()
        except (TypeError, ValueError):
            continue
        out.append(_item(date, "", title, link.replace("http://", "https://", 1), SOURCES["boj"], "housing policy and finance"))
    return out


def mlit(st) -> list[dict] | dict:
    today = dt.date.fromisoformat(iso()[:10])
    prev = (today.replace(day=1) - dt.timedelta(days=1))
    out = []
    for d in (today, prev):
        try:
            text, _ = fetch(st, MLIT_LIST.format(ym=d.strftime("%Y%m")), snapshot=False, purpose="evidence board: MLIT press releases")
        except FETCH_ERRORS as e:
            if d == today:
                return {"error": str(e)[:160]}
            continue
        # the list is <dt>2026年10月27日</dt> followed by <dd> links, newest first
        for block in re.split(r"<dt>", text)[1:]:
            m = re.match(r"\s*(\d{4})年(\d{1,2})月(\d{1,2})日", block)
            if not m:
                continue
            date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            for href, title in re.findall(r'<a href="(/report/press/[^"]+\.html)">(.*?)</a>', block, re.S):
                title = re.sub(r"<br\s*/?>", " ", title)
                title = re.sub(r"<[^>]+>", "", title)
                bureau = href.split("/")[-1].split("_")[0].rstrip("0123456789")
                if (bureau == "house" or MLIT_KEEP.search(title)) and not MLIT_DROP.search(title):
                    out.append(_item(date, "", title, "https://www.mlit.go.jp" + href, SOURCES["mlit"], "housing policy and finance"))
    return out


def jhf(st) -> list[dict] | dict:
    """One item per month: the フラット35 rate range and most common rate (9割以下, 21–35 years)."""
    try:
        body, _ = fetch(st, JHF_RATES, binary=True, purpose="evidence board: フラット35 rates")
    except FETCH_ERRORS as e:
        return {"error": str(e)[:160]}
    text = body.decode("euc_jp", errors="replace")
    text = re.sub(r"(?s)<script.*?</script>", " ", text)
    text = " ".join(re.sub(r"<[^>]+>", " ", text).split()).translate(str.maketrans("０１２３４５６７８９．％", "0123456789.%"))
    m = re.search(r"借入金利水準（(\d{4})年(\d{1,2})月）", text)
    rates = re.findall(r"年(\d\.\d{3})%", text[m.end():] if m else text)
    if not m or len(rates) < 3:
        return {"error": "rate table not found (page layout changed?)"}
    ym = f"{m.group(1)}年{int(m.group(2))}月"
    title = f"【フラット35】借入金利 {ym}: 最も多い金利 年{rates[2]}%（範囲 年{rates[0]}%〜年{rates[1]}%、融資率9割以下・21〜35年）"
    url = f"{JHF_RATES}#{m.group(1)}{int(m.group(2)):02d}"   # one stable id per month
    return [_item(f"{m.group(1)}-{int(m.group(2)):02d}-01", "", title, url, SOURCES["jhf"], "housing policy and finance",
                  excerpt=title)]


def edinet_events(st, members: dict[str, str]) -> list[dict] | dict:
    """臨時報告書, half-year and annual reports filed today or yesterday by universe members (EDINET code -> ticker)."""
    import edinet
    today = dt.date.fromisoformat(iso()[:10])
    out = []
    for i in (1, 0):
        d = (today - dt.timedelta(days=i)).isoformat()
        try:
            rows = edinet.list_day(st, d)
        except FETCH_ERRORS as e:
            return {"error": str(e)[:160]}
        for r in rows:
            t = members.get(r.get("edinetCode"))
            if not t or r.get("docTypeCode") not in EDINET_TYPES or r.get("withdrawalStatus") not in (None, "0"):
                continue
            title = f"{r.get('filerName') or t}: {r.get('docDescription') or EDINET_TYPES[r['docTypeCode']]}"
            out.append(_item((r.get("submitDateTime") or d)[:10], t, title,
                             f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{r.get('docID')}", SOURCES["edinet"],
                             "company and peers"))
    return out


def gdelt(st) -> list[dict] | dict:
    """Headlines only (title, outlet, date). The articles are on outside sites the agent does not fetch, so these
    items are context for the news scouts and can never be cited."""
    try:
        text, _ = fetch(st, GDELT_API, params={"query": GDELT_QUERY, "mode": "artlist", "format": "json",
                                              "maxrecords": "25", "timespan": "2d", "sort": "datedesc"},
                        snapshot=False, purpose="evidence board: GDELT headlines")
        if not text.strip().startswith("{"):   # e.g. "Please limit requests to one every 5 seconds"
            return {"error": "GDELT: " + " ".join(text.split())[:100]} if text.strip() else []
        arts = json.loads(text).get("articles", [])
    except FETCH_ERRORS as e:
        return {"error": str(e)[:160]}
    except ValueError:
        return {"error": "GDELT reply could not be parsed"}
    out = []
    for a in arts:
        sd = a.get("seendate") or ""
        if not re.match(r"\d{8}", sd) or not a.get("url"):
            continue
        out.append(_item(f"{sd[:4]}-{sd[4:6]}-{sd[6:8]}", "", f"{a.get('title', '')} ({a.get('domain', '')})", a["url"],
                         SOURCES["gdelt"], "politics and macro", context_only=True))
    return out
