# -*- coding: utf-8 -*-
"""US Stock Thai News Bot — ข่าวหุ้นสหรัฐฯ แปลไทย + วิเคราะห์ ส่งเข้า Telegram"""
import asyncio
import html
import json
import logging
import os
import re
from datetime import datetime, time as dtime, timedelta, timezone as dt_tz
from pathlib import Path
from urllib.parse import quote_plus

THAI_TZ = dt_tz(timedelta(hours=7))

import feedparser
import requests
from deep_translator import GoogleTranslator
from dotenv import load_dotenv
from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
AI_MODEL = os.environ.get("AI_MODEL", "openai/gpt-4o-mini").strip()
HAS_AI = bool(ANTHROPIC_API_KEY or OPENAI_API_KEY or OPENROUTER_API_KEY)
MARKET_NEWS = os.environ.get("MARKET_NEWS", "true").strip().lower() != "false"
GOLD_NEWS = os.environ.get("GOLD_NEWS", "true").strip().lower() != "false"
MACRO_ALERTS = os.environ.get("MACRO_ALERTS", "true").strip().lower() != "false"
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # วินาที

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
DEFAULT_WATCHLIST = ["RKLB", "EOSE", "ASTS", "RDW", "NVDA"]

KNOWN_NAMES = {
    "NVDA": "Nvidia",
    "RKLB": "Rocket Lab",
    "EOSE": "Eos Energy",
    "ASTS": "AST SpaceMobile",
    "RDW": "Redwire",
    "TSLA": "Tesla",
    "PLTR": "Palantir",
    "SOFI": "SoFi",
}

# บทความความเห็น/listicle/เปรียบเทียบ/recap — ไม่ใช่ข่าวจริงของบริษัท ตัดทิ้งก่อนเลย
NOISE_RE = re.compile(
    r"(\b\d+\s+(reasons?|stocks?|things)\b"
    r"|better buy|best stocks?|stocks? to buy( now)?|top stock"
    r"|should you buy|if you'?d invested|too late to buy"
    r"|where will .{3,50} be in|prediction|millionaire|magnificent"
    r"|bull case|bear case|history says|\bvs\.?\s"
    r"|here'?s (why|how)|why .{3,60}\b(today|this week|right now)"
    r"|could (soar|jump|surge|reach|hit|double|triple|be worth)"
    r"|what (to know|you need to know)|need to know|facts? to (note|know)"
    r"|is it (a buy|time to buy)|time to buy|worth buying"
    r"|billionaire|warren buffett|cathie wood"
    r"|what'?s going on with|smart(er)? (buy|investors)"
    r"|means? for (shareholders?|investors?)|what .{3,40}'?s? .{0,15}means"
    r"|despite market|stock (drops?|falls?|sinks?|slides?|tumbles?|rises?|gains?|jumps?) (despite|amid)"
    r"|(rose|fell|jumped|sank|soared|plunged|dropped|rallied|nosediv\w*)\s+(today|this week))",
    re.I,
)

# ประเภทข่าวที่เป็น catalyst จริงในสายตานักลงทุน
HARD_CATALYSTS = {
    "earnings", "guidance", "contract", "analyst", "ma",
    "insider", "index", "offering", "legal",
}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# ---------------------------------------------------------------- data store


def load_data() -> dict:
    if DATA_FILE.exists():
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    else:
        data = {}
    env_chats = [
        int(x) for x in os.environ.get("CHAT_IDS", "").replace(" ", "").split(",") if x
    ]
    env_watch = [
        x for x in os.environ.get("WATCHLIST", "").upper().replace(" ", "").split(",") if x
    ]
    data.setdefault("chats", env_chats)
    data.setdefault("watchlist", env_watch or list(DEFAULT_WATCHLIST))
    data.setdefault("seen", {})
    data.setdefault("recent_titles", [])
    data.setdefault("alerts", [])  # [{chat, ticker, target, side}]
    return data


def save_data(data: dict) -> None:
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------- news fetch


def fetch_news(ticker: str, limit: int = 5) -> list[dict]:
    url = (
        "https://feeds.finance.yahoo.com/rss/2.0/headline"
        f"?s={ticker}&region=US&lang=en-US"
    )
    feed = feedparser.parse(url)
    items = []
    for e in feed.entries[:limit]:
        items.append(
            {
                "id": e.get("id") or e.get("link", ""),
                "title": e.get("title", ""),
                "summary": re.sub(r"<[^>]+>", "", e.get("summary", "")).strip(),
                "link": e.get("link", ""),
                "published": e.get("published", ""),
            }
        )
    return items


_name_cache: dict[str, str] = {}


