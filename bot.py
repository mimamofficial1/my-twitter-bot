import os
import time
import logging
import requests
import feedparser
from datetime import datetime
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ─── CONFIG (Railway Environment Variables se aayega) ───────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
TWITTER_USERNAMES  = os.environ.get("TWITTER_USERNAMES", "").split(",")
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL", "120"))  # seconds

# Nitter instances (fallback chain)
NITTER_INSTANCES = [
    "https://nitter.privacyredirect.com",
    "https://nitter.poast.org",
    "https://nitter.net",
    "https://nitter.1d4.us",
    "https://nitter.kavin.rocks",
]

# ─── STATE (seen tweet IDs store karta hai) ──────────────────────────────────
seen_ids: set = set()


def get_nitter_rss(username: str) -> feedparser.FeedDict | None:
    """Try each Nitter instance until one works."""
    for instance in NITTER_INSTANCES:
        url = f"{instance}/{username}/rss"
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200 and "<rss" in resp.text:
                feed = feedparser.parse(resp.text)
                if feed.entries:
                    logger.info(f"✅ {instance} worked for @{username}")
                    return feed
        except Exception as e:
            logger.warning(f"❌ {instance} failed: {e}")
    logger.error(f"All Nitter instances failed for @{username}")
    return None


def extract_image(entry) -> str | None:
    """RSS entry se pehli image URL nikaalte hain."""
    # 1) media:content tag
    if hasattr(entry, "media_content") and entry.media_content:
        url = entry.media_content[0].get("url", "")
        if url:
            return url

    # 2) HTML summary mein <img> tag
    summary = entry.get("summary", "")
    if summary:
        soup = BeautifulSoup(summary, "html.parser")
        img = soup.find("img")
        if img and img.get("src"):
            src = img["src"]
            # Nitter relative URLs fix karo
            if src.startswith("/pic/"):
                src = f"https://nitter.privacyredirect.com{src}"
            return src

    return None


def clean_text(html: str) -> str:
    """HTML strip karke plain text."""
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(separator=" ").strip()


def send_telegram_photo(image_url: str, caption: str):
    """Image + caption Telegram pe bhejo."""
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": image_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    resp = requests.post(api, json=payload, timeout=15)
    if not resp.ok:
        logger.warning(f"sendPhoto failed ({resp.status_code}), falling back to text")
        send_telegram_text(caption)


def send_telegram_text(text: str):
    """Sirf text Telegram pe bhejo."""
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    resp = requests.post(api, json=payload, timeout=15)
    if not resp.ok:
        logger.error(f"sendMessage failed: {resp.status_code} {resp.text}")


def format_caption(username: str, entry) -> str:
    """Caption banao: username + time + tweet text + link."""
    tweet_text = clean_text(entry.get("summary", ""))
    # Nitter links ko twitter.com pe redirect karo
    link = entry.get("link", "").replace("nitter.privacyredirect.com", "twitter.com") \
                                .replace("nitter.poast.org", "twitter.com") \
                                .replace("nitter.net", "twitter.com") \
                                .replace("nitter.1d4.us", "twitter.com") \
                                .replace("nitter.kavin.rocks", "twitter.com")

    published = entry.get("published", "")
    try:
        dt = datetime(*entry.published_parsed[:6])
        time_str = dt.strftime("%d %b %Y, %I:%M %p")
    except Exception:
        time_str = published

    caption = (
        f"🐦 <b>@{username}</b>\n"
        f"🕐 {time_str}\n\n"
        f"{tweet_text}\n\n"
        f"🔗 <a href='{link}'>Tweet dekho</a>"
    )
    return caption


def process_feed(username: str):
    """Ek user ke naye tweets fetch karke Telegram pe bhejo."""
    feed = get_nitter_rss(username)
    if not feed:
        return

    new_tweets = []
    for entry in feed.entries:
        tweet_id = entry.get("id", entry.get("link", ""))
        if tweet_id and tweet_id not in seen_ids:
            new_tweets.append((tweet_id, entry))

    # Reverse karo taaki purane pehle aayein
    for tweet_id, entry in reversed(new_tweets):
        seen_ids.add(tweet_id)
        caption = format_caption(username, entry)
        image_url = extract_image(entry)

        if image_url:
            logger.info(f"📸 Sending photo tweet from @{username}")
            send_telegram_photo(image_url, caption)
        else:
            logger.info(f"📝 Sending text tweet from @{username}")
            send_telegram_text(caption)

        time.sleep(1)  # Rate limit avoid


def initialize():
    """Pehli run pe purane tweets mark karo (bhejo nahi)."""
    logger.info("🚀 Bot start ho raha hai — purane tweets skip kar raha hoon...")
    for username in TWITTER_USERNAMES:
        username = username.strip()
        if not username:
            continue
        feed = get_nitter_rss(username)
        if feed:
            for entry in feed.entries:
                tweet_id = entry.get("id", entry.get("link", ""))
                if tweet_id:
                    seen_ids.add(tweet_id)
            logger.info(f"✅ @{username} — {len(feed.entries)} purane tweets skip kiye")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("❌ TELEGRAM_BOT_TOKEN aur TELEGRAM_CHAT_ID set karo!")
        return

    if not any(u.strip() for u in TWITTER_USERNAMES):
        logger.error("❌ TWITTER_USERNAMES set karo!")
        return

    initialize()

    logger.info(f"👀 Monitoring: {', '.join('@' + u.strip() for u in TWITTER_USERNAMES)}")
    logger.info(f"⏱️ Har {CHECK_INTERVAL} seconds pe check karega")

    while True:
        for username in TWITTER_USERNAMES:
            username = username.strip()
            if username:
                process_feed(username)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
