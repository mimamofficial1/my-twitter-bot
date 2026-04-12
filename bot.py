import asyncio
import os
import logging
import hashlib
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

# ─── Config from Environment Variables ───────────────────────────────────────
def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"❌ Missing env var: {key}")
    return val

TELEGRAM_BOT_TOKEN  = require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = require_env("TELEGRAM_CHAT_ID")
TWITTER_USERNAMES   = [u.strip().lstrip("@") for u in require_env("TWITTER_USERNAMES").split(",")]
POLL_INTERVAL       = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))
INCLUDE_RETWEETS    = os.environ.get("INCLUDE_RETWEETS", "false").lower() == "true"
CUSTOM_PREFIX       = os.environ.get("CUSTOM_PREFIX", "🐦 *New Tweet*")

# RSSHub public instance — ya apna self-host kar sakte ho
RSSHUB_BASE = os.environ.get("RSSHUB_BASE", "https://rsshub.app")

# ─── Seen tweets (in-memory) ─────────────────────────────────────────────────
seen_ids: set = set()

# ─── RSS Feed Fetch ───────────────────────────────────────────────────────────
def get_rss_url(username: str) -> str:
    return f"{RSSHUB_BASE}/twitter/user/{username}"

def fetch_tweets(username: str):
    url = get_rss_url(username)
    feed = feedparser.parse(url)
    if feed.bozo and not feed.entries:
        raise Exception(f"RSS fetch failed for @{username}: {feed.bozo_exception}")
    return feed.entries

# ─── Format Message ───────────────────────────────────────────────────────────
def format_message(entry, username: str) -> str:
    title = entry.get("title", "")
    link  = entry.get("link", f"https://twitter.com/{username}")
    published = entry.get("published", "")

    # Clean up summary (remove HTML tags roughly)
    summary = entry.get("summary", title)
    import re
    summary = re.sub(r"<[^>]+>", "", summary).strip()

    try:
        dt = datetime(*entry.published_parsed[:6])
        timestamp = dt.strftime("%d %b %Y, %I:%M %p UTC")
    except:
        timestamp = published

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
    log.info("🚀 Twitter → Telegram Bot started (RSS mode)")
    log.info(f"📋 Monitoring: {', '.join(TWITTER_USERNAMES)}")
    log.info(f"⏱  Poll every {POLL_INTERVAL}s")

    # First run — seed seen_ids without sending (avoid flood on startup)
    log.info("🌱 Seeding existing tweets (won't send old ones)...")
    for username in TWITTER_USERNAMES:
        try:
            entries = fetch_tweets(username)
            for entry in entries:
                seen_ids.add(entry.get("id") or entry.get("link"))
            log.info(f"✓ @{username} — seeded {len(entries)} tweets")
        except Exception as e:
            log.warning(f"⚠️ Could not seed @{username}: {e}")

    log.info("✅ Seeding done. Now watching for NEW tweets...\n")

    while True:
        for username in TWITTER_USERNAMES:
            try:
                entries = fetch_tweets(username)
                new_count = 0

                for entry in reversed(entries):  # oldest first
                    uid = entry.get("id") or entry.get("link")
                    if uid in seen_ids:
                        continue

                    title = entry.get("title", "")

                    # Filter retweets
                    if not INCLUDE_RETWEETS and (
                        title.lower().startswith("rt @") or "RT @" in title
                    ):
                        seen_ids.add(uid)
                        continue

                    msg = format_message(entry, username)
                    await send_to_telegram(msg)
                    seen_ids.add(uid)
                    new_count += 1
                    await asyncio.sleep(1)

                if new_count:
                    log.info(f"📨 @{username}: {new_count} new tweet(s) sent")
                else:
                    log.info(f"😴 @{username}: no new tweets")

            except Exception as e:
                log.error(f"Error (@{username}): {e}")

        log.info(f"💤 Sleeping {POLL_INTERVAL}s...")
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run())
