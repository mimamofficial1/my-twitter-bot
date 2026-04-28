import os
import json
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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TWITTER_USERNAMES  = [u.strip() for u in os.environ.get("TWITTER_USERNAMES", "").split(",") if u.strip()]
TW_USERNAME        = os.environ.get("TW_USERNAME", "")
TW_EMAIL           = os.environ.get("TW_EMAIL", "")
TW_AUTH_TOKEN      = os.environ.get("TW_AUTH_TOKEN", "")
TW_CT0             = os.environ.get("TW_CT0", "")
SEEN_IDS_FILE      = "seen_ids.json"

PER_USER_TIMEOUT   = 30   # max 30 seconds per user
TOTAL_TIMEOUT      = 300  # max 5 minutes total

# ─── SEEN IDs ─────────────────────────────────────────────────────────────────
def load_seen_ids() -> set:
    if os.path.exists(SEEN_IDS_FILE):
        with open(SEEN_IDS_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen_ids(seen: set):
    with open(SEEN_IDS_FILE, "w") as f:
        json.dump(list(seen), f)

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram_photo(image_url: str, caption: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    resp = requests.post(api, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": image_url,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }, timeout=15)
    if not resp.ok:
        send_telegram_text(caption)

def send_telegram_text(text: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(api, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=15)
    if not resp.ok:
        logger.error(f"sendMessage failed: {resp.status_code} {resp.text}")

def send_status(new_count: int, checked: int, errors: int):
    now = datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")
    icon = "🟢" if new_count > 0 else "🔵"
    tweet_line = f"📨 <b>{new_count}</b> naye tweets forward kiye" if new_count > 0 else "💤 Koi naya tweet nahi mila"
    msg = (
        f"{icon} <b>Bot Status — Active</b>\n"
        f"🕐 {now}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{tweet_line}\n"
        f"👀 {checked} accounts check kiye\n"
        f"❌ {errors} errors\n"
        f"⏭️ Agla run ~15 min mein"
    )
    send_telegram_text(msg)

def format_caption(tweet) -> str:
    username = tweet.user.username
    name     = tweet.user.displayname
    text     = tweet.rawContent or ""
    link     = f"https://twitter.com/{username}/status/{tweet.id}"
    try:
        time_str = tweet.date.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        time_str = ""
    return (
        f"🐦 <b>{name}</b> (@{username})\n"
        f"🕐 {time_str}\n\n"
        f"{text}\n\n"
        f"🔗 <a href='{link}'>Tweet dekho</a>"
    )

def get_best_image(tweet) -> str:
    try:
        if tweet.media and tweet.media.photos:
            return tweet.media.photos[0].url
    except Exception:
        pass
    return ""

# ─── FETCH WITH TIMEOUT ───────────────────────────────────────────────────────
async def fetch_user(api: API, username: str, seen_ids: set) -> tuple:
    """Returns (new_count, success)"""
    try:
        user = await asyncio.wait_for(api.user_by_login(username), timeout=PER_USER_TIMEOUT)
        if not user:
            logger.error(f"❌ Not found: @{username}")
            return 0, False

        tweets = await asyncio.wait_for(
            gather(api.user_tweets(user.id, limit=10)),
            timeout=PER_USER_TIMEOUT
        )

        new_tweets = [t for t in tweets if t.id not in seen_ids]
        count = 0

        for tweet in reversed(new_tweets):
            seen_ids.add(tweet.id)
            caption   = format_caption(tweet)
            image_url = get_best_image(tweet)

            if image_url:
                logger.info(f"📸 @{username}")
                send_telegram_photo(image_url, caption)
            else:
                logger.info(f"📝 @{username}")
                send_telegram_text(caption)

            count += 1
            await asyncio.sleep(0.5)

        logger.info(f"✅ @{username} — {count} naye tweets")
        return count, True

    except asyncio.TimeoutError:
        logger.error(f"⏰ Timeout: @{username} — skip kar raha hoon")
        return 0, False
    except Exception as e:
        logger.error(f"❌ Error @{username}: {e}")
        return 0, False

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TW_USERNAME, TW_AUTH_TOKEN, TW_CT0]):
        logger.error("❌ Env variables missing!")
        return

    seen_ids = load_seen_ids()
    logger.info(f"📂 {len(seen_ids)} seen IDs loaded")

    api = API()
    accounts = await api.pool.get_all()
    if not accounts:
        logger.info("🍪 Cookie se login kar raha hoon...")
        await api.pool.add_account(
            username=TW_USERNAME,
            password="dummy_not_needed",
            email=TW_EMAIL or f"{TW_USERNAME}@dummy.com",
            email_password="",
            cookies=f"auth_token={TW_AUTH_TOKEN}; ct0={TW_CT0}",
        )
        logger.info("✅ Cookie login successful!")
    else:
        logger.info(f"✅ Account ready: {accounts[0].username}")

    new_count   = 0
    checked     = 0
    error_count = 0

    async def run_all():
        nonlocal new_count, checked, error_count
        for username in TWITTER_USERNAMES:
            count, success = await fetch_user(api, username, seen_ids)
            new_count += count
            if success:
                checked += 1
            else:
                error_count += 1
            await asyncio.sleep(1)

    try:
        await asyncio.wait_for(run_all(), timeout=TOTAL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error("⏰ Total timeout — 5 min limit reached!")

    save_seen_ids(seen_ids)
    logger.info(f"✅ Done! {new_count} naye tweets. {len(seen_ids)} total seen.")
    send_status(new_count, checked, error_count)

if __name__ == "__main__":
    asyncio.run(main())
