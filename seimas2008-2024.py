#!/usr/bin/env python3
import json
import logging
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urljoin

import requests
from bs4 import BeautifulSoup, NavigableString

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

DB_FILE = "seimas.db"
HTML_FILE = "index.html"
STATIC_BASE = "https://www.vrk.lt/statiniai/puslapiai"
REQUEST_DELAY = 1.0

MANUAL_MEMBERS = {
    2008: [
        "https://www.vrk.lt/statiniai/puslapiai/rinkimai/396/Kandidatai/Kandidatas19310/Kandidato19310Deklaracijos.html",
        "https://www.vrk.lt/statiniai/puslapiai/rinkimai/406_lt/Kandidatai/Kandidatas26261/Kandidato26261Deklaracijos.html",
        "https://www.vrk.lt/statiniai/puslapiai/rinkimai/406_lt/Kandidatai/Kandidatas26249/Kandidato26249Deklaracijos.html",
        "https://www.vrk.lt/statiniai/puslapiai/rinkimai/412_lt/Kandidatai/Kandidatas66343/Kandidato66343Deklaracijos.html",
    ],
    2012: [
        "https://www.vrk.lt/statiniai/puslapiai/rinkimai/459_lt/Kandidatai/Kandidatas87929/Kandidato87929Deklaracijos.html",
        "https://www.vrk.lt/statiniai/puslapiai/rinkimai/448_lt/Kandidatai/Kandidatas87277/Kandidato87277Deklaracijos.html",
    ],
    2016: [
        "https://www.vrk.lt/2017-seim-sav/rezultatai?srcUrl=/rinkimai/748/rnk984/kandidatai/lrsKandidatasTurtas_rkndId-1106688.html",
        "https://www.vrk.lt/2018-09-16_nauji-rinkimai-i-seima/rezultatai?srcUrl=/rinkimai/826/rnk1064/kandidatai/lrsKandidatasTurtas_rkndId-2399846.html",
        "https://www.vrk.lt/2019-seim/rezultatai?srcUrl=/rinkimai/1066/rnk1388/kandidatai/lrsKandidatasTurtas_rkndId-2415632.html",
        "https://www.vrk.lt/2019-seim/rezultatai?srcUrl=/rinkimai/1066/rnk1388/kandidatai/lrsKandidatasTurtas_rkndId-2415642.html",
        "https://www.vrk.lt/2019-seim/rezultatai?srcUrl=/rinkimai/1066/rnk1388/kandidatai/lrsKandidatasTurtas_rkndId-2415641.html",
    ],
}

ELECTIONS = {
    2024: {
        "list_src":  "/rinkimai/1544/2/2148/rezultatai/lt/rezultataiIsrinktiNariai.html",
        "site_prefix": "https://www.vrk.lt/2024-seimo/rezultatai",
        "currency":  "EUR",
    },
    2020: {
        "list_src":  "/rinkimai/1104/2/1744/rezultatai/lt/rezultataiIsrinktiNariai.html",
        "site_prefix": "https://www.vrk.lt/2020-seimo/rezultatai",
        "currency":  "EUR",
    },
    2016: {
        "list_src":  "/rinkimai/102/2/1306/rezultatai/lt/rezultataiIsrinktiNariai.html",
        "site_prefix": "https://www.vrk.lt/2016-seimo/rezultatai",
        "currency":  "EUR",
    },
    2012: {
        "list_url": "https://www.vrk.lt/statiniai/puslapiai/2012_seimo_rinkimai/output_lt/rinkimu_diena/isrinkti_seimo_nariai_kadencijaik.html",
        "currency": "LTL",
    },
    2008: {
        "list_url": "https://www.vrk.lt/statiniai/puslapiai/2008_seimo_rinkimai/output_lt/rinkimu_diena/isrinkti_seimo_nariai_kadencijaik.html",
        "currency": "LTL",
    },
}


@dataclass
class MemberInfo:
    name: str
    anketa_url: str
    turto_url: str
    district_name: str
    district_url: Optional[str]
    party_name: str
    party_url: Optional[str]


@dataclass
class Declaration:
    declaration_year: Optional[int]
    turto_url: str
    currency: str
    mandatory_property: Optional[float] = None
    securities: Optional[float] = None
    monetary_funds: Optional[float] = None
    loans_given: Optional[float] = None
    loans_received: Optional[float] = None
    total_income: Optional[float] = None
    income_tax: Optional[float] = None
    raw_data: str = ""


def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS elections (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            year     INTEGER UNIQUE NOT NULL,
            list_url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS members (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            election_id   INTEGER NOT NULL REFERENCES elections(id),
            name          TEXT NOT NULL,
            anketa_url    TEXT,
            turto_url     TEXT,
            district_name TEXT,
            district_url  TEXT,
            party_name    TEXT,
            party_url     TEXT,
            UNIQUE(election_id, anketa_url)
        );
        CREATE TABLE IF NOT EXISTS declarations (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id          INTEGER UNIQUE NOT NULL REFERENCES members(id),
            declaration_year   INTEGER,
            turto_url          TEXT,
            currency           TEXT DEFAULT 'EUR',
            mandatory_property REAL,
            securities         REAL,
            monetary_funds     REAL,
            loans_given        REAL,
            loans_received     REAL,
            total_income       REAL,
            income_tax         REAL,
            raw_data           TEXT
        );
    """)
    conn.commit()


def upsert_election(conn, year, list_url) -> int:
    conn.execute("INSERT OR IGNORE INTO elections(year, list_url) VALUES(?,?)", (year, list_url))
    conn.commit()
    return conn.execute("SELECT id FROM elections WHERE year=?", (year,)).fetchone()[0]


def upsert_member(conn, election_id, m: MemberInfo) -> Optional[int]:
    conn.execute(
        "INSERT OR IGNORE INTO members(election_id,name,anketa_url,turto_url,district_name,district_url,party_name,party_url) VALUES(?,?,?,?,?,?,?,?)",
        (election_id, m.name, m.anketa_url, m.turto_url, m.district_name, m.district_url, m.party_name, m.party_url),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM members WHERE election_id=? AND anketa_url=?", (election_id, m.anketa_url)).fetchone()
    return row[0] if row else None


def save_declaration(conn, member_id, d: Declaration):
    conn.execute(
        """INSERT OR REPLACE INTO declarations
           (member_id,declaration_year,turto_url,currency,
            mandatory_property,securities,monetary_funds,loans_given,loans_received,
            total_income,income_tax,raw_data)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (member_id, d.declaration_year, d.turto_url, d.currency,
         d.mandatory_property, d.securities, d.monetary_funds, d.loans_given, d.loans_received,
         d.total_income, d.income_tax, d.raw_data),
    )
    conn.commit()


