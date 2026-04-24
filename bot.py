import asyncio
import os
import logging
import re
import feedparser
import telegram
import yt_dlp
import tempfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"❌ Missing env var: {key}")
    return val

TELEGRAM_BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = require_env("TELEGRAM_CHAT_ID")
TWITTER_USERNAMES  = [u.strip().lstrip("@") for u in require_env("TWITTER_USERNAMES").split(",")]
POLL_INTERVAL      = max(15, int(os.environ.get("POLL_INTERVAL_SECONDS", "30")))
INCLUDE_RETWEETS   = os.environ.get("INCLUDE_RETWEETS", "false").lower() == "true"
CUSTOM_PREFIX      = os.environ.get("CUSTOM_PREFIX", "🐦 *New Tweet*")

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://nitter.privacyredirect.com",
]

seen_ids: set = set()
executor = ThreadPoolExecutor(max_workers=10)

# ─── /start command ───────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usernames_list = "\n".join([f"• @{u}" for u in TWITTER_USERNAMES])
    msg = (
        f"👋 Welcome!\n\n"
        f"🤖 Main ek Twitter → Telegram Auto-Forward Bot hoon!\n\n"
        f"🐦 Main in accounts ko monitor kar raha hoon:\n"
        f"{usernames_list}\n\n"
        f"⚡ Kya karta hoon:\n"
        f"• Naye tweets automatically forward karta hoon\n"
        f"• Photos aur Videos bhi bhejta hoon\n"
        f"• Har {POLL_INTERVAL} seconds mein check karta hoon\n\n"
        f"📌 Commands:\n"
        f"/start - Yeh message dikhao\n"
        f"/status - Bot ka status check karo\n\n"
        f"✅ Bot chal raha hai — tweets aate rahenge!"
    )
    await update.message.reply_text(msg)

# ─── /status command ──────────────────────────────────────────────────────────
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"🟢 Bot Status: Active\n\n"
        f"👥 Accounts monitored: {len(TWITTER_USERNAMES)}\n"
        f"⏱ Poll interval: {POLL_INTERVAL} seconds\n"
        f"📨 Tweets seen so far: {len(seen_ids)}\n\n"
        f"✅ Sab theek chal raha hai!"
    )
    await update.message.reply_text(msg)

# ─── Nitter RSS Fetch ─────────────────────────────────────────────────────────
def _fetch_sync(username: str):
    import requests
    last_error = None
    for instance in NITTER_INSTANCES:
        url = f"{instance}/{username}/rss"
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                continue
            feed = feedparser.parse(resp.content)
            if feed.entries and feed.feed.get("title"):
                log.info(f"✓ {instance} → @{username}")
                return feed.entries
            last_error = "Empty feed"
        except requests.exceptions.Timeout:
            log.warning(f"⏰ Timeout: {instance} skipping...")
            last_error = "Timeout"
        except Exception as e:
            last_error = str(e)
    raise Exception(f"All Nitter failed for @{username}: {last_error}")

async def fetch_tweets(username: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _fetch_sync, username)

# ─── Media Extraction ─────────────────────────────────────────────────────────
def extract_image(html: str):
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html)
    if match:
        img_url = match.group(1)
        if img_url.startswith("/pic/"):
            import urllib.parse
            decoded = urllib.parse.unquote(img_url[5:])
            if not decoded.startswith("http"):
                decoded = "https://pbs.twimg.com/" + decoded
            return decoded
        elif img_url.startswith("http"):
            return img_url
    return None

def has_video_indicator(html: str) -> bool:
    return any(x in html.lower() for x in ['card.html', 'video', 'player', 'amplify'])

