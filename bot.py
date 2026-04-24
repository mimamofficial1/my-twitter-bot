import asyncio
import os
import logging
import re
import json
import requests
import telegram
import yt_dlp
import tempfile
from datetime import datetime, timezone
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
CUSTOM_PREFIX      = os.environ.get("CUSTOM_PREFIX", "🐦 New Tweet")

# Twitter's own public bearer token (used by their website)
TWITTER_BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I4xENYrAh74%2FtDkk3dYI05DHvHKN2PWDQVLB1bB7wc9v95%2F31E%2Frt%2FPqkpnM%3D"

seen_ids: set = set()
executor = ThreadPoolExecutor(max_workers=10)

# ─── Twitter Guest Token ───────────────────────────────────────────────────────
guest_token = None

def get_guest_token():
    global guest_token
    resp = requests.post(
        "https://api.twitter.com/1.1/guest/activate.json",
        headers={"Authorization": f"Bearer {TWITTER_BEARER}"},
        timeout=10
    )
    guest_token = resp.json().get("guest_token")
    log.info(f"✓ Guest token: {guest_token}")
    return guest_token

def twitter_headers():
    return {
        "Authorization": f"Bearer {TWITTER_BEARER}",
        "x-guest-token": guest_token or get_guest_token(),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://twitter.com/",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    }

# ─── Fetch Tweets via Syndication API (no auth needed!) ───────────────────────
def _fetch_sync(username: str):
    """Use Twitter's syndication API — free, no key needed"""
    try:
        url = f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
        params = {"count": 10, "includeAvailability": "true"}
        resp = requests.get(url, params=params, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": f"https://twitter.com/{username}",
        }, timeout=15)

        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}")

        # Extract JSON from the HTML response
        html = resp.text
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
        if not match:
            raise Exception("No JSON data found in response")

        data = json.loads(match.group(1))
        entries = (
            data.get("props", {})
                .get("pageProps", {})
                .get("timeline", {})
                .get("entries", [])
        )

        tweets = []
        for entry in entries:
            content = entry.get("content", {})
            tweet = content.get("tweet", {})
            if not tweet:
                continue
            tweets.append(tweet)

        log.info(f"✓ Syndication API → @{username}: {len(tweets)} tweets")
        return tweets

    except Exception as e:
        log.error(f"Syndication failed for @{username}: {e}")
        raise

async def fetch_tweets(username: str):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _fetch_sync, username)

# ─── Extract Media ────────────────────────────────────────────────────────────
def extract_media(tweet: dict):
    """Get image/video URL from tweet"""
    media_list = (
        tweet.get("entities", {}).get("media", []) or
        tweet.get("extended_entities", {}).get("media", [])
    )
    photo_url = None
    has_video = False
    for m in media_list:
        mtype = m.get("type", "")
        if mtype == "photo" and not photo_url:
            photo_url = m.get("media_url_https") or m.get("media_url")
        elif mtype in ("video", "animated_gif"):
            has_video = True
    return photo_url, has_video

# ─── Format Caption ───────────────────────────────────────────────────────────
def format_caption(tweet: dict, username: str) -> str:
    text = tweet.get("full_text") or tweet.get("text", "")
    # Remove media URLs from text
    for url_obj in tweet.get("entities", {}).get("urls", []):
        text = text.replace(url_obj.get("url", ""), url_obj.get("expanded_url", ""))
    # Remove pic.twitter.com links
    text = re.sub(r'https://t\.co/\S+', '', text).strip()

    tweet_id = tweet.get("id_str") or tweet.get("id")
    link = f"https://twitter.com/{username}/status/{tweet_id}"

    created_at = tweet.get("created_at", "")
    try:
        from datetime import timedelta
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S +0000 %Y")
        dt_ist = dt + timedelta(hours=5, minutes=30)
        timestamp = dt_ist.strftime("%d %b %Y, %I:%M %p IST")
    except:
        timestamp = created_at

    return (
        f"{CUSTOM_PREFIX}\n\n"
        f"👤 @{username}\n"
        f"🕐 {timestamp}\n\n"
        f"{text}\n\n"
        f"View on X: {link}"
    )

