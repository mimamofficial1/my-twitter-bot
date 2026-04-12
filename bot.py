import tweepy
import telegram
import asyncio
import os
import json
import logging
from datetime import datetime

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]   # Railway captures stdout logs
)
log = logging.getLogger(__name__)

# ─── Config from Environment Variables ───────────────────────────────────────
def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"❌ Missing required env var: {key}")
    return val

TWITTER_BEARER_TOKEN  = require_env("TWITTER_BEARER_TOKEN")
TELEGRAM_BOT_TOKEN    = require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID      = require_env("TELEGRAM_CHAT_ID")
TWITTER_USERNAMES     = [u.strip() for u in require_env("TWITTER_USERNAMES").split(",")]

POLL_INTERVAL         = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
INCLUDE_RETWEETS      = os.environ.get("INCLUDE_RETWEETS", "false").lower() == "true"
INCLUDE_REPLIES       = os.environ.get("INCLUDE_REPLIES", "false").lower() == "true"
FORWARD_MEDIA         = os.environ.get("FORWARD_MEDIA", "true").lower() == "true"
CUSTOM_PREFIX         = os.environ.get("CUSTOM_PREFIX", "🐦 *New Tweet*")

# ─── State (in-memory for Railway; persists within session) ──────────────────
state = {}

# ─── Twitter Client ───────────────────────────────────────────────────────────
client = tweepy.Client(bearer_token=TWITTER_BEARER_TOKEN, wait_on_rate_limit=True)

def get_user_id(username: str) -> str:
    username = username.lstrip("@")
    user = client.get_user(username=username)
    if user.data:
        return str(user.data.id)
    raise ValueError(f"User @{username} not found")

def fetch_new_tweets(user_id: str, since_id: str = None):
    kwargs = dict(
        id=user_id,
        max_results=10,
        tweet_fields=["created_at", "attachments", "entities"],
        expansions=["attachments.media_keys"],
        media_fields=["url", "type"],
    )
    if since_id:
        kwargs["since_id"] = since_id
    return client.get_users_tweets(**kwargs)

# ─── Format Message ───────────────────────────────────────────────────────────
def format_message(tweet, username: str) -> str:
    tweet_url = f"https://twitter.com/{username}/status/{tweet.id}"
    timestamp = tweet.created_at.strftime("%d %b %Y, %I:%M %p UTC") if tweet.created_at else ""
    return (
        f"{CUSTOM_PREFIX}\n\n"
        f"👤 *@{username}*\n"
        f"🕐 {timestamp}\n\n"
        f"{tweet.text}\n\n"
        f"[View on X ↗]({tweet_url})"
    )

# ─── Telegram Sender ──────────────────────────────────────────────────────────
async def send_to_telegram(message: str, photo_url: str = None):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    try:
        if photo_url and FORWARD_MEDIA:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=photo_url,
                caption=message,
                parse_mode="Markdown"
            )
        else:
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
    log.info("🚀 Twitter → Telegram Bot started on Railway")
    log.info(f"📋 Monitoring: {', '.join(TWITTER_USERNAMES)}")
    log.info(f"⏱  Poll every {POLL_INTERVAL}s")

    # Resolve usernames → IDs
    user_map = {}
    for username in TWITTER_USERNAMES:
        clean = username.lstrip("@")
        try:
            uid = get_user_id(clean)
            user_map[uid] = clean
            log.info(f"✓ @{clean} → {uid}")
        except Exception as e:
            log.error(f"✗ @{clean}: {e}")

    if not user_map:
        log.error("No valid users found. Exiting.")
        return

    while True:
        for user_id, username in user_map.items():
            try:
                response = fetch_new_tweets(user_id, since_id=state.get(user_id))

                if not response.data:
                    log.info(f"No new tweets from @{username}")
                    continue

                # Media lookup
                media_lookup = {}
                if response.includes and "media" in response.includes:
                    for m in response.includes["media"]:
                        media_lookup[m.media_key] = m

                for tweet in reversed(response.data):
                    if not INCLUDE_RETWEETS and tweet.text.startswith("RT @"):
                        continue
                    if not INCLUDE_REPLIES and tweet.text.startswith("@"):
                        continue

                    photo_url = None
                    if FORWARD_MEDIA and tweet.attachments:
                        for mk in tweet.attachments.get("media_keys", []):
                            m = media_lookup.get(mk)
                            if m and m.type == "photo":
                                photo_url = m.url
                                break

                    msg = format_message(tweet, username)
                    await send_to_telegram(msg, photo_url=photo_url)
                    await asyncio.sleep(1)

                state[user_id] = str(response.data[0].id)

            except tweepy.errors.TooManyRequests:
                log.warning("⏳ Rate limited. Waiting 15 min...")
                await asyncio.sleep(900)
            except Exception as e:
                log.error(f"Error (@{username}): {e}")

        log.info(f"💤 Sleeping {POLL_INTERVAL}s...")
        await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(run())
