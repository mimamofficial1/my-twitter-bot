import os
import asyncio
import logging
import requests
from datetime import datetime, timezone
from twscrape import API, gather
from twscrape.logger import set_log_level

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
set_log_level("ERROR")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
TWITTER_USERNAMES   = [u.strip() for u in os.environ.get("TWITTER_USERNAMES", "").split(",") if u.strip()]
CHECK_INTERVAL      = int(os.environ.get("CHECK_INTERVAL", "120"))

# Twitter account credentials (Railway variables mein daalna)
TW_USERNAME  = os.environ.get("TW_USERNAME", "")
TW_PASSWORD  = os.environ.get("TW_PASSWORD", "")
TW_EMAIL     = os.environ.get("TW_EMAIL", "")

# ─── STATE ────────────────────────────────────────────────────────────────────
seen_ids: set = set()
user_id_cache: dict = {}


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram_photo(image_url: str, caption: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": image_url,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }
    resp = requests.post(api, json=payload, timeout=15)
    if not resp.ok:
        logger.warning(f"sendPhoto failed ({resp.status_code}), trying text...")
        send_telegram_text(caption)


def send_telegram_text(text: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(api, json=payload, timeout=15)
    if not resp.ok:
        logger.error(f"sendMessage failed: {resp.status_code} {resp.text}")


def format_caption(tweet) -> str:
    username = tweet.user.username
    name     = tweet.user.displayname
    text     = tweet.rawContent or ""
    link     = f"https://twitter.com/{username}/status/{tweet.id}"

    try:
        time_str = tweet.date.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        time_str = ""

    caption = (
        f"🐦 <b>{name}</b> (@{username})\n"
        f"🕐 {time_str}\n\n"
        f"{text}\n\n"
        f"🔗 <a href='{link}'>Tweet dekho</a>"
    )
    return caption


def get_best_image(tweet) -> str:
    """Tweet se best image URL nikaalte hain."""
    try:
        if tweet.media and tweet.media.photos:
            return tweet.media.photos[0].url
    except Exception:
        pass
    return ""


# ─── TWSCRAPE ─────────────────────────────────────────────────────────────────
async def setup_account(api: API):
    """Twitter account add karo agar already nahi hai."""
    accounts = await api.pool.get_all()
    if not accounts:
        logger.info("🔑 Twitter account add kar raha hoon...")
        await api.pool.add_account(
            username=TW_USERNAME,
            password=TW_PASSWORD,
            email=TW_EMAIL,
            email_password="",  # agar email password nahi hai
        )
        await api.pool.login_all()
        logger.info("✅ Twitter login successful!")
    else:
        logger.info(f"✅ Account already logged in: {accounts[0].username}")


async def get_user_id(api: API, username: str) -> int:
    """Username se user ID nikaalte hain (cache karte hain)."""
    if username in user_id_cache:
        return user_id_cache[username]
    user = await api.user_by_login(username)
    if user:
        user_id_cache[username] = user.id
        return user.id
    return 0


async def fetch_new_tweets(api: API, username: str):
    """Naye tweets fetch karke Telegram pe bhejo."""
    user_id = await get_user_id(api, username)
    if not user_id:
        logger.error(f"❌ User not found: @{username}")
        return

    try:
        tweets = await gather(api.user_tweets(user_id, limit=10))
    except Exception as e:
        logger.error(f"❌ Error fetching tweets for @{username}: {e}")
        return

    new_tweets = [t for t in tweets if t.id not in seen_ids]

    for tweet in reversed(new_tweets):
        seen_ids.add(tweet.id)
        caption   = format_caption(tweet)
        image_url = get_best_image(tweet)

        if image_url:
            logger.info(f"📸 Photo tweet from @{username} — {tweet.id}")
            send_telegram_photo(image_url, caption)
        else:
            logger.info(f"📝 Text tweet from @{username} — {tweet.id}")
            send_telegram_text(caption)

        await asyncio.sleep(1)


async def initialize(api: API):
    """Pehli run pe purane tweets mark karo — spam nahi aayega."""
    logger.info("🚀 Bot start — purane tweets skip kar raha hoon...")
    for username in TWITTER_USERNAMES:
        user_id = await get_user_id(api, username)
        if not user_id:
            continue
        try:
            tweets = await gather(api.user_tweets(user_id, limit=20))
            for t in tweets:
                seen_ids.add(t.id)
            logger.info(f"✅ @{username} — {len(tweets)} purane tweets skip kiye")
        except Exception as e:
            logger.error(f"❌ @{username} initialize error: {e}")
        await asyncio.sleep(1)


async def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TW_USERNAME, TW_PASSWORD, TW_EMAIL]):
        logger.error("❌ Saare environment variables set karo!")
        logger.error("Chahiye: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TW_USERNAME, TW_PASSWORD, TW_EMAIL")
        return

    if not TWITTER_USERNAMES:
        logger.error("❌ TWITTER_USERNAMES set karo!")
        return

    api = API()
    await setup_account(api)
    await initialize(api)

    logger.info(f"👀 Monitoring {len(TWITTER_USERNAMES)} accounts...")
    logger.info(f"⏱️ Har {CHECK_INTERVAL} seconds pe check karega")

    while True:
        for username in TWITTER_USERNAMES:
            await fetch_new_tweets(api, username)
            await asyncio.sleep(2)  # accounts ke beech thodi delay
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
