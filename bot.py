# -*- coding: utf-8 -*-
"""US Stock Thai News Bot — ข่าวหุ้นสหรัฐฯ แปลไทย + วิเคราะห์ ส่งเข้า Telegram"""
import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path

import feedparser
import requests
from deep_translator import GoogleTranslator
from dotenv import load_dotenv
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # วินาที

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data.json"
DEFAULT_WATCHLIST = ["RKLB", "EOSE", "ASTS", "RDW", "NVDA"]

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


LLM_PROMPT = """คุณเป็นนักวิเคราะห์หุ้นสหรัฐฯ วิเคราะห์ข่าวต่อไปนี้ของหุ้น {ticker}

หัวข้อ: {title}
เนื้อหา: {summary}

ตอบเป็น JSON เท่านั้น (ไม่มีข้อความอื่น) รูปแบบ:
{{"summary_th": "สรุปข่าวภาษาไทย 1-2 ประโยค", "pros": ["ข้อดี 1", "ข้อดี 2"], "cons": ["ความเสี่ยง 1", "ความเสี่ยง 2"], "bullish": 50, "neutral": 30, "bearish": 20, "impact": "สูง/กลาง/ต่ำ"}}

bullish+neutral+bearish ต้องรวมเป็น 100 (ความน่าจะเป็นของทิศทางราคาใน 1-7 วัน)"""


def llm_analysis(ticker: str, title: str, summary: str) -> dict | None:
    prompt = LLM_PROMPT.format(ticker=ticker, title=title, summary=summary or title)
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
                    "max_tokens": 600,
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
                    "max_tokens": 600,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        else:
            return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(match.group(0)) if match else None
    except Exception as exc:
        log.warning("LLM analysis failed: %s", exc)
        return None


# ---------------------------------------------------------------- formatting


def build_message(ticker: str, item: dict) -> str:
    title_th = translate_th(item["title"])
    analysis = llm_analysis(ticker, item["title"], item["summary"])
    ai_mode = analysis is not None
    if analysis is None:
        analysis = heuristic_analysis(item["title"], item["summary"])

    lines = [
        f"🚨 <b>{html.escape(ticker)}</b> — ข่าวใหม่",
        "",
        f"📰 <b>{html.escape(title_th)}</b>",
        f"<i>{html.escape(item['title'])}</i>",
    ]

    if ai_mode and analysis.get("summary_th"):
        lines += ["", f"📝 {html.escape(analysis['summary_th'])}"]
    elif item["summary"]:
        summary_th = translate_th(item["summary"][:500])
        lines += ["", f"📝 {html.escape(summary_th)}"]

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

# ---------------------------------------------------------------- commands


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    chat_id = update.effective_chat.id
    if chat_id not in data["chats"]:
        data["chats"].append(chat_id)
        save_data(data)
    ai = "✅ เปิดใช้งาน" if (ANTHROPIC_API_KEY or OPENAI_API_KEY) else "❌ ยังไม่มี API key (ใช้ keyword วิเคราะห์แทน)"
    await update.message.reply_text(
        "👋 สวัสดีครับ! ผมคือ bot ข่าวหุ้นสหรัฐฯ แปลไทย\n\n"
        f"📈 Watchlist: {', '.join(data['watchlist'])}\n"
        f"🤖 AI วิเคราะห์: {ai}\n"
        f"⏱ เช็คข่าวใหม่ทุก {CHECK_INTERVAL // 60} นาที\n\n"
        "คำสั่ง:\n"
        "/watchlist — ดูรายชื่อหุ้น\n"
        "/add TSLA — เพิ่มหุ้น\n"
        "/remove TSLA — ลบหุ้น\n"
        "/news NVDA — ดูข่าวล่าสุดตอนนี้\n"
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
    for item in items:
        msg = await asyncio.to_thread(build_message, ticker, item)
        await update.message.reply_text(
            msg, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW
        )


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
        for item in new_items[:3]:
            msg = await asyncio.to_thread(build_message, ticker, item)
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
    if changed:
        save_data(data)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error: %s", context.error)


# ---------------------------------------------------------------- main


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(check_news_job, interval=CHECK_INTERVAL, first=15)
    log.info("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