# ─── Video Download ───────────────────────────────────────────────────────────
def download_video(tweet_url: str):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            outpath = os.path.join(tmpdir, "video.%(ext)s")
            ydl_opts = {
                "outtmpl": outpath,
                "format": "best[filesize<50M]/best",
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(tweet_url, download=True)
                for f in os.listdir(tmpdir):
                    if f.startswith("video"):
                        with open(os.path.join(tmpdir, f), "rb") as vf:
                            return vf.read()
    except Exception as e:
        log.warning(f"⚠️ Video download failed: {e}")
    return None

# ─── Send Tweet to Telegram ───────────────────────────────────────────────────
async def send_tweet(tweet: dict, username: str):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    caption = format_caption(tweet, username)
    photo_url, has_video = extract_media(tweet)

    tweet_id = tweet.get("id_str") or tweet.get("id")
    tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"

    # Try video
    if has_video:
        loop = asyncio.get_event_loop()
        video_data = await loop.run_in_executor(executor, download_video, tweet_url)
        if video_data:
            try:
                await bot.send_video(chat_id=TELEGRAM_CHAT_ID, video=video_data, caption=caption, supports_streaming=True)
                log.info("✅ Sent with video!")
                return
            except Exception as e:
                log.warning(f"⚠️ Video send failed: {e}")

    # Try photo
    if photo_url:
        try:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=photo_url, caption=caption)
            log.info("✅ Sent with photo!")
            return
        except Exception as e:
            log.warning(f"⚠️ Photo send failed: {e}")

    # Text fallback
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, disable_web_page_preview=False)
        log.info("✅ Sent as text!")
    except Exception as e:
        log.error(f"❌ Send failed: {e}")

# ─── Check User ───────────────────────────────────────────────────────────────
async def check_user(username: str):
    try:
        tweets = await fetch_tweets(username)
        new_count = 0
        for tweet in reversed(tweets):
            tid = str(tweet.get("id_str") or tweet.get("id", ""))
            if not tid or tid in seen_ids:
                continue
            # Skip retweets
            if not INCLUDE_RETWEETS and tweet.get("retweeted_status"):
                seen_ids.add(tid)
                continue
            await send_tweet(tweet, username)
            seen_ids.add(tid)
            new_count += 1
            await asyncio.sleep(0.5)
        if new_count:
            log.info(f"📨 @{username}: {new_count} new!")
        else:
            log.info(f"😴 @{username}: no new tweets")
    except Exception as e:
        log.error(f"Error @{username}: {e}")

# ─── Bot Commands ─────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usernames_list = "\n".join([f"• @{u}" for u in TWITTER_USERNAMES])
    msg = (
        f"👋 Welcome!\n\n"
        f"🤖 Main Twitter to Telegram Auto-Forward Bot hoon!\n\n"
        f"🐦 Monitor kar raha hoon:\n{usernames_list}\n\n"
        f"⚡ Features:\n"
        f"• Tweets, Photos, Videos forward karta hoon\n"
        f"• Har {POLL_INTERVAL} seconds mein check karta hoon\n\n"
        f"📌 Commands:\n"
        f"/start - Yeh message\n"
        f"/status - Bot status\n\n"
        f"✅ Bot active hai!"
    )
    await update.message.reply_text(msg)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"🟢 Bot Status: Active\n\n"
        f"👥 Accounts: {len(TWITTER_USERNAMES)}\n"
        f"⏱ Poll: {POLL_INTERVAL}s\n"
        f"📨 Tweets tracked: {len(seen_ids)}\n\n"
        f"✅ Sab theek!"
    )
    await update.message.reply_text(msg)

# ─── Forward Loop ─────────────────────────────────────────────────────────────
async def forward_loop():
    # Get guest token first
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(executor, get_guest_token)

    log.info("🌱 Seeding existing tweets...")
    seed_results = await asyncio.gather(*[fetch_tweets(u) for u in TWITTER_USERNAMES], return_exceptions=True)
    for username, result in zip(TWITTER_USERNAMES, seed_results):
        if isinstance(result, Exception):
            log.warning(f"⚠️ Seed failed @{username}: {result}")
        else:
            for t in result:
                tid = str(t.get("id_str") or t.get("id", ""))
                if tid:
                    seen_ids.add(tid)
            log.info(f"✓ @{username} seeded {len(result)}")

    log.info("✅ Watching for NEW tweets!\n")
    cycle = 0
    while True:
        cycle += 1
        # Refresh guest token every 50 cycles
        if cycle % 50 == 0:
            try:
                await loop.run_in_executor(executor, get_guest_token)
            except:
                pass
        await asyncio.gather(*[check_user(u) for u in TWITTER_USERNAMES])
        log.info(f"💤 Sleeping {POLL_INTERVAL}s...")
        await asyncio.sleep(POLL_INTERVAL)

# ─── Main ─────────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 Twitter Bot starting (Syndication API mode)")
    log.info(f"📋 {len(TWITTER_USERNAMES)} accounts | Poll: {POLL_INTERVAL}s")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("status", status_command))

    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await forward_loop()
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
