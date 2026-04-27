import os
import json
import asyncio
import logging
import requests
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
TW_PASSWORD        = os.environ.get("TW_PASSWORD", "")
TW_EMAIL           = os.environ.get("TW_EMAIL", "")
SEEN_IDS_FILE      = "seen_ids.json"

# ─── SEEN IDs (file se load/save) ────────────────────────────────────────────
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
        logger.warning(f"sendPhoto failed, trying text...")
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

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TW_USERNAME, TW_PASSWORD, TW_EMAIL]):
        logger.error("❌ Env variables missing!")
        return

    seen_ids = load_seen_ids()
    logger.info(f"📂 {len(seen_ids)} seen IDs loaded")

    api = API()

    # Account add karo agar nahi hai
    accounts = await api.pool.get_all()
    if not accounts:
        logger.info("🔑 Twitter account add kar raha hoon...")
        await api.pool.add_account(TW_USERNAME, TW_PASSWORD, TW_EMAIL, "")
        await api.pool.login_all()
        logger.info("✅ Login successful!")
    else:
        logger.info(f"✅ Account ready: {accounts[0].username}")

    new_count = 0

    for username in TWITTER_USERNAMES:
        try:
            user = await api.user_by_login(username)
            if not user:
                logger.error(f"❌ User not found: @{username}")
                continue

            logger.info(f"🔍 Checking @{username}...")
            tweets = await gather(api.user_tweets(user.id, limit=10))

            new_tweets = [t for t in tweets if t.id not in seen_ids]

            for tweet in reversed(new_tweets):
                seen_ids.add(tweet.id)
                caption   = format_caption(tweet)
                image_url = get_best_image(tweet)

                if image_url:
                    logger.info(f"📸 Sending photo tweet from @{username}")
                    send_telegram_photo(image_url, caption)
                else:
                    logger.info(f"📝 Sending text tweet from @{username}")
                    send_telegram_text(caption)

                new_count += 1
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"❌ Error for @{username}: {e}")

        await asyncio.sleep(2)

    save_seen_ids(seen_ids)
    logger.info(f"✅ Done! {new_count} naye tweets bheje. {len(seen_ids)} total seen.")

if __name__ == "__main__":
    asyncio.run(main())
