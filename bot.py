import asyncio
import os
import logging
import re
import feedparser
import telegram
from datetime import datetime, timedelta
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
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
    "https://nitter.privacyredirect.com",
]

seen_ids: set = set()
executor = ThreadPoolExecutor(max_workers=10)

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
        dt_ist = dt + timedelta(hours=5, minutes=30)
        timestamp = dt_ist.strftime("%d %b %Y, %I:%M %p IST")
    except:
        timestamp = entry.get("published", "")
    return (
        f"{CUSTOM_PREFIX}\n\n"
        f"👤 *@{username}*\n"
        f"🕐 {timestamp}\n\n"
        f"{summary}\n\n"
        f"[View on X ↗]({link})"
    )

async def send_to_telegram(entry, username: str):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    caption = format_caption(entry, username)
    image_url = extract_image(entry.get("summary", ""))
    try:
        if image_url:
            try:
                await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=image_url, caption=caption, parse_mode="Markdown")
                log.info("✅ Sent with image!")
                return
            except Exception as e:
                log.warning(f"⚠️ Image failed: {e}")
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, parse_mode="Markdown", disable_web_page_preview=False)
        log.info("✅ Sent as text!")
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
            await send_to_telegram(entry, username)
            seen_ids.add(uid)
            new_count += 1
            await asyncio.sleep(0.5)
        if new_count:
            log.info(f"📨 @{username}: {new_count} new tweet(s)")
        else:
            log.info(f"😴 @{username}: no new tweets")
    except Exception as e:
        log.error(f"Error (@{username}): {e}")

async def run():
    log.info("🚀 Twitter → Telegram Bot (Nitter RSS)")
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
