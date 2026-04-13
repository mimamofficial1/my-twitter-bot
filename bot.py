import asyncio
import os
import logging
import re
import feedparser
import telegram
from datetime import datetime

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"❌ Missing env var: {key}")
    return val

TELEGRAM_BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = require_env("TELEGRAM_CHAT_ID")
TWITTER_USERNAMES  = [u.strip().lstrip("@") for u in require_env("TWITTER_USERNAMES").split(",")]
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
INCLUDE_RETWEETS   = os.environ.get("INCLUDE_RETWEETS", "false").lower() == "true"
CUSTOM_PREFIX      = os.environ.get("CUSTOM_PREFIX", "🐦 *New Tweet*")

# Multiple Nitter instances — tries each until one works
NITTER_INSTANCES = [
    "https://nitter.privacyredirect.com",
    "https://nitter.poast.org",
    "https://nitter.net",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
]

# ─── State ────────────────────────────────────────────────────────────────────
seen_ids: set = set()

# ─── RSS Fetch with Fallback ──────────────────────────────────────────────────
def fetch_tweets(username: str):
    last_error = None
    for instance in NITTER_INSTANCES:
        url = f"{instance}/{username}/rss"
        try:
            feed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
            # Validate it's actually an RSS feed
            if feed.entries and feed.feed.get("title"):
                log.info(f"✓ Using {instance} for @{username}")
                return feed.entries
            elif feed.bozo:
                last_error = str(feed.bozo_exception)
                continue
            else:
                last_error = "Empty feed"
                continue
        except Exception as e:
            last_error = str(e)
            continue

    raise Exception(f"All Nitter instances failed for @{username}. Last error: {last_error}")

# ─── Format Message ───────────────────────────────────────────────────────────
def format_message(entry, username: str) -> str:
    # Clean HTML tags from summary
    summary = entry.get("summary", entry.get("title", ""))
    summary = re.sub(r"<[^>]+>", "", summary).strip()
    summary = summary.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')

    # Build original Twitter link (not nitter)
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
async def send_to_telegram(message: str):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=message,
            parse_mode="Markdown",
            disable_web_page_preview=False
        )
        log.info("✅ Sent to Telegram")
    except Exception as e:
        log.error(f"❌ Telegram error: {e}")

# ─── Main Loop ────────────────────────────────────────────────────────────────
async def run():
    log.info("🚀 Twitter → Telegram Bot started (Nitter RSS mode)")
    log.info(f"📋 Monitoring: {', '.join(TWITTER_USERNAMES)}")
    log.info(f"⏱  Poll every {POLL_INTERVAL}s")

    # Seed — don't send old tweets on startup
    log.info("🌱 Seeding existing tweets...")
    for username in TWITTER_USERNAMES:
        try:
            entries = fetch_tweets(username)
            for e in entries:
                seen_ids.add(e.get("id") or e.get("link"))
            log.info(f"✓ @{username} seeded {len(entries)} tweets")
        except Exception as ex:
            log.warning(f"⚠️ Seed failed @{username}: {ex}")

    log.info("✅ Watching for NEW tweets...\n")

    async def check_user(username: str):
        try:
            entries = fetch_tweets(username)
            new_count = 0
            for entry in reversed(entries):
                uid = entry.get("id") or entry.get("link")
                if uid in seen_ids:
                    continue
                title = entry.get("title", "")
                if not INCLUDE_RETWEETS and "RT by" in title:
                    seen_ids.add(uid)
                    continue
                msg = format_message(entry, username)
                await send_to_telegram(msg)
                seen_ids.add(uid)
                new_count += 1
                await asyncio.sleep(0.5)
            log.info(f"@{username}: {new_count} new tweet(s)" if new_count else f"@{username}: no new tweets")
        except Exception as e:
            log.error(f"Error (@{username}): {e}")

    while True:
        # Check ALL accounts simultaneously in parallel!
        await asyncio.gather(*[check_user(u) for u in TWITTER_USERNAMES])
        if POLL_INTERVAL > 0:
            log.info(f"💤 Sleeping {POLL_INTERVAL}s...")
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run())