def already_scraped(conn, member_id) -> bool:
    return conn.execute("SELECT 1 FROM declarations WHERE member_id=?", (member_id,)).fetchone() is not None


SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})


def fetch(url: str) -> Optional[str]:
    try:
        r = SESSION.get(url, timeout=30)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        return r.text
    except Exception as e:
        log.warning(f"  GET {url}: {e}")
        return None


def static_url(src_path: str) -> str:
    return STATIC_BASE + src_path


def spa_display_url(site_prefix: str, src_path: str) -> str:
    return f"{site_prefix}?srcUrl={src_path}"


def extract_src_path(href: str) -> str:
    if href.startswith("?srcUrl="):
        return href[8:]
    if "srcUrl=" in href:
        return parse_qs(href.split("?", 1)[-1]).get("srcUrl", [""])[0]
    return href


def anketa_src_to_turto_src(src_path: str) -> str:
    if "lrsKandidatasAnketa_" in src_path:
        return src_path.replace("lrsKandidatasAnketa_", "lrsKandidatasTurtas_")
    if "KandidatasAnketa_" in src_path:
        return src_path.replace("KandidatasAnketa_", "KandidatasTurtoPajDekl_")
    return re.sub(r"Kandidato(\d+)Anketa\.html", r"Kandidato\1Deklaracijos.html", src_path)


def resolve_href_to_static(href: str, list_url: str) -> str:
    if not href:
        return ""
    if href.startswith("?srcUrl="):
        return static_url(href[8:])
    if href.startswith("?") and "srcUrl" in href:
        src = extract_src_path(href)
        return static_url(src) if src else ""
    return urljoin(list_url, href)


def parse_amount(text: str) -> Optional[float]:
    if not text:
        return None
    text = re.sub(r"\b(EUR|Eur|Lt|LTL)\b", "", text).strip()
    text = re.sub(r"\s+", "", text)
    if not text or text in ("-", "–", "—"):
        return None
    if "." in text and "," in text:
        if text.rfind(".") > text.rfind(","):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        parts = text.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def _fill_field(d: Declaration, label: str, amt: Optional[float]):
    lo = label.lower()
    if re.match(r"^i\.", label, re.I) or "privalomas registruoti" in lo:
        if d.mandatory_property is None:
            d.mandatory_property = amt
    elif re.match(r"^ii\.", label, re.I) or "vertybiniai popieriai" in lo:
        if d.securities is None:
            d.securities = amt
    elif re.match(r"^iii\.", label, re.I) or "piniginės lėšos" in lo:
        if d.monetary_funds is None:
            d.monetary_funds = amt
    elif re.match(r"^iv\.", label, re.I) or ("suteiktos paskolos" in lo and "gautos" not in lo):
        if d.loans_given is None:
            d.loans_given = amt
    elif re.match(r"^v\.", label, re.I) or "gautos paskolos" in lo:
        if d.loans_received is None:
            d.loans_received = amt
    elif "apmokestinamųjų ir neapmokestinamųjų pajamų suma" in lo:
        if d.total_income is None:
            d.total_income = amt
    elif "mokėtina pajamų mokesčio suma" in lo:
        if d.income_tax is None:
            d.income_tax = amt
    elif ("gauta pajamų" in lo or ("pajamų suma" in lo and "individualios" not in lo and "mokesčio" not in lo)) and d.total_income is None:
        d.total_income = amt
    elif ("sumokėta mokesčio" in lo or "išskaičiuota" in lo or "išskaičiuotas" in lo or ("pajamų mokestis" in lo and "mokėtina" not in lo)) and d.income_tax is None:
        d.income_tax = amt


def _fill_from_regex(d: Declaration, text: str):
    cur = r"(?:EUR|Eur|Lt|LTL)?"
    patterns = [
        (r"I\.\s+Privalomas registruoti turtas[:\s]+([\d\s.,]+)\s*" + cur, "mandatory_property"),
        (r"II\.\s+Vertybiniai popieriai[^\n]*?[:\s]+([\d\s.,]+)\s*" + cur, "securities"),
        (r"III\.\s+Piniginės lėšos[:\s]+([\d\s.,]+)\s*" + cur, "monetary_funds"),
        (r"IV\.\s+Suteiktos paskolos[:\s]+([\d\s.,]+)\s*" + cur, "loans_given"),
        (r"V\.\s+Gautos paskolos[:\s]+([\d\s.,]+)\s*" + cur, "loans_received"),
        (r"apmokestinamųjų ir neapmokestinamųjų pajamų suma\s+([\d\s.,]+)\s*" + cur, "total_income"),
        (r"mokėtina pajamų mokesčio suma\s+([\d\s.,]+)\s*" + cur, "income_tax"),
    ]
    for pat, attr in patterns:
        if getattr(d, attr) is None:
            m = re.search(pat, text, re.I | re.S)
            if m:
                setattr(d, attr, parse_amount(m.group(1)))


