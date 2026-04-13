import asyncio
import os
import logging
import re
import feedparser
import telegram
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

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
    "https://nitter.privacyredirect.com",
    "https://nitter.poast.org",
    "https://nitter.net",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
]

seen_ids: set = set()
executor = ThreadPoolExecutor(max_workers=10)

def _fetch_sync(username: str):
    last_error = None
    for instance in NITTER_INSTANCES:
        url = f"{instance}/{username}/rss"
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            if feed.entries and feed.feed.get("title"):
                log.info(f"✓ {instance} → @{username}")
                return feed.entries
            last_error = str(feed.bozo_exception) if feed.bozo else "Empty feed"
        except Exception as e:
            last_error = str(e)
    raise Exception(f"All Nitter failed for @{username}: {last_error}")

async def fetch_tweets(username: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _fetch_sync, username)

def format_message(entry, username: str) -> str:
    summary = entry.get("summary", entry.get("title", ""))
    summary = re.sub(r"<[^>]+>", "", summary).strip()
    summary = summary.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    link = entry.get("link", "")
    link = re.sub(r"https?://[^/]+/", "https://twitter.com/", link)
    try:
        dt = datetime(*entry.published_parsed[:6])
        timestamp = dt.strftime("%d %b %Y, %I:%M %p UTC")
    except:
        timestamp = entry.get("published", "")
    return (f"{CUSTOM_PREFIX}\n\n👤 *@{username}*\n🕐 {timestamp}\n\n{summary}\n\n[View on X ↗]({link})")

async def send_to_telegram(message: str):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode="Markdown", disable_web_page_preview=False)
        log.info("✅ Sent!")
    except Exception as e:
        log.error(f"❌ Telegram error: {e}")

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
            msg = format_message(entry, username)
            await send_to_telegram(msg)
            seen_ids.add(uid)
            new_count += 1
            await asyncio.sleep(0.5)
        log.info(f"📨 @{username}: {new_count} new" if new_count else f"😴 @{username}: no new tweets")
    except Exception as e:
        log.error(f"Error (@{username}): {e}")

async def run():
    log.info("🚀 Twitter → Telegram Bot (Parallel)")
    log.info(f"📋 Monitoring: {', '.join(TWITTER_USERNAMES)}")
    log.info(f"⏱  Poll every {POLL_INTERVAL}s")

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

if __name__ == "__main__":
    asyncio.run(run())
