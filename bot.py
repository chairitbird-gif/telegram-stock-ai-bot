import os
import requests
import finnhub
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

WATCHLIST = ["RKLB", "EOSE", "ASTS", "RDW", "NVDA"]

finnhub_client = finnhub.Client(api_key=FINNHUB_API_KEY)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📈 US Stock AI Bot พร้อมใช้งาน\n\n"
        "คำสั่ง:\n"
        "/news SYMBOL เช่น /news NVDA\n"
        "/watchlist ดูหุ้นที่ติดตาม\n"
    )
    await update.message.reply_text(text)


async def watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📌 Watchlist:\n" + "\n".join(WATCHLIST)
    )


async def news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "ใช้แบบนี้: /news NVDA"
        )
        return

    symbol = context.args[0].upper()

    try:
        news_data = finnhub_client.company_news(
            symbol,
            _from="2026-06-01",
            to="2026-06-11"
        )

        if not news_data:
            await update.message.reply_text(
                f"ไม่พบข่าวของ {symbol}"
            )
            return

        top_news = news_data[:3]

        msg = f"📰 ข่าวล่าสุด {symbol}\n\n"

        for item in top_news:
            headline = item.get("headline", "ไม่มีหัวข้อ")
            source = item.get("source", "")
            url = item.get("url", "")

            msg += (
                f"• {headline}\n"
                f"แหล่งข่าว: {source}\n"
                f"{url}\n\n"
            )

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(
            f"เกิดข้อผิดพลาด: {str(e)}"
        )


def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("watchlist", watchlist))
    app.add_handler(CommandHandler("news", news))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