def parse_declaration(html: str, url: str, currency: str) -> Declaration:
    soup = BeautifulSoup(html, "html.parser")
    raw = soup.get_text(separator="\n", strip=True)

    d = Declaration(declaration_year=None, turto_url=url, currency=currency, raw_data=raw[:8000])

    m = re.search(r"\((\d{4})\s*m\.?\)", raw)
    if m:
        d.declaration_year = int(m.group(1))
    else:
        m = re.search(r"(\d{4})-\d{2}-\d{2}", raw)
        if m:
            d.declaration_year = int(m.group(1))

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(separator=" ", strip=True)
        if "išrašas" in label.lower():
            continue
        value = cells[-1].get_text(separator=" ", strip=True)
        _fill_field(d, label, parse_amount(value))

    _fill_from_regex(d, raw)

    if currency == "LTL":
        for f in ["mandatory_property", "securities", "monetary_funds",
                  "loans_given", "loans_received", "total_income", "income_tax"]:
            v = getattr(d, f)
            if v is not None:
                setattr(d, f, round(v / 3.45, 2))
        d.currency = "EUR"

    return d


def parse_member_list(html: str, list_url: str, site_prefix: Optional[str]) -> list[MemberInfo]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        log.error(f"No table in {list_url}")
        return []

    members = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 3:
            continue

        a = cells[0].find("a")
        if not a:
            continue
        name = a.get_text(strip=True)
        anketa_href = a.get("href", "")
        anketa_url = resolve_href_to_static(anketa_href, list_url)

        anketa_src = extract_src_path(anketa_href)
        turto_src = anketa_src_to_turto_src(anketa_src)
        turto_url = (static_url(turto_src) if turto_src.startswith("/rinkimai/") else urljoin(list_url, turto_src))

        d_a = cells[1].find("a")
        district_name = cells[1].get_text(strip=True)
        if d_a:
            d_href = d_a.get("href", "")
            district_url = spa_display_url(site_prefix, d_href[8:]) if d_href.startswith("?srcUrl=") and site_prefix else urljoin(list_url, d_href)
        else:
            district_url = None

        p_a = cells[2].find("a")
        party_name = cells[2].get_text(strip=True)
        if p_a:
            p_href = p_a.get("href", "")
            party_url = spa_display_url(site_prefix, p_href[8:]) if p_href.startswith("?srcUrl=") and site_prefix else urljoin(list_url, p_href)
        else:
            party_url = None

        members.append(MemberInfo(
            name=name, anketa_url=anketa_url, turto_url=turto_url,
            district_name=district_name, district_url=district_url,
            party_name=party_name, party_url=party_url,
        ))

    return members


def scrape_year(year: int, config: dict, conn: sqlite3.Connection):
    currency = config["currency"]
    site_prefix = config.get("site_prefix")

    if "list_src" in config:
        list_url = static_url(config["list_src"])
        display_list_url = spa_display_url(site_prefix, config["list_src"])
    else:
        list_url = config["list_url"]
        display_list_url = list_url

    log.info(f"=== {year} ===")
    election_id = upsert_election(conn, year, display_list_url)

    html = fetch(list_url)
    if not html:
        log.error(f"  Could not fetch list for {year}")
        return

    members = parse_member_list(html, list_url, site_prefix)
    log.info(f"  Nariai: {len(members)}")

    for i, m in enumerate(members, 1):
        member_id = upsert_member(conn, election_id, m)
        if member_id is None:
            continue
        if already_scraped(conn, member_id):
            log.debug(f"  [{i}/{len(members)}] {m.name} – praleista")
            continue
        log.info(f"  [{i}/{len(members)}] {m.name}")
        turto_html = fetch(m.turto_url)
        if turto_html:
            d = parse_declaration(turto_html, m.turto_url, currency)
            save_declaration(conn, member_id, d)
        time.sleep(REQUEST_DELAY)