def download_video(tweet_url: str):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            outpath = os.path.join(tmpdir, "video.%(ext)s")
            ydl_opts = {
                "outtmpl": outpath,
                "format": "best[filesize<50M]/best",
                "quiet": True,
                "no_warnings": True,
                "extractor_args": {"twitter": {"api": ["syndication"]}},
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(tweet_url, download=True)
                if info:
                    for f in os.listdir(tmpdir):
                        if f.startswith("video"):
                            filepath = os.path.join(tmpdir, f)
                            with open(filepath, "rb") as vf:
                                return vf.read(), f.split(".")[-1]
    except Exception as e:
        log.warning(f"⚠️ Video download failed: {e}")
    return None, None

# ─── Format Caption ───────────────────────────────────────────────────────────
def format_caption(entry, username: str) -> str:
    summary = entry.get("summary", entry.get("title", ""))
    summary = re.sub(r'<img[^>]+>', '', summary)
    summary = re.sub(r'<[^>]+>', '', summary).strip()
    summary = summary.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    summary = re.sub(r'\n{3,}', '\n\n', summary).strip()
    link = entry.get("link", "")
    link = re.sub(r"https?://[^/]+/", "https://twitter.com/", link)
    try:
        dt = datetime(*entry.published_parsed[:6])
        timestamp = dt.strftime("%d %b %Y, %I:%M %p UTC")
    except:
        timestamp = entry.get("published", "")
    return (
        f"{CUSTOM_PREFIX}\n\n"
        f"👤 *@{username}*\n"
        f"🕐 {timestamp}\n\n"
        f"{summary}\n\n"
        f"[View on X ↗]({link})"
    )

# ─── Telegram Sender ──────────────────────────────────────────────────────────
async def send_tweet(entry, username: str):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    caption = format_caption(entry, username)
    summary_html = entry.get("summary", "")
    raw_link = entry.get("link", "")
    tweet_url = re.sub(r"https?://[^/]+/", "https://twitter.com/", raw_link)

    if has_video_indicator(summary_html):
        loop = asyncio.get_event_loop()
        video_data, ext = await loop.run_in_executor(executor, download_video, tweet_url)
        if video_data:
            try:
                await bot.send_video(chat_id=TELEGRAM_CHAT_ID, video=video_data, caption=caption, parse_mode="Markdown", supports_streaming=True)
                log.info("✅ Sent with video!")
                return
            except Exception as e:
                log.warning(f"⚠️ Video send failed: {e}")

    image_url = extract_image(summary_html)
    if image_url:
        try:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image_url, caption=caption, parse_mode="Markdown")
            log.info("✅ Sent with image!")
            return
        except Exception as e:
            log.warning(f"⚠️ Image send failed: {e}")

    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode="Markdown", disable_web_page_preview=False)
        log.info("✅ Sent as text!")
    except Exception as e:
        log.error(f"❌ Telegram error: {e}")

# ─── Check User ───────────────────────────────────────────────────────────────
async def check_user(username: str):
    try:
        entries = await fetch_tweets(username)
        new_count = 0
        for entry in reversed(entries):
            uid = entry.get("id") or entry.get("link")
            if uid in seen_ids:
                continue
            if not INCLUDE_RETWEETS and "RT by" in entry.get("title", ""):
                seen_ids.add(uid)
                continue
            await send_tweet(entry, username)
            seen_ids.add(uid)
            new_count += 1
            await asyncio.sleep(0.5)
        if new_count:
            log.info(f"📨 @{username}: {new_count} new tweet(s)")
        else:
            log.info(f"😴 @{username}: no new tweets")
    except Exception as e:
        log.error(f"Error (@{username}): {e}")

# ─── Forward Loop ─────────────────────────────────────────────────────────────
async def forward_loop():
    log.info("🌱 Seeding...")
    seed_results = await asyncio.gather(*[fetch_tweets(u) for u in TWITTER_USERNAMES], return_exceptions=True)
    for username, result in zip(TWITTER_USERNAMES, seed_results):
        if isinstance(result, Exception):
            log.warning(f"⚠️ Seed failed @{username}: {result}")
        else:
            for e in result:
                seen_ids.add(e.get("id") or e.get("link"))
            log.info(f"✓ @{username} seeded {len(result)}")
    log.info("✅ Watching for NEW tweets!\n")
    while True:
        await asyncio.gather(*[check_user(u) for u in TWITTER_USERNAMES])
        log.info(f"💤 Sleeping {POLL_INTERVAL}s...")
        await asyncio.sleep(POLL_INTERVAL)

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Twitter → Telegram Bot starting...")
    log.info(f"📋 Monitoring: {', '.join(TWITTER_USERNAMES)}")

    # Setup bot with commands
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))

    # Run both: command listener + forward loop simultaneously
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("✅ Bot commands active (/start, /status)")
        await forward_loop()  # runs forever
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
