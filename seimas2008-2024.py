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
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

DB_FILE = "seimas.db"
HTML_FILE = "seimas2008-2024.html"
STATIC_BASE = "https://www.vrk.lt/statiniai/puslapiai"
REQUEST_DELAY = 1.0

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
    data_json = json.dumps(members_list, ensure_ascii=False, separators=(',', ':'))

    html = f'''<!DOCTYPE html>
<html lang="lt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Seimo narių turto deklaracijos</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #f0f2f5; color: #222; }}
  header {{ background: #1a3a5c; color: #fff; padding: 18px 32px; }}
  header h1 {{ font-size: 1.4rem; font-weight: 600; }}
  header p {{ font-size: 0.85rem; opacity: 0.75; margin-top: 4px; }}
  .layout {{ display: flex; height: calc(100vh - 72px); }}
  .sidebar {{ width: 320px; min-width: 220px; background: #fff; border-right: 1px solid #dde; display: flex; flex-direction: column; }}
  .search-wrap {{ padding: 12px; border-bottom: 1px solid #eee; }}
  .search-wrap input {{ width: 100%; padding: 8px 12px; border: 1px solid #ccd; border-radius: 6px; font-size: 0.9rem; outline: none; }}
  .search-wrap input:focus {{ border-color: #1a3a5c; }}
  .sort-wrap {{ padding: 6px 12px; border-bottom: 1px solid #eee; display: flex; align-items: center; gap: 8px; }}
  .sort-wrap select {{ flex: 1; padding: 4px 6px; border: 1px solid #ccd; border-radius: 5px; font-size: 0.78rem; color: #444; background: #fff; outline: none; cursor: pointer; }}
  .sort-wrap select:focus {{ border-color: #1a3a5c; }}
  .member-count {{ font-size: 0.75rem; color: #aaa; white-space: nowrap; }}
  .member-list {{ overflow-y: auto; flex: 1; }}
  .member-item {{ padding: 10px 16px; cursor: pointer; border-bottom: 1px solid #f0f0f0; font-size: 0.88rem; transition: background 0.15s; }}
  .member-item:hover {{ background: #e8f0fb; }}
  .member-item.active {{ background: #1a3a5c; color: #fff; }}
  .member-item .elections-badge {{ font-size: 0.72rem; color: #888; margin-top: 2px; }}
  .member-item.active .elections-badge {{ color: #aac4e8; }}
  .main {{ flex: 1; overflow-y: auto; padding: 24px 28px; }}
  .placeholder {{ display: flex; align-items: center; justify-content: center; height: 100%; color: #aaa; font-size: 1rem; }}
  .member-header {{ margin-bottom: 18px; }}
  .member-header h2 {{ font-size: 1.25rem; color: #1a3a5c; }}
  .member-header p {{ font-size: 0.82rem; color: #777; margin-top: 4px; }}
  .table-wrap {{ overflow-x: auto; margin-bottom: 28px; }}
  table {{ border-collapse: collapse; min-width: 600px; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 6px rgba(0,0,0,0.08); }}
  thead tr {{ background: #1a3a5c; color: #fff; }}
  th {{ padding: 10px 14px; font-size: 0.82rem; font-weight: 600; text-align: right; white-space: nowrap; }}
  th:first-child {{ text-align: left; }}
  th.year-link {{ cursor: pointer; text-decoration: underline; text-underline-offset: 3px; }}
  th.year-link:hover {{ background: #2a5a8c; }}
  tbody tr {{ border-bottom: 1px solid #eef; transition: background 0.12s; }}
  tbody tr:last-child {{ border-bottom: none; }}
  tbody tr:hover {{ background: #e8f0fb; cursor: pointer; }}
  tbody tr.selected {{ background: #d0e4ff; }}
  td {{ padding: 9px 14px; font-size: 0.84rem; text-align: right; white-space: nowrap; }}
  td:first-child {{ text-align: left; font-weight: 500; color: #1a3a5c; }}
  .null-val {{ color: #bbb; }}
  .click-hint {{ font-size: 0.75rem; color: #999; margin-bottom: 8px; }}
  .delta-pos {{ color: #1a8a3a; font-size: 0.75rem; margin-left: 4px; }}
  .delta-neg {{ color: #c0392b; font-size: 0.75rem; margin-left: 4px; }}
  .delta-pct {{ color: #5570a0; font-size: 0.75rem; margin-left: 4px; }}
  .chart-section {{ background: #fff; border-radius: 8px; box-shadow: 0 1px 6px rgba(0,0,0,0.08); padding: 20px; }}
  .chart-section h3 {{ font-size: 1rem; color: #1a3a5c; margin-bottom: 14px; }}
  .chart-container {{ position: relative; height: 300px; }}
</style>
</head>
<body>
<header>
  <h1>Seimo narių turto deklaracijos</h1>
  <p>Rinkimai 2008–2024 &bull; {len(members_list)} unikalių narių</p>
</header>
<div class="layout">
  <aside class="sidebar">
    <div class="search-wrap">
      <input type="text" id="searchInput" placeholder="Ieškoti nario..." autocomplete="off">
    </div>
    <div class="sort-wrap">
      <select id="sortSelect">
        <option value="name">Pagal pavardę</option>
        <option value="mandatory_property">Privalomas registruoti turtas</option>
        <option value="securities">Vertybiniai popieriai ir kt.</option>
        <option value="monetary_funds">Piniginės lėšos</option>
        <option value="loans_given">Suteiktos paskolos</option>
        <option value="loans_received">Gautos paskolos</option>
        <option value="total_income">Bendros pajamos</option>
        <option value="income_tax">Pajamų mokestis</option>
      </select>
      <span class="member-count" id="memberCount"></span>
    </div>
    <div class="member-list" id="memberList"></div>
  </aside>
  <main class="main" id="main">
    <div class="placeholder">← Pasirinkite Seimo narį iš sąrašo</div>
  </main>
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

let currentChart = null;
let currentSort = 'name';

function lastName(name) {{
  const parts = name.trim().split(/[ ]+/);
  return parts[parts.length - 1];
}}

function maxVal(member, key) {{
  const vals = member.declarations.map(d => d[key]).filter(v => v !== null && v !== undefined);
  return vals.length ? Math.max(...vals) : null;
}}

function sortedMembers(data, sort) {{
  const arr = [...data];
  if (sort === 'name') {{
    return arr.sort((a, b) => lastName(a.name).localeCompare(lastName(b.name), 'lt', {{sensitivity: 'base'}}));
  }}
  return arr.sort((a, b) => {{
    const va = maxVal(a, sort), vb = maxVal(b, sort);
    if (va === null && vb === null) return 0;
    if (va === null) return 1;
    if (vb === null) return -1;
    return vb - va;
  }});
}}

function fmt(v) {{
  if (v === null || v === undefined) return '<span class="null-val">—</span>';
  return Number(v).toLocaleString('lt-LT', {{minimumFractionDigits: 2, maximumFractionDigits: 2}}) + ' €';
}}

function fmtDelta(curr, prev) {{
  if (curr === null || curr === undefined || prev === null || prev === undefined) return '';
  const d = curr - prev;
  if (d === 0) return '';
  const sign = d > 0 ? '+' : '';
  const cls = d > 0 ? 'delta-pos' : 'delta-neg';
  const str = sign + Number(d).toLocaleString('lt-LT', {{minimumFractionDigits: 2, maximumFractionDigits: 2}}) + ' €';
  return `<span class="${{cls}}">(${{str}})</span>`;
}}

function fmtPercent(v, base) {{
  if (v === null || v === undefined || base === null || base === undefined || base === 0) return '';
  const pct = (v / base) * 100;
  return `<span class="delta-pct">(${{pct.toFixed(1)}}%)</span>`;
}}

function renderMemberList(filter) {{
  const list = document.getElementById('memberList');
  const count = document.getElementById('memberCount');
  const lc = (filter || '').toLowerCase();
  const filtered = lc ? DATA.filter(m => m.name.toLowerCase().includes(lc)) : DATA;
  const sorted = sortedMembers(filtered, currentSort);
  count.textContent = sorted.length;
  list.innerHTML = '';
  sorted.forEach(m => {{
    const years = m.declarations.map(d => d.election_year).sort();
    const div = document.createElement('div');
    div.className = 'member-item';
    div.innerHTML = `<div>${{m.name}}</div><div class="elections-badge">Rinkimai: ${{years.join(', ')}}</div>`;
    div.addEventListener('click', () => selectMember(m, div));
    list.appendChild(div);
  }});
}}

function selectMember(member, el) {{
  document.querySelectorAll('.member-item').forEach(e => e.classList.remove('active'));
  el.classList.add('active');
  renderMember(member);
}}

function renderMember(member) {{
  const main = document.getElementById('main');
  const decs = member.declarations.sort((a, b) => a.election_year - b.election_year);
  const years = decs.map(d => d.election_year);

  const tableHead = '<th>Rodiklis</th>' + decs.map(d =>
    d.turto_url
      ? `<th class="year-link" data-url="${{d.turto_url}}">${{d.election_year}}</th>`
      : `<th>${{d.election_year}}</th>`
  ).join('');

  const multiKadencija = decs.length > 1;
  let tableBody = '';

  FIELDS.forEach(field => {{
    const vals = decs.map(d => d[field.key]);
    tableBody += `<tr data-field="${{field.key}}" data-label="${{field.lt}}">
      <td>${{field.lt}}</td>
      ${{vals.map((v, i) => {{
        let extra = '';
        if (field.percentOf) {{
          extra = fmtPercent(v, decs[i][field.percentOf]);
        }} else if (multiKadencija && i > 0) {{
          extra = fmtDelta(v, vals[i - 1]);
        }}
        return `<td>${{fmt(v)}}${{extra}}</td>`;
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
        <tbody id="decTable">${{tableBody}}</tbody>
      </table>
    </div>
    <div class="chart-section">
      <h3 id="chartTitle">Pasirinkite rodiklį grafikui</h3>
      <div class="chart-container"><canvas id="myChart"></canvas></div>
    </div>
  `;

  if (currentChart) {{ currentChart.destroy(); currentChart = null; }}

  document.querySelectorAll('th.year-link').forEach(th => {{
    th.addEventListener('click', () => window.open(th.dataset.url, '_blank'));
  }});

  document.querySelectorAll('#decTable tr').forEach(tr => {{
    tr.addEventListener('click', () => {{
      document.querySelectorAll('#decTable tr').forEach(r => r.classList.remove('selected'));
      tr.classList.add('selected');
      showChart(years, decs.map(d => d[tr.dataset.field]), tr.dataset.label, member.name);
    }});
  }});
}}

function showChart(years, vals, label, memberName) {{
  document.getElementById('chartTitle').textContent = label + ' — ' + memberName;
  if (currentChart) {{ currentChart.destroy(); }}
  const ctx = document.getElementById('myChart').getContext('2d');
  currentChart = new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: years.map(String),
      datasets: [{{
        label: label,
        data: vals.map(v => (v === null || v === undefined) ? 0 : v),
        backgroundColor: years.map((_, i) => `hsla(${{200 + i * 30}}, 65%, 48%, 0.78)`),
        borderColor: years.map((_, i) => `hsla(${{200 + i * 30}}, 65%, 35%, 1)`),
        borderWidth: 1.5,
        borderRadius: 4,
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: ctx => ' ' + ctx.raw.toLocaleString('lt-LT', {{minimumFractionDigits: 2}}) + ' €' }} }}
      }},
      scales: {{ y: {{ beginAtZero: true, ticks: {{ callback: v => v.toLocaleString('lt-LT') + ' €' }} }} }}
    }}
  }});
}}

renderMemberList('');
document.getElementById('searchInput').addEventListener('input', e => renderMemberList(e.target.value));
document.getElementById('sortSelect').addEventListener('change', e => {{
  currentSort = e.target.value;
  renderMemberList(document.getElementById('searchInput').value);
}});
</script>
</body>
</html>
'''

    with open(HTML_FILE, 'w', encoding='utf-8') as f:
        f.write(html)

    log.info(f"Sugeneruota {HTML_FILE} su {len(members_list)} unikaliais nariais")


def main():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    init_db(conn)

    for year in sorted(ELECTIONS.keys(), reverse=True):
        try:
            scrape_year(year, ELECTIONS[year], conn)
        except Exception as e:
            log.error(f"Klaida {year}: {e}", exc_info=True)

    generate_html(conn)
    conn.close()
    log.info("Baigta.")


if __name__ == "__main__":
    main()