def get_company_name(ticker: str) -> str:
    if ticker in KNOWN_NAMES:
        return KNOWN_NAMES[ticker]
    if ticker in _name_cache:
        return _name_cache[ticker]
    name = ticker
    try:
        r = requests.get(
            "https://query2.finance.yahoo.com/v1/finance/search",
            params={"q": ticker, "quotesCount": 5, "newsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        for q in r.json().get("quotes", []):
            if q.get("symbol") == ticker and (q.get("shortname") or q.get("longname")):
                name = q.get("shortname") or q["longname"]
                name = re.sub(
                    r"[,.]?\s*(inc|corp|corporation|ltd|plc|co|company)\.?$",
                    "", name, flags=re.I,
                ).strip()
                break
    except Exception as exc:
        log.warning("name lookup %s failed: %s", ticker, exc)
    _name_cache[ticker] = name
    return name


_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "are", "its", "has",
    "have", "will", "stock", "stocks", "shares", "says", "after", "amid",
}


def title_tokens(title: str) -> set[str]:
    t = re.split(r"\s+-\s+", title.lower())[0]  # ตัดชื่อสำนักข่าวท้ายหัวข้อ
    return {w for w in re.findall(r"[a-z0-9']+", t) if len(w) > 2 and w not in _STOPWORDS}


def is_duplicate_title(title: str, recent: list[str]) -> bool:
    toks = title_tokens(title)
    if not toks:
        return False
    for prev in recent:
        ptoks = set(prev.split())
        if not ptoks:
            continue
        union = len(toks | ptoks)
        if union and len(toks & ptoks) / union >= 0.6:
            return True
    return False


GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
MACRO_QUERY = "federal reserve OR inflation OR war OR geopolitical markets when:1d"
GOLD_QUERY = 'gold price OR "gold market" when:1d'
MARKET_QUERY = "US stock market when:1d"


def fetch_feed(query: str, limit: int = 8) -> list[dict]:
    feed = feedparser.parse(GOOGLE_NEWS_RSS.format(q=quote_plus(query)))
    items = []
    for e in feed.entries[:limit]:
        items.append(
            {
                "id": e.get("id") or e.get("link", ""),
                "title": e.get("title", ""),
                "link": e.get("link", ""),
                "published": e.get("published", ""),
            }
        )
    return items


# ---------------------------------------------------------------- translation


def translate_th(text: str) -> str:
    if not text:
        return ""
    try:
        return GoogleTranslator(source="auto", target="th").translate(text[:4500])
    except Exception as exc:
        log.warning("translate failed: %s", exc)
        return text


# ---------------------------------------------------------------- analysis

BULLISH_WORDS = [
    "beat", "beats", "surge", "soar", "record", "upgrade", "upgraded", "raise",
    "raised", "contract", "wins", "win", "won", "partnership", "deal",
    "approval", "approved", "buyback", "growth", "strong", "profit", "jump",
    "rally", "expand", "expands", "expansion", "award", "awarded",
    "breakthrough", "outperform", "buy rating", "all-time high", "milestone",
]
BEARISH_WORDS = [
    "miss", "misses", "missed", "downgrade", "downgraded", "cut", "lawsuit",
    "probe", "investigation", "recall", "decline", "drop", "drops", "fall",
    "falls", "plunge", "layoff", "layoffs", "delay", "delayed", "warning",
    "weak", "loss", "losses", "sell rating", "underperform", "bankruptcy",
    "dilution", "offering", "short report", "fraud", "halt", "halted",
]


def heuristic_analysis(title: str, summary: str) -> dict:
    text = f"{title} {summary}".lower()
    bull = sum(1 for w in BULLISH_WORDS if w in text)
    bear = sum(1 for w in BEARISH_WORDS if w in text)
    total = bull + bear
    if total == 0:
        return {"bullish": 30, "neutral": 40, "bearish": 30, "impact": "ไม่ชัดเจน"}
    bullish = round(100 * (bull + 0.5) / (total + 1))
    bearish = round(100 * (bear + 0.5) / (total + 1))
    neutral = 100 - bullish - bearish
    impact = "สูง" if total >= 3 else ("กลาง" if total == 2 else "ต่ำ")
    return {"bullish": bullish, "neutral": neutral, "bearish": bearish, "impact": impact}


LLM_PROMPT = """คุณเป็นนักวิเคราะห์หุ้นสหรัฐฯ ที่มีประสบการณ์ 20 ปี ประเมินข่าวต่อไปนี้สำหรับหุ้น {ticker} ({company})

หัวข้อ: {title}
เนื้อหา: {summary}

ตอบเป็น JSON เท่านั้น (ไม่มีข้อความอื่น) รูปแบบ:
{{"relevant": true, "news_type": "analyst", "catalyst": "Jefferies ลดเรตติ้ง Buy เป็น Hold", "summary_th": "สรุปข่าว + สาเหตุที่ทำให้เกิดข่าว เป็นภาษาไทย 1-2 ประโยค", "relevance_th": "อธิบายว่าข่าวนี้เกี่ยวข้องและกระทบ {company} อย่างไร (เจาะจง เป็นเหตุเป็นผล)", "material": "สูง", "pros": ["ข้อดีที่เจาะจงกับข่าวนี้"], "cons": ["ความเสี่ยงที่เจาะจงกับข่าวนี้"], "bullish": 50, "neutral": 30, "bearish": 20, "impact": "สูง"}}

เกณฑ์ (คิดแบบนักลงทุนมืออาชีพ เข้มงวด):
- catalyst = "ตัวจุดชนวน" ที่ทำให้เป็นข่าว ต้องระบุสาเหตุที่จับต้องได้ (ผลประกอบการ/สัญญา/ดีล/ปรับเรตติ้ง/ฟ้องร้อง/เพิ่มทุน ฯลฯ) — ⚠️ ถ้าข่าวบอกแค่ "หุ้นขึ้น/ลง X%" โดยไม่มีเหตุการณ์ใหม่ที่อธิบายได้ ให้ตอบ catalyst เป็น "ไม่มี"
- relevant: true เฉพาะเมื่อข่าวกระทบ {company} อย่างมีนัยสำคัญ — false ถ้าเป็นข่าวรวมหลายหุ้น (roundup) หรือข่าวภาพรวมอุตสาหกรรมที่ไม่เจาะจง {company}
- relevance_th: สำคัญมากเมื่อข่าวหลักเป็นเรื่องบริษัทอื่นที่เอ่ยถึง {company} — ต้องอธิบายกลไกว่าทำไมถึงกระทบ {company} (เช่น เป็นซัพพลายเออร์/ลูกค้า/คู่แข่ง) ถ้าโยงไม่ได้จริงให้ relevant=false
- news_type: earnings|guidance|contract|analyst|ma|product|insider|index|offering|legal|macro|opinion|roundup|recap|other (recap = รายงานการเคลื่อนไหวราคาเฉยๆ ไม่มีเหตุการณ์ใหม่)
- material: "สูง" = กระทบรายได้/กำไร/แนวโน้มชัดเจน | "กลาง" = ข่าวจริงแต่ผลจำกัด | "ต่ำ" = ความเห็น/สรุปราคา/แค่เอ่ยชื่อ
- bullish+neutral+bearish รวม 100 (ทิศทางราคา 1-7 วัน)"""


def llm_text(prompt: str, max_tokens: int = 600) -> str | None:
    try:
        if ANTHROPIC_API_KEY:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            r.raise_for_status()
            text = r.json()["content"][0]["text"]
        elif OPENAI_API_KEY:
            r = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": "gpt-4o-mini",
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        elif OPENROUTER_API_KEY:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={
                    "model": AI_MODEL,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        else:
            return None
        return text
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return None


def llm_analysis(ticker: str, company: str, title: str, summary: str) -> dict | None:
    text = llm_text(
        LLM_PROMPT.format(
            ticker=ticker, company=company, title=title, summary=summary or title
        )
    )
    if not text:
        return None
    try:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(match.group(0)) if match else None
    except Exception as exc:
        log.warning("LLM JSON parse failed: %s", exc)
        return None


# ---------------------------------------------------------------- formatting


def evaluate_item(ticker: str, company: str, item: dict) -> tuple[bool, dict | None, str]:
    """กรองข่าวแบบเข้มสุด: คืน (ควรส่งไหม, ผลวิเคราะห์, เหตุผล)"""
    title, summary = item["title"], item.get("summary", "")
    if NOISE_RE.search(title):
        return False, None, "opinion/listicle/recap"
    mentions = [ticker.lower(), company.lower()]
    if not any(m in title.lower() for m in mentions):
        return False, None, "company not in headline"
    analysis = llm_analysis(ticker, company, title, summary) if HAS_AI else None
    if analysis:
        rel = analysis.get("relevant")
        if not (rel is True or str(rel).lower() == "true"):
            return False, analysis, "AI: not directly about company"
        ntype = str(analysis.get("news_type", "")).lower()
        if ntype in ("opinion", "roundup", "recap"):
            return False, analysis, "AI: opinion/roundup/recap"
        catalyst = str(analysis.get("catalyst", "")).strip().lower()
        if catalyst in ("", "ไม่มี", "none", "n/a", "na", "-", "ไม่ระบุ"):
            return False, analysis, "AI: no catalyst (price recap)"
        mat = analysis.get("material", "")
        if mat == "สูง":
            pass  # catalyst แรง ส่งได้เสมอ
        elif mat == "กลาง" and ntype in HARD_CATALYSTS:
            pass  # ผลกลางแต่เป็นข่าวประเภท catalyst จริง
        else:
            return False, analysis, f"AI: not material enough ({mat}/{ntype})"
    return True, analysis, "ok"


def build_message(
    ticker: str, item: dict, analysis: dict | None = None, company: str | None = None
) -> str:
    title_th = translate_th(item["title"])
    if analysis is None:
        analysis = llm_analysis(
            ticker, company or ticker, item["title"], item["summary"]
        )
    ai_mode = analysis is not None
    if analysis is None:
        analysis = heuristic_analysis(item["title"], item["summary"])

    q = get_quote(ticker)
    price_line = f"  💵 {fmt_price(q)}" if q else ""
    lines = [
        f"🚨 <b>{html.escape(ticker)}</b> — ข่าวใหม่{price_line}",
        "",
        f"📰 <b>{html.escape(title_th)}</b>",
        f"<i>{html.escape(item['title'])}</i>",
    ]

    catalyst = str(analysis.get("catalyst", "")).strip() if ai_mode else ""
    if catalyst and catalyst.lower() not in ("ไม่มี", "none", "n/a", "na", "-", "ไม่ระบุ"):
        lines += ["", f"⚡ <b>สาเหตุ:</b> {html.escape(catalyst)}"]

    if ai_mode and analysis.get("summary_th"):
        lines += ["", f"📝 {html.escape(analysis['summary_th'])}"]
    elif item["summary"]:
        summary_th = translate_th(item["summary"][:500])
        lines += ["", f"📝 {html.escape(summary_th)}"]

    if ai_mode and analysis.get("relevance_th"):
        lines += [
            "",
            f"🔎 <b>เกี่ยวกับ {html.escape(ticker)} อย่างไร:</b> "
            f"{html.escape(analysis['relevance_th'])}",
        ]

    if ai_mode:
        if analysis.get("pros"):
            lines += [""] + ["✅ " + html.escape(p) for p in analysis["pros"][:3]]
        if analysis.get("cons"):
            lines += [""] + ["⚠️ " + html.escape(c) for c in analysis["cons"][:3]]

    label = "วิเคราะห์ AI" if ai_mode else "วิเคราะห์เบื้องต้น (keyword)"
    lines += [
        "",
        f"📊 <b>{label}</b> (1-7 วัน)",
        f"🟢 Bullish {analysis.get('bullish', '?')}%  "
        f"⚪ Neutral {analysis.get('neutral', '?')}%  "
        f"🔴 Bearish {analysis.get('bearish', '?')}%",
        f"ระดับผลกระทบ: {html.escape(str(analysis.get('impact', '-')))}",
    ]
    if item.get("published"):
        lines += [f"🕐 {html.escape(item['published'])}"]
    if item.get("link"):
        lines += [f"🔗 {html.escape(item['link'])}"]
    return "\n".join(lines)


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ---------------------------------------------------------------- market overview

SET_QUERY = "Thailand SET index OR ตลาดหุ้นไทย when:1d"

# ดัชนี/ราคาที่แสดงใน snapshot — (ชื่อไทย, ticker, แสดงเป็นดอลลาร์ไหม)
SNAPSHOT_TICKERS = [
    ("S&P 500", "^GSPC", False),
    ("Nasdaq", "^IXIC", False),
    ("Dow Jones", "^DJI", False),
    ("ทองคำ (Gold)", "GC=F", True),
    ("น้ำมัน WTI", "CL=F", True),
    ("Bitcoin", "BTC-USD", True),
    ("SET (ไทย)", "^SET.BK", False),
]

MARKET_PROMPT = """คุณเป็นนักวิเคราะห์ตลาดการเงิน สรุปภาพรวมตลาดเป็นภาษาไทย กระชับ อ่านง่าย

ดัชนี/ราคาล่าสุด (ใช้ตัวเลขเหล่านี้เป๊ะๆ ห้ามแก้):
{snapshot}

ข่าวตลาดหุ้นสหรัฐฯ:
{market}

ข่าวทองคำ:
{gold}

ข่าวหุ้นไทย (SET):
{setnews}

ข่าวเศรษฐกิจ/ภูมิรัฐศาสตร์:
{macro}

ตอบตามรูปแบบนี้เท่านั้น (อิงตัวเลขจริงจาก snapshot):
📊 ภาพรวมตลาดหุ้นสหรัฐฯ: (2-3 ประโยค อ้างดัชนีจริง)
🟢 ข่าวดี:
- (bullet สั้นๆ)
🔴 ข่าวร้าย/ความเสี่ยง:
- (bullet สั้นๆ)
⚔️ สงคราม/ภูมิรัฐศาสตร์: (เหตุการณ์สำคัญ + ผลต่อตลาด ถ้าไม่มีบอกว่าไม่มีประเด็นใหม่)
🥇 ทองคำ: (ราคาล่าสุด + ทิศทาง + ปัจจัยขับเคลื่อน)
🇹🇭 หุ้นไทย SET: (ระดับดัชนีล่าสุด + ข่าว/ปัจจัยเด่นวันนี้)
💰 มูลค่าตลาด (valuation): ตลาดหุ้นสหรัฐฯ ตอนนี้ "แพง/เหมาะสม/ถูก" แค่ไหนเทียบค่าเฉลี่ยในอดีต อิงมุมมองนักวิเคราะห์ทั่วไป (เช่น P/E, sentiment) บอกเหตุผลสั้นๆ — เป็นข้อมูลเชิงภาพรวม ไม่ใช่คำแนะนำซื้อขายรายตัว
🔮 มุมมอง 1-7 วัน: หุ้นสหรัฐฯ Bullish/Neutral/Bearish กี่ % + ทองคำ + SET ทิศทาง พร้อมเหตุผลสั้นๆ"""


def _titles(items: list[dict]) -> str:
    return "\n".join(f"- {i['title']}" for i in items) or "- (ไม่มีข่าว)"


def build_snapshot() -> tuple[str, list[str]]:
    """คืน (ข้อความสำหรับ AI, บรรทัดสำหรับแสดงผล) ของดัชนี/ราคาล่าสุด"""
    ai_lines, show_lines = [], []
    for name, tk, as_usd in SNAPSHOT_TICKERS:
        q = get_quote(tk)
        if not q:
            continue
        unit = "$" if as_usd else ""
        num = f"{unit}{q['price']:,.2f} ({q['pct']:+.2f}%)"
        ai_lines.append(f"- {name}: {num}")
        arrow = "🟢" if q["pct"] >= 0 else "🔴"
        show_lines.append(f"{arrow} {name}: {num}")
    return "\n".join(ai_lines) or "- (ไม่พบข้อมูล)", show_lines


def build_market() -> str:
    snap_ai, snap_show = build_snapshot()
    market = fetch_feed(MARKET_QUERY, 10) if MARKET_NEWS else []
    gold = fetch_feed(GOLD_QUERY, 6) if GOLD_NEWS else []
    setnews = fetch_feed(SET_QUERY, 6)
    macro = fetch_feed(MACRO_QUERY, 8)
    now_th = datetime.now(dt_tz.utc).astimezone(THAI_TZ).strftime("%d/%m %H:%M")
    footer = (
        f"\n\n📡 ข้อมูล ณ {now_th} น. (ไทย) | ราคา: Yahoo Finance | "
        "ข่าว: Yahoo + Google News (24 ชม.)\n"
        "ℹ️ เป็นข้อมูลเชิงภาพรวม ไม่ใช่คำแนะนำการลงทุน"
    )
    if HAS_AI and (market or gold or setnews or macro):
        text = llm_text(
            MARKET_PROMPT.format(
                snapshot=snap_ai, market=_titles(market), gold=_titles(gold),
                setnews=_titles(setnews), macro=_titles(macro),
            ),
            max_tokens=1600,
        )
        if text:
            head = "🌐 ภาพรวมตลาด\n" + "\n".join(snap_show)
            return head + "\n\n" + text.strip() + footer
    lines = ["🌐 ภาพรวมตลาด", *snap_show]
    for label, items in (
        ("\n📊 ข่าวตลาดหุ้นสหรัฐฯ", market[:5]),
        ("\n🥇 ทองคำ", gold[:3]),
        ("\n🇹🇭 หุ้นไทย SET", setnews[:4]),
        ("\n🌍 เศรษฐกิจ/ภูมิรัฐศาสตร์", macro[:4]),
    ):
        if items:
            lines.append(label)
            lines += ["• " + translate_th(i["title"]) for i in items]
    return "\n".join(lines) + footer


# ---------------------------------------------------------------- daily movers


def get_quote(ticker: str) -> dict | None:
    """ราคาปัจจุบัน + %วันนี้ (จากราคาปิดจริง) + แนวรับ/แนวต้าน (30 วัน)"""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"range": "3mo", "interval": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        res = r.json()["chart"]["result"][0]
        meta = res["meta"]
        q0 = res["indicators"]["quote"][0]
        closes = [c for c in q0["close"] if c is not None]
        if len(closes) < 2:
            return None
        price = meta.get("regularMarketPrice") or closes[-1]
        prev = closes[-2]
        if not (price and prev):
            return None
        highs = [h for h in q0.get("high", []) if h is not None][-30:]
        lows = [l for l in q0.get("low", []) if l is not None][-30:]
        return {
            "price": price,
            "prev": prev,
            "pct": (price - prev) / prev * 100,
            "currency": meta.get("currency", "USD"),
            "resistance": max(highs) if highs else None,
            "support": min(lows) if lows else None,
        }
    except Exception as exc:
        log.warning("quote %s failed: %s", ticker, exc)
    return None


def fmt_price(q: dict | None) -> str:
    if not q:
        return "ราคาไม่พบ"
    sym = "$" if q.get("currency", "USD") == "USD" else ""
    return f"{sym}{q['price']:.2f} ({q['pct']:+.2f}%)"


_yahoo_session = None
_yahoo_crumb = None

REC_TH = {
    "strong_buy": "ซื้อเด่นชัด", "buy": "ซื้อ", "hold": "ถือ",
    "underperform": "ต่ำกว่าตลาด", "sell": "ขาย", "none": "ไม่มีคำแนะนำ",
}


def _yahoo_auth() -> tuple:
    global _yahoo_session, _yahoo_crumb
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    try:
        s.get("https://fc.yahoo.com", timeout=10)
    except Exception:
        pass
    _yahoo_crumb = s.get(
        "https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=10
    ).text.strip()
    _yahoo_session = s
    return s, _yahoo_crumb


def get_fair_value(ticker: str) -> dict | None:
    """เป้าหมายนักวิเคราะห์ + P/E + วันประกาศผล + 52w จาก Yahoo (โมดูลเดียวจบ)"""
    global _yahoo_session, _yahoo_crumb
    for _ in range(2):
        try:
            if not _yahoo_session or not _yahoo_crumb:
                _yahoo_auth()
            r = _yahoo_session.get(
                f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                params={
                    "modules": "financialData,summaryDetail,calendarEvents",
                    "crumb": _yahoo_crumb,
                },
                timeout=10,
            )
            if r.status_code in (401, 403):
                _yahoo_session = _yahoo_crumb = None
                continue
            res = r.json()["quoteSummary"]["result"][0]
            fd = res.get("financialData", {}) or {}
            sd = res.get("summaryDetail", {}) or {}
            ce = res.get("calendarEvents", {}) or {}

            def rw(o, k):
                v = o.get(k)
                return v.get("raw") if isinstance(v, dict) else v

            mean = rw(fd, "targetMeanPrice")
            cur = rw(fd, "currentPrice")
            upside = ((mean - cur) / cur * 100) if (mean and cur) else None
            # P/E: ใช้ trailing ถ้าเป็นบวก, ไม่งั้น forward ถ้าสมเหตุผล
            tpe, fpe = rw(sd, "trailingPE"), rw(sd, "forwardPE")
            pe = tpe if (tpe and tpe > 0) else None
            fwd_pe = fpe if (fpe and 0 < fpe < 200) else None
            ed = (ce.get("earnings", {}) or {}).get("earningsDate", [])
            earn = None
            if ed:
                e0 = ed[0]
                earn = e0.get("fmt") if isinstance(e0, dict) else (
                    datetime.fromtimestamp(e0, dt_tz.utc).strftime("%Y-%m-%d")
                    if isinstance(e0, (int, float)) else None
                )
            if not (mean or pe or fwd_pe or earn):
                return None
            return {
                "current": cur, "low": rw(fd, "targetLowPrice"), "mean": mean,
                "high": rw(fd, "targetHighPrice"), "n": rw(fd, "numberOfAnalystOpinions"),
                "rec": (rw(fd, "recommendationKey") or "none"), "upside": upside,
                "pe": pe, "fwd_pe": fwd_pe, "earnings": earn,
                "wk_hi": rw(sd, "fiftyTwoWeekHigh"), "wk_lo": rw(sd, "fiftyTwoWeekLow"),
            }
        except Exception as exc:
            _yahoo_session = _yahoo_crumb = None
            log.warning("fair value %s failed: %s", ticker, exc)
    return None


def fmt_fair_value(fv: dict | None) -> str:
    if not fv or not fv.get("mean"):
        return "ราคาเป้าหมาย: ไม่มีข้อมูลนักวิเคราะห์"
    n = fv.get("n") or "?"
    rng = ""
    if fv.get("low") and fv.get("high"):
        rng = f"${fv['low']:.2f}–${fv['high']:.2f} "
    rec = REC_TH.get(str(fv.get("rec")), str(fv.get("rec")))
    up = fv.get("upside")
    if up is None:
        verdict = ""
    elif up >= 25:
        verdict = f"ราคาต่ำกว่าเป้าหมายมาก (upside +{up:.0f}%)"
    elif up >= 5:
        verdict = f"ราคาต่ำกว่าเป้าหมายเฉลี่ย (upside +{up:.0f}%)"
    elif up >= -5:
        verdict = f"ราคาใกล้เคียงมูลค่าเหมาะสม ({up:+.0f}%)"
    elif up >= -25:
        verdict = f"ราคาสูงกว่าเป้าหมายเฉลี่ย ({up:+.0f}%)"
    else:
        verdict = f"ราคาสูงกว่าเป้าหมายมาก ({up:+.0f}%)"
    return (
        f"มูลค่าเหมาะสม (นักวิเคราะห์ {n} ราย): {rng}เฉลี่ย ${fv['mean']:.2f} | "
        f"คำแนะนำ: {rec} | {verdict}"
    )


def _fmt_date_th(iso: str | None) -> str:
    """'2026-08-06' -> '06/08/2026' (+ระบุถ้าใกล้ภายใน 7 วัน)"""
    if not iso:
        return ""
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d").date()
        days = (d - datetime.now(THAI_TZ).date()).days
        s = d.strftime("%d/%m/%Y")
        if 0 <= days <= 7:
            return f"{s} ⚠️อีก {days} วัน"
        return s
    except Exception:
        return iso


def fmt_technical(q: dict | None, fv: dict | None) -> str:
    parts = []
    if fv and fv.get("pe"):
        parts.append(f"P/E {fv['pe']:.1f}")
    elif fv and fv.get("fwd_pe"):
        parts.append(f"P/E ล่วงหน้า {fv['fwd_pe']:.1f}")
    else:
        parts.append("ยังไม่มีกำไร (ไม่มี P/E)")
    if q and q.get("support") and q.get("resistance"):
        sym = "$" if q.get("currency", "USD") == "USD" else ""
        parts.append(
            f"แนวรับ {sym}{q['support']:.2f} / แนวต้าน {sym}{q['resistance']:.2f}"
        )
    if fv and fv.get("earnings"):
        parts.append(f"ประกาศผล {_fmt_date_th(fv['earnings'])}")
    return " | ".join(parts)


def collect_ticker_news(ticker: str, company: str, limit: int = 6) -> list[str]:
    """รวมพาดหัวข่าววันนี้จากหลายแหล่ง (Yahoo + Google) แล้วตัดข่าวซ้ำ"""
    raw: list[dict] = []
    try:
        raw += fetch_news(ticker, 6)
    except Exception as exc:
        log.warning("movers yahoo %s failed: %s", ticker, exc)
    try:
        raw += fetch_feed(f'"{company}" {ticker} stock when:1d', 8)
    except Exception as exc:
        log.warning("movers google %s failed: %s", ticker, exc)
    titles: list[str] = []
    seen_tokens: list[str] = []
    mentions = (ticker.lower(), company.lower())
    for it in raw:
        title = it["title"]
        if NOISE_RE.search(title):
            continue
        if not any(m in title.lower() for m in mentions):
            continue
        if is_duplicate_title(title, seen_tokens):
            continue
        seen_tokens.append(" ".join(title_tokens(title)))
        titles.append(re.split(r"\s+-\s+[^-]+$", title)[0])  # ตัดชื่อสำนักข่าวท้าย
        if len(titles) >= limit:
            break
    return titles


MOVERS_PROMPT = """คุณเป็นนักวิเคราะห์หุ้นสหรัฐฯ มืออาชีพ ด้านล่างคือข้อมูลหุ้นทุกตัวในวอทช์ลิสต์วันนี้ (ราคา, มูลค่าเหมาะสมจากนักวิเคราะห์, พาดหัวข่าว) และข่าวทองคำ

{blocks}

งาน: เขียนสรุปภาษาไทยให้นักลงทุน ครอบคลุม "ทุกตัว" ที่ให้มา
กฎสำคัญ:
- ⚠️ ตัวเลขทุกตัว (ราคา, %, มูลค่าเหมาะสม, คำแนะนำ) ให้คัดลอกตามที่ให้มาเป๊ะๆ ห้ามแก้/ห้ามคิดเอง ทิศทางเหตุผลต้องสอดคล้องกับ +/-%
- ต้องแสดงครบทุกหุ้น — หุ้นที่ไม่มีข่าวให้เขียนว่า "ยังไม่มีข่าววันนี้ (เคลื่อนตามตลาด)" แต่ยังต้องแสดงราคาและมูลค่าเหมาะสม
- ถ้าหุ้นหลายตัวลง/ขึ้นด้วยเหตุผลเดียวกัน (เช่น แรงขายทั้งกลุ่มอวกาศ, ข่าว Fed) ให้รวมเหตุผลเป็นกลุ่มเดียว แต่ยังคงแสดงราคา+มูลค่าเหมาะสมรายตัว
- ถ้ามีข่าว "ปรับเรตติ้ง" ต้องระบุให้ชัดว่าปรับจากอะไรเป็นอะไร และราคาเป้าหมายใหม่ถ้ามีในข่าว (เช่น "Jefferies ลดจาก Buy เหลือ Hold เป้า $18")
- กระชับ ตรงประเด็น แบบนักลงทุนคุยกัน

รูปแบบ (แต่ละหุ้น 3 บรรทัด คัดลอกตัวเลขที่ให้มาเป๊ะๆ):
📅 สรุปหุ้นวันนี้ + มูลค่าเหมาะสม

• <SYM> $ราคา (+/-%) — เหตุผลที่ขึ้น/ลง (หรือ "ยังไม่มีข่าววันนี้")
  📈 <บรรทัดมูลค่าเหมาะสม>
  📐 <บรรทัดข้อมูลพื้นฐาน/เทคนิค (P/E, แนวรับ/แนวต้าน, ประกาศผล)>

🥇 ทองคำ: $ราคา (+/-%) — ทิศทาง + ปัจจัยขับเคลื่อนสั้นๆ"""


def build_movers() -> str:
    data = load_data()
    if not data["watchlist"]:
        return "ยังไม่มีหุ้นใน watchlist ครับ ลอง /add ก่อน"
    blocks = []
    fallback_lines = []
    for tk in data["watchlist"]:
        q = get_quote(tk)
        fv = get_fair_value(tk)
        company = get_company_name(tk)
        news = collect_ticker_news(tk, company)
        price = fmt_price(q)
        fair = fmt_fair_value(fv)
        tech = fmt_technical(q, fv)
        heads = "\n".join("- " + t for t in news) or "- (ไม่มีข่าวเฉพาะตัววันนี้)"
        blocks.append(
            f"[{tk}] {company}: {price}\n{fair}\nข้อมูลพื้นฐาน/เทคนิค: {tech}\n"
            f"ข่าววันนี้:\n{heads}"
        )
        fallback_lines.append(
            f"• {tk} {price}" + (f" — {news[0]}" if news else " — ยังไม่มีข่าววันนี้")
            + f"\n  📈 {fair}\n  📐 {tech}"
        )
    # ทองคำ
    gq = get_quote("GC=F")
    gold_news = fetch_feed(GOLD_QUERY, 4)
    gold_heads = "\n".join("- " + i["title"] for i in gold_news) or "- (ไม่มีข่าว)"
    gold_price = fmt_price(gq) if gq else "ราคาไม่พบ"
    blocks.append(f"[ทองคำ GOLD] ราคา: {gold_price}\nข่าววันนี้:\n{gold_heads}")

    now_th = datetime.now(dt_tz.utc).astimezone(THAI_TZ).strftime("%d/%m %H:%M")
    footer = (
        f"\n\n📡 ข้อมูล ณ {now_th} น. (ไทย)\n"
        "ราคา & เป้านักวิเคราะห์: Yahoo Finance | ข่าว: Yahoo + Google News (24 ชม.ล่าสุด)\n"
        "ℹ️ มูลค่าเหมาะสม = ค่าเฉลี่ยเป้าหมายนักวิเคราะห์ ไม่ใช่คำแนะนำลงทุน"
    )
    if HAS_AI:
        text = llm_text(
            MOVERS_PROMPT.format(blocks="\n\n".join(blocks)), max_tokens=1800
        )
        if text:
            return text.strip() + footer
    gold_line = f"\n\n🥇 ทองคำ: {gold_price}"
    return (
        "📅 สรุปหุ้นวันนี้ + มูลค่าเหมาะสม\n\n"
        + "\n".join(fallback_lines)
        + gold_line
        + footer
    )


def build_earnings() -> str:
    data = load_data()
    if not data["watchlist"]:
        return "ยังไม่มีหุ้นใน watchlist ครับ"
    rows = []
    for tk in data["watchlist"]:
        fv = get_fair_value(tk)
        earn = fv.get("earnings") if fv else None
        rows.append((earn or "9999-99-99", tk, earn))
    rows.sort()
    lines = ["📆 ปฏิทินประกาศผลประกอบการ (watchlist)\n"]
    for _, tk, earn in rows:
        lines.append(f"• {tk}: {_fmt_date_th(earn) if earn else 'ยังไม่มีกำหนด'}")
    lines.append("\n⚠️ = ประกาศภายใน 7 วัน (ระวังความผันผวน)")
    return "\n".join(lines)


async def check_alerts(context: ContextTypes.DEFAULT_TYPE, data: dict) -> bool:
    """เช็คการแจ้งเตือนราคา คืน True ถ้ามีการเปลี่ยนแปลง (ต้อง save)"""
    if not data["alerts"]:
        return False
    tickers = {a["ticker"] for a in data["alerts"]}
    prices = {}
    for tk in tickers:
        q = await asyncio.to_thread(get_quote, tk)
        if q:
            prices[tk] = q["price"]
    remaining, fired = [], []
    for a in data["alerts"]:
        p = prices.get(a["ticker"])
        if p is None:
            remaining.append(a)
            continue
        hit = (a["side"] == "above" and p >= a["target"]) or (
            a["side"] == "below" and p <= a["target"]
        )
        if hit:
            fired.append((a, p))
        else:
            remaining.append(a)
    for a, p in fired:
        arrow = "🔼" if a["side"] == "above" else "🔽"
        msg = (
            f"🔔 <b>แจ้งเตือนราคา {html.escape(a['ticker'])}</b>\n"
            f"{arrow} ราคาแตะ ${p:.2f} (เป้าที่ตั้ง ${a['target']:.2f})"
        )
        try:
            await context.bot.send_message(
                chat_id=a["chat"], text=msg, parse_mode=ParseMode.HTML
            )
        except Exception as exc:
            log.warning("alert send failed: %s", exc)
    if fired:
        data["alerts"] = remaining
        return True
    return False


HIGH_IMPACT_WORDS = [
    "war", "invasion", "invades", "airstrike", "airstrikes", "missile", "nuclear",
    "rate cut", "rate hike", "rate decision", "fed cuts", "fed hikes", "fed chair",
    "emergency", "cpi", "inflation report", "jobs report", "nonfarm", "payrolls",
    "market crash", "default", "government shutdown", "tariff", "tariffs",
    "sanctions", "recession", "sell-off", "selloff", "bank failure", "ceasefire",
    "stocks plunge", "stocks tumble", "wall street plunges", "circuit breaker",
]

# คำที่ดูเหมือน macro แต่จริงๆ เป็นบทวิเคราะห์ค่าเงิน/โบรกเกอร์ ไม่ใช่ข่าวใหญ่
MACRO_NOISE_RE = re.compile(
    r"(usd/|/usd|eur/|gbp/|/jpy|/cad|/chf|\bfx\b|forex|vt markets|fxstreet"
    r"|forecast|technical (analysis|outlook)|price (prediction|target)|elliott wave"
    r"|what (to know|you need to know)|\d+\s+(reasons?|stocks?|things)\b)",
    re.I,
)

HIGH_IMPACT_RE = re.compile(
    r"\b(?:"
    + "|".join(re.escape(w).replace(r"\ ", r"\s+") for w in HIGH_IMPACT_WORDS)
    + r")\b",
    re.I,
)


def is_high_impact(title: str) -> bool:
    return bool(HIGH_IMPACT_RE.search(title)) and not MACRO_NOISE_RE.search(title)


def build_macro_alert(item: dict) -> str:
    title_th = translate_th(item["title"])
    lines = ["🚨 Macro Alert — ข่าวใหญ่กระทบตลาด", "", f"📰 {title_th}", f"({item['title']})"]
    if HAS_AI:
        impact = llm_text(
            f"ข่าว: {item['title']}\n"
            "วิเคราะห์สั้นๆ 2-3 ประโยคเป็นภาษาไทย: ข่าวนี้มีผลต่อตลาดหุ้นสหรัฐฯ "
            "และทองคำอย่างไร (บอกทิศทาง Bullish/Bearish ด้วย)",
            max_tokens=350,
        )
        if impact:
            lines += ["", "💡 " + impact.strip()]
    if item.get("link"):
        lines += ["", "🔗 " + item["link"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- commands


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat_id = update.effective_chat.id
    if chat_id not in data["chats"]:
        data["chats"].append(chat_id)
        save_data(data)
    ai = "✅ เปิดใช้งาน" if HAS_AI else "❌ ยังไม่มี API key (ใช้ keyword วิเคราะห์แทน)"
    await update.message.reply_text(
        "👋 สวัสดีครับ! ผมคือ bot ข่าวหุ้นสหรัฐฯ แปลไทย\n\n"
        f"📈 Watchlist: {', '.join(data['watchlist'])}\n"
        f"🤖 AI วิเคราะห์: {ai}\n"
        f"⏱ เช็คข่าวใหม่ทุก {CHECK_INTERVAL // 60} นาที\n\n"
        "📬 ของที่จะได้รับอัตโนมัติ:\n"
        "• ข่าวหุ้นใน watchlist (เช็คทุก 5 นาที)\n"
        "• สรุปหุ้นรายวัน ทำไมขึ้น/ลง + มูลค่าเหมาะสม ทุกเช้า 05:00 น.\n"
        "• ภาพรวมตลาด (สหรัฐฯ+ทองคำ+SET) วันละ 2 รอบ (07:00 และ 20:00 น.)\n"
        "• Macro Alert ข่าวใหญ่ (สงคราม/Fed/เงินเฟ้อ) เด้งทันที\n\n"
        "คำสั่ง:\n"
        "/movers — สรุปหุ้นวันนี้ + มูลค่าเหมาะสม + P/E + แนวรับแนวต้าน\n"
        "/market — ภาพรวมตลาด สหรัฐฯ+ทองคำ+SET+คริปโต/น้ำมัน\n"
        "/earnings — ปฏิทินวันประกาศผลประกอบการ\n"
        "/alert NVDA 200 — ตั้งเตือนเมื่อราคาถึง (ดู: /alert, ล้าง: /alert clear)\n"
        "/news NVDA — ข่าวล่าสุดรายหุ้น\n"
        "/watchlist /add /remove — จัดการหุ้น\n"
        "/stop — หยุดรับข่าว"
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat_id = update.effective_chat.id
    if chat_id in data["chats"]:
        data["chats"].remove(chat_id)
        save_data(data)
    await update.message.reply_text("🛑 หยุดส่งข่าวแล้วครับ พิมพ์ /start เพื่อรับข่าวอีกครั้ง")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    await update.message.reply_text(
        "📈 Watchlist:\n" + "\n".join(f"• {t}" for t in data["watchlist"])
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("ใช้แบบนี้ครับ: /add TSLA")
        return
    ticker = context.args[0].upper().strip()
    if not re.fullmatch(r"[A-Z.\-]{1,6}", ticker):
        await update.message.reply_text(f"❌ '{ticker}' ไม่ใช่สัญลักษณ์หุ้นที่ถูกต้อง")
        return
    data = load_data()
    if ticker in data["watchlist"]:
        await update.message.reply_text(f"ℹ️ {ticker} อยู่ใน watchlist อยู่แล้ว")
        return
    data["watchlist"].append(ticker)
    save_data(data)
    await update.message.reply_text(f"✅ เพิ่ม {ticker} แล้ว ({len(data['watchlist'])} ตัว)")


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("ใช้แบบนี้ครับ: /remove TSLA")
        return
    ticker = context.args[0].upper().strip()
    data = load_data()
    if ticker not in data["watchlist"]:
        await update.message.reply_text(f"ℹ️ ไม่มี {ticker} ใน watchlist")
        return
    data["watchlist"].remove(ticker)
    data["seen"].pop(ticker, None)
    save_data(data)
    await update.message.reply_text(f"🗑 ลบ {ticker} แล้ว")


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    ticker = (context.args[0].upper().strip() if context.args else data["watchlist"][0])
    await update.message.reply_text(f"🔎 กำลังดึงข่าว {ticker} ...")
    items = await asyncio.to_thread(fetch_news, ticker, 3)
    if not items:
        await update.message.reply_text(f"ไม่พบข่าวของ {ticker} ในตอนนี้ครับ")
        return
    company = await asyncio.to_thread(get_company_name, ticker)
    for item in items:
        msg = await asyncio.to_thread(build_message, ticker, item, None, company)
        await update.message.reply_text(
            msg, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW
        )


async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔎 กำลังรวบรวมภาพรวมตลาด (หุ้นสหรัฐฯ + ทองคำ + SET ไทย) รอสักครู่ (~30 วินาที)..."
    )
    text = await asyncio.to_thread(build_market)
    await update.message.reply_text(text[:3900], link_preview_options=NO_PREVIEW)


async def cmd_movers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🔎 กำลังรวบรวมราคา+เป้านักวิเคราะห์+ข่าวหลายแหล่งของทุกตัว (~60-90 วินาที)..."
    )
    text = await asyncio.to_thread(build_movers)
    await update.message.reply_text(text[:3900], link_preview_options=NO_PREVIEW)


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat_id = update.effective_chat.id
    args = context.args or []
    if args and args[0].lower() in ("clear", "ล้าง"):
        data["alerts"] = [a for a in data["alerts"] if a["chat"] != chat_id]
        save_data(data)
        await update.message.reply_text("🗑 ล้างการแจ้งเตือนราคาทั้งหมดแล้ว")
        return
    if len(args) >= 2:
        ticker = args[0].upper().strip()
        try:
            target = float(args[1].replace("$", "").replace(",", ""))
        except ValueError:
            await update.message.reply_text("ใส่ราคาเป็นตัวเลขครับ เช่น /alert NVDA 200")
            return
        q = await asyncio.to_thread(get_quote, ticker)
        if not q:
            await update.message.reply_text(f"❌ หาราคา {ticker} ไม่เจอ")
            return
        side = "above" if target >= q["price"] else "below"
        data["alerts"].append(
            {"chat": chat_id, "ticker": ticker, "target": target, "side": side}
        )
        save_data(data)
        cond = "ขึ้นถึง" if side == "above" else "ลงถึง"
        await update.message.reply_text(
            f"🔔 ตั้งแจ้งเตือน {ticker} เมื่อราคา{cond} ${target:.2f}\n"
            f"(ตอนนี้ ${q['price']:.2f}) — เด้งทันทีเมื่อถึง"
        )
        return
    # ไม่มี args = แสดงรายการ
    mine = [a for a in data["alerts"] if a["chat"] == chat_id]
    if not mine:
        await update.message.reply_text(
            "ยังไม่มีการแจ้งเตือนราคา\nตั้งได้แบบนี้: /alert NVDA 200\nล้างทั้งหมด: /alert clear"
        )
        return
    lines = ["🔔 การแจ้งเตือนราคาของคุณ:"]
    for a in mine:
        cond = "≥" if a["side"] == "above" else "≤"
        lines.append(f"• {a['ticker']} {cond} ${a['target']:.2f}")
    lines.append("\nล้างทั้งหมด: /alert clear")
    await update.message.reply_text("\n".join(lines))


async def cmd_earnings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("🔎 กำลังเช็ควันประกาศผลประกอบการ...")
    text = await asyncio.to_thread(build_earnings)
    await update.message.reply_text(text[:3900], link_preview_options=NO_PREVIEW)


# ---------------------------------------------------------------- background


async def check_news_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    if not data["chats"]:
        return
    changed = False
    for ticker in list(data["watchlist"]):
        try:
            items = await asyncio.to_thread(fetch_news, ticker, 5)
        except Exception as exc:
            log.warning("fetch %s failed: %s", ticker, exc)
            continue
        if not items:
            continue
        first_run = ticker not in data["seen"]
        seen = set(data["seen"].get(ticker, []))
        new_items = [i for i in items if i["id"] not in seen]
        data["seen"][ticker] = ([i["id"] for i in items] + list(seen))[:100]
        changed = True
        if first_run:
            continue  # บันทึก baseline ก่อน ไม่ spam ข่าวเก่าตอนเริ่ม
        company = await asyncio.to_thread(get_company_name, ticker)
        for item in new_items:
            if is_duplicate_title(item["title"], data["recent_titles"]):
                log.info("skip dup [%s]: %s", ticker, item["title"][:70])
                continue
            ok, analysis, reason = await asyncio.to_thread(
                evaluate_item, ticker, company, item
            )
            if not ok:
                log.info("skip [%s] (%s): %s", ticker, reason, item["title"][:70])
                continue
            msg = await asyncio.to_thread(build_message, ticker, item, analysis, company)
            data["recent_titles"] = (
                data["recent_titles"] + [" ".join(title_tokens(item["title"]))]
            )[-300:]
            for chat_id in data["chats"]:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode=ParseMode.HTML,
                        link_preview_options=NO_PREVIEW,
                    )
                except Exception as exc:
                    log.warning("send to %s failed: %s", chat_id, exc)
    try:
        if await check_alerts(context, data):
            changed = True
    except Exception as exc:
        log.warning("alert check failed: %s", exc)

    if MACRO_ALERTS:
        try:
            items = await asyncio.to_thread(fetch_feed, MACRO_QUERY, 12)
            if items:
                first_run = "__macro__" not in data["seen"]
                seen = set(data["seen"].get("__macro__", []))
                data["seen"]["__macro__"] = (
                    [i["id"] for i in items] + list(seen)
                )[:200]
                changed = True
                if not first_run:
                    for item in items:
                        if item["id"] in seen or not is_high_impact(item["title"]):
                            continue
                        if is_duplicate_title(item["title"], data["recent_titles"]):
                            log.info("skip dup macro: %s", item["title"][:70])
                            continue
                        msg = await asyncio.to_thread(build_macro_alert, item)
                        data["recent_titles"] = (
                            data["recent_titles"]
                            + [" ".join(title_tokens(item["title"]))]
                        )[-300:]
                        for chat_id in data["chats"]:
                            try:
                                await context.bot.send_message(
                                    chat_id=chat_id,
                                    text=msg[:3900],
                                    link_preview_options=NO_PREVIEW,
                                )
                            except Exception as exc:
                                log.warning("macro send to %s failed: %s", chat_id, exc)
        except Exception as exc:
            log.warning("macro check failed: %s", exc)

    if changed:
        save_data(data)


async def digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    if not data["chats"]:
        return
    text = await asyncio.to_thread(build_market)
    for chat_id in data["chats"]:
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=text[:3900], link_preview_options=NO_PREVIEW
            )
        except Exception as exc:
            log.warning("digest send to %s failed: %s", chat_id, exc)


async def movers_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    if not data["chats"]:
        return
    text = await asyncio.to_thread(build_movers)
    for chat_id in data["chats"]:
        try:
            await context.bot.send_message(
                chat_id=chat_id, text=text[:3900], link_preview_options=NO_PREVIEW
            )
        except Exception as exc:
            log.warning("movers send to %s failed: %s", chat_id, exc)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error: %s", context.error)


# ---------------------------------------------------------------- main


BOT_VERSION = "1.9-pe-alerts-earnings"


async def post_init(app: Application) -> None:
    try:
        await app.bot.set_my_short_description(
            f"ข่าวหุ้นสหรัฐฯ แปลไทย + AI วิเคราะห์ | v{BOT_VERSION}"
        )
    except Exception as exc:
        log.warning("set description failed: %s", exc)
    await app.bot.set_my_commands(
        [
            BotCommand("start", "เริ่มรับข่าว + ดูวิธีใช้"),
            BotCommand("movers", "สรุปหุ้นวันนี้ + มูลค่าเหมาะสม + P/E"),
            BotCommand("market", "ภาพรวมตลาด สหรัฐฯ+ทองคำ+SET+คริปโต"),
            BotCommand("earnings", "ปฏิทินวันประกาศผลประกอบการ"),
            BotCommand("alert", "ตั้งเตือนราคา เช่น /alert NVDA 200"),
            BotCommand("news", "ดูข่าวล่าสุด เช่น /news NVDA"),
            BotCommand("watchlist", "ดูรายชื่อหุ้นที่ติดตาม"),
            BotCommand("add", "เพิ่มหุ้น เช่น /add TSLA"),
            BotCommand("remove", "ลบหุ้น เช่น /remove TSLA"),
            BotCommand("stop", "หยุดรับข่าว"),
        ]
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("movers", cmd_movers))
    app.add_handler(CommandHandler("alert", cmd_alert))
    app.add_handler(CommandHandler("earnings", cmd_earnings))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(check_news_job, interval=CHECK_INTERVAL, first=15)
    if MARKET_NEWS or GOLD_NEWS:
        # 00:00 UTC = 07:00 น. ไทย (หลังตลาดสหรัฐฯ ปิด), 13:00 UTC = 20:00 น. ไทย (ก่อนเปิด)
        app.job_queue.run_daily(digest_job, time=dtime(hour=0, minute=0, tzinfo=dt_tz.utc))
        app.job_queue.run_daily(digest_job, time=dtime(hour=13, minute=0, tzinfo=dt_tz.utc))
    # สรุปหุ้นรายวัน "ทำไมขึ้น/ลง" — 22:00 UTC = 05:00 น. ไทย (หลังตลาดสหรัฐฯ ปิดสนิททั้งฤดูร้อน/หนาว)
    app.job_queue.run_daily(movers_job, time=dtime(hour=22, minute=0, tzinfo=dt_tz.utc))
    log.info("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