def generate_html(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute('''
        SELECT m.name, e.year as election_year,
               d.declaration_year, d.turto_url,
               d.mandatory_property, d.securities, d.monetary_funds,
               d.loans_given, d.loans_received, d.total_income,
               d.income_tax, m.party_name
        FROM members m
        JOIN elections e ON m.election_id = e.id
        LEFT JOIN declarations d ON d.member_id = m.id
        ORDER BY m.name, e.year
    ''')

    members_dict = defaultdict(lambda: {"name": "", "declarations": []})

    def is_mixed_case(s):
        return s != s.upper()

    for row in cur.fetchall():
        name = row["name"]
        key = name.upper()
        existing = members_dict[key]["name"]
        if not existing or (not is_mixed_case(existing) and is_mixed_case(name)):
            members_dict[key]["name"] = name
        members_dict[key]["declarations"].append({
            "election_year": row["election_year"],
            "declaration_year": row["declaration_year"],
            "turto_url": row["turto_url"] or "",
            "party_name": row["party_name"] or "",
            "mandatory_property": row["mandatory_property"],
            "securities": row["securities"],
            "monetary_funds": row["monetary_funds"],
            "loans_given": row["loans_given"],
            "loans_received": row["loans_received"],
            "total_income": row["total_income"],
            "income_tax": row["income_tax"],
        })

    members_list = sorted(members_dict.values(), key=lambda x: x["name"])
    n = len(members_list)
    data_json = json.dumps(members_list, ensure_ascii=False, separators=(',', ':'))

    html = f'''<!DOCTYPE html>
<html lang="lt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Seimo narių turto deklaracijos</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
/* ── Bendra ── */
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, sans-serif; background: #f0f2f5; color: #222; }}
#m {{ display: none; }}

/* ══════════════════════════════════════
   DESKTOP  (#d)
══════════════════════════════════════ */
#d {{ height: 100vh; display: flex; flex-direction: column; }}
#d header {{ background: #1a3a5c; color: #fff; padding: 18px 32px; flex-shrink: 0; }}
#d header h1 {{ font-size: 1.4rem; font-weight: 600; }}
#d header p {{ font-size: 0.85rem; opacity: 0.75; margin-top: 4px; }}
#d .layout {{ display: flex; flex: 1; overflow: hidden; }}
#d .sidebar {{ width: 320px; min-width: 220px; background: #fff; border-right: 1px solid #dde; display: flex; flex-direction: column; }}
#d .search-wrap {{ padding: 12px; border-bottom: 1px solid #eee; }}
#d .search-wrap input {{ width: 100%; padding: 8px 12px; border: 1px solid #ccd; border-radius: 6px; font-size: 0.9rem; outline: none; }}
#d .search-wrap input:focus {{ border-color: #1a3a5c; }}
#d .sort-wrap {{ padding: 6px 12px; border-bottom: 1px solid #eee; display: flex; align-items: center; gap: 8px; }}
#d .sort-wrap select {{ flex: 1; padding: 4px 6px; border: 1px solid #ccd; border-radius: 5px; font-size: 0.78rem; color: #444; background: #fff; outline: none; cursor: pointer; }}
#d .sort-wrap select:focus {{ border-color: #1a3a5c; }}
#d .member-count {{ font-size: 0.75rem; color: #aaa; white-space: nowrap; }}
#d .member-list {{ overflow-y: auto; flex: 1; }}
#d .member-item {{ padding: 10px 16px; cursor: pointer; border-bottom: 1px solid #f0f0f0; font-size: 0.88rem; transition: background 0.15s; }}
#d .member-item:hover {{ background: #e8f0fb; }}
#d .member-item.active {{ background: #1a3a5c; color: #fff; }}
#d .member-item .elections-badge {{ font-size: 0.72rem; color: #888; margin-top: 2px; }}
#d .member-item.active .elections-badge {{ color: #aac4e8; }}
#d .main {{ flex: 1; overflow-y: auto; padding: 24px 28px; }}
#d .placeholder {{ display: flex; align-items: center; justify-content: center; height: 100%; color: #aaa; font-size: 1rem; }}
#d .member-header {{ margin-bottom: 18px; }}
#d .member-header h2 {{ font-size: 1.25rem; color: #1a3a5c; }}
#d .member-header p {{ font-size: 0.82rem; color: #777; margin-top: 4px; }}
#d .table-wrap {{ overflow-x: auto; margin-bottom: 28px; }}
#d table {{ border-collapse: collapse; min-width: 600px; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 6px rgba(0,0,0,0.08); }}
#d thead tr {{ background: #1a3a5c; color: #fff; }}
#d th {{ padding: 10px 14px; font-size: 0.82rem; font-weight: 600; text-align: right; white-space: nowrap; }}
#d th:first-child {{ text-align: left; }}
#d th.year-link {{ cursor: pointer; text-decoration: underline; text-underline-offset: 3px; }}
#d th.year-link:hover {{ background: #2a5a8c; }}
#d tbody tr {{ border-bottom: 1px solid #eef; transition: background 0.12s; }}
#d tbody tr:last-child {{ border-bottom: none; }}
#d tbody tr:hover {{ background: #e8f0fb; cursor: pointer; }}
#d tbody tr.selected {{ background: #d0e4ff; }}
#d td {{ padding: 9px 14px; font-size: 0.84rem; text-align: right; white-space: nowrap; }}
#d td:first-child {{ text-align: left; font-weight: 500; color: #1a3a5c; }}
#d .null-val {{ color: #bbb; }}
#d .click-hint {{ font-size: 0.75rem; color: #999; margin-bottom: 8px; }}
#d .delta-pos {{ color: #1a8a3a; font-size: 0.75rem; margin-left: 4px; }}
#d .delta-neg {{ color: #c0392b; font-size: 0.75rem; margin-left: 4px; }}
#d .delta-pct {{ color: #5570a0; font-size: 0.75rem; margin-left: 4px; }}
#d .chart-section {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 6px rgba(0,0,0,0.08); padding: 20px; }}
#d .chart-section h3 {{ font-size: 1rem; color: #1a3a5c; margin-bottom: 14px; }}
#d .chart-container {{ position: relative; height: 300px; }}

/* ══════════════════════════════════════
   MOBILE  (#m)
══════════════════════════════════════ */
#m {{ -webkit-tap-highlight-color: transparent; overflow-x: hidden; height: 100dvh; display: flex; flex-direction: column; }}
#m * {{ box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }}
#m header {{ background: #1a3a5c; color: #fff; padding: 14px 16px; position: sticky; top: 0; z-index: 100; display: flex; align-items: center; gap: 10px; min-height: 56px; flex-shrink: 0; }}
#m header h1 {{ font-size: 1rem; font-weight: 600; flex: 1; line-height: 1.3; }}
#m header p {{ font-size: 0.72rem; opacity: 0.7; margin-top: 2px; }}
#m .back-btn {{ display: none; background: rgba(255,255,255,0.15); border: none; color: #fff; font-size: 1.3rem; width: 38px; height: 38px; border-radius: 8px; cursor: pointer; align-items: center; justify-content: center; flex-shrink: 0; }}
#m .back-btn.visible {{ display: flex; }}
#m #m_listView {{ display: flex; flex-direction: column; flex: 1; overflow: hidden; }}
#m #m_detailView {{ display: none; flex: 1; overflow-y: auto; }}
#m .search-bar {{ padding: 10px 12px; background: #fff; border-bottom: 1px solid #e0e3e8; display: flex; gap: 8px; flex-shrink: 0; }}
#m .search-bar input {{ flex: 1; padding: 9px 12px; border: 1.5px solid #ccd; border-radius: 8px; font-size: 1rem; outline: none; background: #f7f8fa; }}
#m .search-bar input:focus {{ border-color: #1a3a5c; background: #fff; }}
#m .sort-bar {{ padding: 6px 12px; background: #fff; border-bottom: 1px solid #e0e3e8; display: flex; align-items: center; gap: 8px; flex-shrink: 0; }}
#m .sort-bar select {{ flex: 1; padding: 6px 8px; border: 1.5px solid #ccd; border-radius: 8px; font-size: 0.82rem; color: #444; background: #fff; outline: none; }}
#m .sort-bar select:focus {{ border-color: #1a3a5c; }}
#m .member-count {{ font-size: 0.75rem; color: #999; white-space: nowrap; }}
#m .member-list {{ overflow-y: auto; flex: 1; }}
#m .member-item {{ padding: 13px 16px; border-bottom: 1px solid #ebebf0; background: #fff; cursor: pointer; display: flex; justify-content: space-between; align-items: center; -webkit-user-select: none; user-select: none; }}
#m .member-item:active {{ background: #e8f0fb; }}
#m .member-item-left .name {{ font-size: 0.93rem; font-weight: 500; }}
#m .member-item-left .years {{ font-size: 0.75rem; color: #999; margin-top: 2px; }}
#m .member-arrow {{ color: #c0c8d8; font-size: 1rem; flex-shrink: 0; margin-left: 8px; }}
#m .detail-inner {{ padding: 0 0 32px; }}
#m .detail-hero {{ background: #fff; padding: 16px; border-bottom: 1px solid #e8ebf0; margin-bottom: 12px; }}
#m .detail-hero h2 {{ font-size: 1.1rem; color: #1a3a5c; font-weight: 700; }}
#m .detail-hero p {{ font-size: 0.8rem; color: #888; margin-top: 4px; }}
#m .year-cards {{ padding: 0 12px; display: flex; flex-direction: column; gap: 12px; }}
#m .year-card {{ background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
#m .year-card-header {{ background: #1a3a5c; color: #fff; padding: 10px 14px; display: flex; align-items: center; justify-content: space-between; }}
#m .year-card-header .year-label {{ font-size: 1rem; font-weight: 700; }}
#m .year-card-header .party-label {{ font-size: 0.75rem; opacity: 0.75; text-align: right; max-width: 55%; line-height: 1.3; }}
#m .year-card-header a {{ color: #7ec8ff; font-size: 0.75rem; text-decoration: none; border: 1px solid rgba(126,200,255,0.4); padding: 3px 7px; border-radius: 5px; margin-left: 8px; flex-shrink: 0; }}
#m .field-row {{ display: flex; justify-content: space-between; align-items: flex-start; padding: 8px 14px; border-bottom: 1px solid #f2f4f8; cursor: pointer; transition: background 0.1s; }}
#m .field-row:last-child {{ border-bottom: none; }}
#m .field-row:active {{ background: #eef3fb; }}
#m .field-row.selected-field {{ background: #ddeeff; }}
#m .field-name {{ font-size: 0.78rem; color: #555; flex: 1; padding-right: 10px; line-height: 1.4; }}
#m .field-value {{ font-size: 0.85rem; font-weight: 600; color: #1a3a5c; text-align: right; white-space: nowrap; }}
#m .field-value.null-val {{ color: #bbb; font-weight: 400; }}
#m .delta-pos {{ color: #1a8a3a; font-size: 0.72rem; display: block; text-align: right; }}
#m .delta-neg {{ color: #c0392b; font-size: 0.72rem; display: block; text-align: right; }}
#m .delta-pct {{ color: #5570a0; font-size: 0.72rem; display: block; text-align: right; }}
#m .chart-section {{ margin: 12px; background: #fff; border-radius: 10px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); padding: 14px; }}
#m .chart-section h3 {{ font-size: 0.88rem; color: #1a3a5c; margin-bottom: 12px; line-height: 1.4; }}
#m .chart-placeholder {{ display: flex; align-items: center; justify-content: center; height: 140px; color: #bbb; font-size: 0.85rem; border: 2px dashed #e0e0e0; border-radius: 8px; text-align: center; padding: 12px; }}
#m .chart-container {{ position: relative; height: 240px; }}
#m .empty-state {{ text-align: center; padding: 48px 24px; color: #aaa; font-size: 0.9rem; }}

/* ── Perjungimas pagal ekrano plotį ── */
@media (max-width: 767px) {{
  #d {{ display: none !important; }}
  #m {{ display: flex !important; }}
}}
</style>
</head>
<body>

<!-- ══ DESKTOP ══ -->
<div id="d">
  <header>
    <h1>Seimo narių turto deklaracijos</h1>
    <p>Rinkimai 2008&ndash;2024 &bull; {n} nariai &bull; 703 deklaracijos</p>
  </header>
  <div class="layout">
    <aside class="sidebar">
      <div class="search-wrap">
        <input type="text" id="d_search" placeholder="Ieškoti nario..." autocomplete="off">
      </div>
      <div class="sort-wrap">
        <select id="d_sort">
          <option value="name">Pagal pavardę</option>
          <option value="mandatory_property">Privalomas registruoti turtas</option>
          <option value="securities">Vertybiniai popieriai ir kt.</option>
          <option value="monetary_funds">Piniginės lėšos</option>
          <option value="loans_given">Suteiktos paskolos</option>
          <option value="loans_received">Gautos paskolos</option>
          <option value="total_income">Bendros pajamos</option>
          <option value="income_tax">Pajamų mokestis</option>
        </select>
        <span class="member-count" id="d_count"></span>
      </div>
      <div class="member-list" id="d_list"></div>
    </aside>
    <main class="main" id="d_main">
      <div class="placeholder">&#8592; Pasirinkite Seimo narį iš sąrašo</div>
    </main>
  </div>
</div>

<!-- ══ MOBILE ══ -->
<div id="m">
  <header>
    <button class="back-btn" id="m_back">&#8592;</button>
    <div style="flex:1">
      <h1 id="m_title">Seimo narių turto deklaracijos</h1>
      <p id="m_sub">Rinkimai 2008&ndash;2024</p>
    </div>
  </header>
  <div id="m_listView">
    <div class="search-bar">
      <input type="search" id="m_search" placeholder="Ieškoti nario..." autocomplete="off" enterkeyhint="search">
    </div>
    <div class="sort-bar">
      <select id="m_sort">
        <option value="name">Rikiuoti pagal pavardę</option>
        <option value="mandatory_property">Privalomas turtas</option>
        <option value="securities">Vertybiniai popieriai</option>
        <option value="monetary_funds">Piniginės lėšos</option>
        <option value="loans_given">Suteiktos paskolos</option>
        <option value="loans_received">Gautos paskolos</option>
        <option value="total_income">Bendros pajamos</option>
        <option value="income_tax">Pajamų mokestis</option>
      </select>
      <span class="member-count" id="m_count"></span>
    </div>
    <div class="member-list" id="m_list"></div>
  </div>
  <div id="m_detailView">
    <div class="detail-inner" id="m_detail"></div>
  </div>
</div>

<script>
const DATA = {data_json};

const FIELDS = [
  {{ key: 'mandatory_property', lt: 'Privalomas registruoti turtas' }},
  {{ key: 'securities',         lt: 'Vertybiniai popieriai, meno ir juvelyriniai dirbiniai' }},
  {{ key: 'monetary_funds',     lt: 'Piniginės lėšos' }},
  {{ key: 'loans_given',        lt: 'Suteiktos paskolos' }},
  {{ key: 'loans_received',     lt: 'Gautos paskolos' }},
  {{ key: 'total_income',       lt: 'Bendros pajamos' }},
  {{ key: 'income_tax',         lt: 'Pajamų mokestis', percentOf: 'total_income' }},
];

/* ── Bendros pagalbinės funkcijos ── */
function lastName(name) {{
  const p = name.trim().split(/\\s+/);
  return p[p.length - 1];
}}
function maxVal(member, key) {{
  const vals = member.declarations.map(d => d[key]).filter(v => v !== null && v !== undefined);
  return vals.length ? Math.max(...vals) : null;
}}
function sortedMembers(data, sort) {{
  const arr = [...data];
  if (sort === 'name') return arr.sort((a, b) => lastName(a.name).localeCompare(lastName(b.name), 'lt', {{sensitivity: 'base'}}));
  return arr.sort((a, b) => {{
    const va = maxVal(a, sort), vb = maxVal(b, sort);
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    return vb - va;
  }});
}}
function fmt(v) {{
  if (v === null || v === undefined) return null;
  return Number(v).toLocaleString('lt-LT', {{minimumFractionDigits: 0, maximumFractionDigits: 0}}) + ' €';
}}
function fmtDelta(curr, prev) {{
  if (curr === null || curr === undefined || prev === null || prev === undefined) return '';
  const d = curr - prev;
  if (d === 0) return '';
  const sign = d > 0 ? '+' : '';
  const cls = d > 0 ? 'delta-pos' : 'delta-neg';
  return `<span class="${{cls}}">${{sign}}${{Number(d).toLocaleString('lt-LT', {{minimumFractionDigits: 0, maximumFractionDigits: 0}})}} €</span>`;
}}
function fmtPercent(v, base) {{
  if (v === null || v === undefined || !base) return '';
  return `<span class="delta-pct">(${{(v / base * 100).toFixed(1)}}%)</span>`;
}}

/* viewport fix tik tikram mobiliajam */
if (window.matchMedia('(max-width: 767px)').matches) {{
  document.querySelector('meta[name=viewport]').content =
    'width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no';
}}

/* ════════════════════════════════════
   DESKTOP
════════════════════════════════════ */
{{
  let dChart = null;
  let dSort = 'name';

  function dRenderList(filter) {{
    const list = document.getElementById('d_list');
    const lc = (filter || '').toLowerCase();
    const filtered = lc ? DATA.filter(m => m.name.toLowerCase().includes(lc)) : DATA;
    const sorted = sortedMembers(filtered, dSort);
    document.getElementById('d_count').textContent = sorted.length;
    list.innerHTML = '';
    sorted.forEach(m => {{
      const years = m.declarations.map(d => d.election_year).sort();
      const div = document.createElement('div');
      div.className = 'member-item';
      div.innerHTML = `<div>${{m.name}}</div><div class="elections-badge">Rinkimai: ${{years.join(', ')}}</div>`;
      div.addEventListener('click', () => {{
        document.querySelectorAll('#d_list .member-item').forEach(e => e.classList.remove('active'));
        div.classList.add('active');
        dRenderMember(m);
      }});
      list.appendChild(div);
    }});
  }}

  function dRenderMember(member) {{
    const main = document.getElementById('d_main');
    const decs = [...member.declarations].sort((a, b) => a.election_year - b.election_year);
    const years = decs.map(d => d.election_year);
    const multi = decs.length > 1;

    const tableHead = '<th>Rodiklis</th>' + decs.map(d =>
      d.turto_url
        ? `<th class="year-link" data-url="${{d.turto_url}}">${{d.election_year}}</th>`
        : `<th>${{d.election_year}}</th>`
    ).join('');

    let tableBody = '';
    FIELDS.forEach(field => {{
      const vals = decs.map(d => d[field.key]);
      tableBody += `<tr data-field="${{field.key}}" data-label="${{field.lt}}">
        <td>${{field.lt}}</td>
        ${{vals.map((v, i) => {{
          const extra = field.percentOf
            ? fmtPercent(v, decs[i][field.percentOf])
            : (multi && i > 0 ? fmtDelta(v, vals[i - 1]) : '');
          const f = fmt(v);
          return `<td>${{f !== null ? f : '<span class="null-val">—</span>'}}</td>`
            .replace('</td>', extra + '</td>');
        }}).join('')}}
      </tr>`;
    }});

    const parties = [...new Set(decs.map(d => d.party_name).filter(Boolean))].join(', ');
    main.innerHTML = `
      <div class="member-header">
        <h2>${{member.name}}</h2>
        ${{parties ? `<p>Partija(-os): ${{parties}}</p>` : ''}}
      </div>
      <p class="click-hint">Spustelėkite ant eilutės, kad pamatytumėte grafiką</p>
      <div class="table-wrap">
        <table>
          <thead><tr>${{tableHead}}</tr></thead>
          <tbody id="d_decTable">${{tableBody}}</tbody>
        </table>
      </div>
      <div class="chart-section">
        <h3 id="d_chartTitle">Pasirinkite rodiklį grafikui</h3>
        <div class="chart-container"><canvas id="d_chart"></canvas></div>
      </div>
    `;

    if (dChart) {{ dChart.destroy(); dChart = null; }}
    document.querySelectorAll('th.year-link').forEach(th =>
      th.addEventListener('click', () => window.open(th.dataset.url, '_blank'))
    );
    document.querySelectorAll('#d_decTable tr').forEach(tr =>
      tr.addEventListener('click', () => {{
        document.querySelectorAll('#d_decTable tr').forEach(r => r.classList.remove('selected'));
        tr.classList.add('selected');
        dShowChart(years, decs.map(d => d[tr.dataset.field]), tr.dataset.label, member.name);
      }})
    );
  }}

  function dShowChart(years, vals, label, memberName) {{
    document.getElementById('d_chartTitle').textContent = label + ' — ' + memberName;
    if (dChart) dChart.destroy();
    dChart = new Chart(document.getElementById('d_chart').getContext('2d'), {{
      type: 'bar',
      data: {{
        labels: years.map(String),
        datasets: [{{
          label: label,
          data: vals.map(v => v ?? 0),
          backgroundColor: years.map((_, i) => `hsla(${{200 + i * 30}}, 65%, 48%, 0.78)`),
          borderColor: years.map((_, i) => `hsla(${{200 + i * 30}}, 65%, 35%, 1)`),
          borderWidth: 1.5, borderRadius: 4,
        }}]
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{ callbacks: {{ label: c => ' ' + c.raw.toLocaleString('lt-LT', {{minimumFractionDigits: 0, maximumFractionDigits: 0}}) + ' €' }} }}
        }},
        scales: {{ y: {{ beginAtZero: true, ticks: {{ callback: v => v.toLocaleString('lt-LT') + ' €' }} }} }}
      }}
    }});
  }}

  dRenderList('');
  document.getElementById('d_search').addEventListener('input', e => dRenderList(e.target.value));
  document.getElementById('d_sort').addEventListener('change', e => {{
    dSort = e.target.value;
    dRenderList(document.getElementById('d_search').value);
  }});
}}

/* ════════════════════════════════════
   MOBILE
════════════════════════════════════ */
{{
  let mChart = null;
  let mSort = 'name';
  let mCurrentMember = null;

  function mRenderList(filter) {{
    const list = document.getElementById('m_list');
    const lc = (filter || '').toLowerCase();
    const filtered = lc ? DATA.filter(m => m.name.toLowerCase().includes(lc)) : DATA;
    const sorted = sortedMembers(filtered, mSort);
    document.getElementById('m_count').textContent = sorted.length + ' nariai(-ių)';
    if (!sorted.length) {{
      list.innerHTML = '<div class="empty-state">Nerasta narių pagal paiešką</div>';
      return;
    }}
    list.innerHTML = '';
    sorted.forEach(m => {{
      const years = [...new Set(m.declarations.map(d => d.election_year))].sort();
      const div = document.createElement('div');
      div.className = 'member-item';
      div.innerHTML = `
        <div class="member-item-left">
          <div class="name">${{m.name}}</div>
          <div class="years">Rinkimai: ${{years.join(', ')}}</div>
        </div>
        <span class="member-arrow">&#8250;</span>
      `;
      div.addEventListener('click', () => mShowDetail(m));
      list.appendChild(div);
    }});
  }}

  function mShowDetail(member) {{
    mCurrentMember = member;
    mRenderDetail(member);
    document.getElementById('m_listView').style.display = 'none';
    document.getElementById('m_detailView').style.display = 'block';
    document.getElementById('m_detailView').scrollTop = 0;
    document.getElementById('m_back').classList.add('visible');
    document.getElementById('m_title').textContent = member.name;
    document.getElementById('m_sub').style.display = 'none';
    if (mChart) {{ mChart.destroy(); mChart = null; }}
  }}

  function mShowList() {{
    document.getElementById('m_listView').style.display = 'flex';
    document.getElementById('m_detailView').style.display = 'none';
    document.getElementById('m_back').classList.remove('visible');
    document.getElementById('m_title').textContent = 'Seimo narių turto deklaracijos';
    document.getElementById('m_sub').style.display = '';
    if (mChart) {{ mChart.destroy(); mChart = null; }}
  }}

  function mRenderDetail(member) {{
    const decs = [...member.declarations].sort((a, b) => a.election_year - b.election_year);
    const multi = decs.length > 1;
    const parties = [...new Set(decs.map(d => d.party_name).filter(Boolean))].join(', ');

    let cardsHtml = '';
    decs.forEach((dec, idx) => {{
      const prev = idx > 0 ? decs[idx - 1] : null;
      let fieldsHtml = '';
      FIELDS.forEach(field => {{
        const v = dec[field.key];
        const f = fmt(v);
        const extra = field.percentOf
          ? fmtPercent(v, dec[field.percentOf])
          : (multi && prev ? fmtDelta(v, prev[field.key]) : '');
        fieldsHtml += `
          <div class="field-row" data-field="${{field.key}}" data-label="${{field.lt}}">
            <div class="field-name">${{field.lt}}</div>
            <div class="field-value${{f === null ? ' null-val' : ''}}">${{f !== null ? f : '—'}}${{extra}}</div>
          </div>`;
      }});
      const link = dec.turto_url
        ? `<a href="${{dec.turto_url}}" target="_blank" rel="noopener">Originalas &#8599;</a>`
        : '';
      cardsHtml += `
        <div class="year-card">
          <div class="year-card-header">
            <span class="year-label">${{dec.election_year}}</span>
            <span class="party-label">${{dec.party_name || ''}}</span>
            ${{link}}
          </div>
          <div class="year-card-body">${{fieldsHtml}}</div>
        </div>`;
    }});

    document.getElementById('m_detail').innerHTML = `
      <div class="detail-hero">
        <h2>${{member.name}}</h2>
        ${{parties ? `<p>Partija(-os): ${{parties}}</p>` : ''}}
      </div>
      <div class="year-cards">${{cardsHtml}}</div>
      <div class="chart-section" id="m_chartSection">
        <h3 id="m_chartTitle">Paspauskite ant lauko, kad pamatytumėte grafiką</h3>
        <div class="chart-placeholder" id="m_chartPh">&#128196; Pasirinkite rodiklį iš kortelių aukščiau</div>
        <div class="chart-container" id="m_chartBox" style="display:none"><canvas id="m_chart"></canvas></div>
      </div>
    `;

    document.getElementById('m_detail').addEventListener('click', e => {{
      const row = e.target.closest('.field-row');
      if (!row) return;
      document.querySelectorAll('#m_detail .field-row').forEach(r => r.classList.remove('selected-field'));
      row.classList.add('selected-field');
      const decs2 = [...mCurrentMember.declarations].sort((a, b) => a.election_year - b.election_year);
      mShowChart(
        decs2.map(d => d.election_year),
        decs2.map(d => d[row.dataset.field]),
        row.dataset.label,
        mCurrentMember.name
      );
      setTimeout(() => document.getElementById('m_chartSection')
        .scrollIntoView({{behavior: 'smooth', block: 'nearest'}}), 100);
    }});
  }}

  function mShowChart(years, vals, label, memberName) {{
    document.getElementById('m_chartTitle').textContent = label + ' — ' + memberName;
    document.getElementById('m_chartPh').style.display = 'none';
    document.getElementById('m_chartBox').style.display = 'block';
    if (mChart) mChart.destroy();
    mChart = new Chart(document.getElementById('m_chart').getContext('2d'), {{
      type: 'bar',
      data: {{
        labels: years.map(String),
        datasets: [{{
          label: label,
          data: vals.map(v => v ?? 0),
          backgroundColor: years.map((_, i) => `hsla(${{200 + i * 30}}, 65%, 48%, 0.82)`),
          borderColor: years.map((_, i) => `hsla(${{200 + i * 30}}, 65%, 35%, 1)`),
          borderWidth: 1.5, borderRadius: 5,
        }}]
      }},
      options: {{
        responsive: true, maintainAspectRatio: false,
        plugins: {{
          legend: {{ display: false }},
          tooltip: {{ callbacks: {{ label: c => ' ' + c.raw.toLocaleString('lt-LT', {{minimumFractionDigits: 0, maximumFractionDigits: 0}}) + ' €' }} }}
        }},
        scales: {{
          y: {{ beginAtZero: true, ticks: {{ callback: v => v.toLocaleString('lt-LT') + ' €', maxTicksLimit: 6, font: {{size: 11}} }} }},
          x: {{ ticks: {{ font: {{size: 12}} }} }}
        }}
      }}
    }});
  }}

  document.getElementById('m_back').addEventListener('click', mShowList);
  mRenderList('');
  document.getElementById('m_search').addEventListener('input', e => mRenderList(e.target.value));
  document.getElementById('m_sort').addEventListener('change', e => {{
    mSort = e.target.value;
    mRenderList(document.getElementById('m_search').value);
  }});
}}
</script>
</body>
</html>
'''

    with open(HTML_FILE, 'w', encoding='utf-8') as f:
        f.write(html)

    log.info(f"Sugeneruota {HTML_FILE} su {n} unikaliais nariais (desktop + mobile)")


def display_url_to_static(url: str) -> str:
    if "srcUrl=" in url:
        src = parse_qs(url.split("?", 1)[-1]).get("srcUrl", [""])[0]
        return static_url(src) if src else url
    return url


def parse_name_from_declaration(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    SKIP = re.compile(
        r'^(METINĖS|I\.|II\.|III\.|IV\.|V\.|GPM|Gautų|Išskaičiuota'
        r'|Vienmandatė|Iškėlė|Turas|Sąrašas|Numeris|Porinkiminis'
        r'|Apygarda|Kandidatai|\d)', re.I
    )
    def clean(s: str) -> str:
        return re.sub(r'\s+', ' ', s.replace('\xa0', ' ')).strip()

    for tr in soup.find_all('tr')[:6]:
        for td in tr.find_all(['td', 'th']):
            # 2016 format: vardas yra pirmasis teksto mazgas prieš <b>
            for child in td.children:
                if isinstance(child, NavigableString):
                    name = clean(str(child))
                    if name and len(name) > 3 and not SKIP.match(name):
                        return name
                    break
            # 2008 format: vardas yra pirmame <b> vaikiniame elemente
            b = td.find('b', recursive=False)
            if b:
                name = clean(b.get_text(strip=True))
                if name and len(name) > 3 and not SKIP.match(name):
                    return name
    return "Nežinomas narys"


def scrape_manual_members(conn: sqlite3.Connection):
    for year, urls in MANUAL_MEMBERS.items():
        row = conn.execute("SELECT id FROM elections WHERE year=?", (year,)).fetchone()
        if not row:
            log.warning(f"Rinkimų {year} nėra DB – praleista")
            continue
        election_id = row[0]
        currency = ELECTIONS[year]["currency"]

        for display_url in urls:
            static = display_url_to_static(display_url)
            existing = conn.execute(
                "SELECT id FROM members WHERE election_id=? AND anketa_url=?",
                (election_id, static)
            ).fetchone()
            if existing and already_scraped(conn, existing[0]):
                log.debug(f"  [{year}] {static} – jau yra DB")
                continue

            log.info(f"  Papildomas narys [{year}]: {static}")
            html = fetch(static)
            if not html:
                log.warning(f"  Nepavyko gauti: {static}")
                continue

            name = parse_name_from_declaration(html)
            log.info(f"    Vardas: {name}")

            member_id = upsert_member(conn, election_id, MemberInfo(
                name=name, anketa_url=static, turto_url=static,
                district_name="", district_url=None,
                party_name="", party_url=None,
            ))
            if member_id:
                save_declaration(conn, member_id, parse_declaration(html, static, currency))
            time.sleep(REQUEST_DELAY)


def main():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)

    for year in sorted(ELECTIONS.keys(), reverse=True):
        try:
            scrape_year(year, ELECTIONS[year], conn)
        except Exception as e:
            log.error(f"Klaida {year}: {e}", exc_info=True)

    scrape_manual_members(conn)
    generate_html(conn)
    conn.close()
    log.info("Baigta.")


if __name__ == "__main__":
    main()
