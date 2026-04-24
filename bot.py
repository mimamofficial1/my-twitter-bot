import asyncio
import os
import logging
import re
import json
import telegram
import yt_dlp
import tempfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import twikit

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

def require_env(key):
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(f"❌ Missing env var: {key}")
    return val

TELEGRAM_BOT_TOKEN  = require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID    = require_env("TELEGRAM_CHAT_ID")
TWITTER_USERNAMES   = [u.strip().lstrip("@") for u in require_env("TWITTER_USERNAMES").split(",")]
TWITTER_USERNAME    = require_env("TWITTER_USERNAME")   # Your Twitter login
TWITTER_EMAIL       = require_env("TWITTER_EMAIL")      # Your Twitter email
TWITTER_PASSWORD    = require_env("TWITTER_PASSWORD")   # Your Twitter password
POLL_INTERVAL       = max(30, int(os.environ.get("POLL_INTERVAL_SECONDS", "60")))
INCLUDE_RETWEETS    = os.environ.get("INCLUDE_RETWEETS", "false").lower() == "true"
CUSTOM_PREFIX       = os.environ.get("CUSTOM_PREFIX", "🐦 New Tweet")

seen_ids: set = set()
executor = ThreadPoolExecutor(max_workers=5)
twitter_client = None

# ─── Twitter Login ────────────────────────────────────────────────────────────
async def init_twitter():
    global twitter_client
    client = twikit.Client(language="en-US")
    cookies_file = "/tmp/twitter_cookies.json"
    
    if os.path.exists(cookies_file):
        log.info("Loading saved cookies...")
        client.load_cookies(cookies_file)
    else:
        log.info("Logging in to Twitter...")
        await client.login(
            auth_info_1=TWITTER_USERNAME,
            auth_info_2=TWITTER_EMAIL,
            password=TWITTER_PASSWORD
        )
        client.save_cookies(cookies_file)
        log.info("✅ Logged in & cookies saved!")
    
    twitter_client = client
    return client

# ─── Fetch Tweets ─────────────────────────────────────────────────────────────
async def fetch_tweets(username: str):
    global twitter_client
    try:
        user = await twitter_client.get_user_by_screen_name(username)
        tweets = await twitter_client.get_user_tweets(user.id, tweet_type="Tweets", count=10)
        log.info(f"✓ @{username}: {len(tweets)} tweets fetched")
        return tweets
    except Exception as e:
        log.error(f"Fetch failed @{username}: {e}")
        return []

# ─── Format Caption ───────────────────────────────────────────────────────────
def format_caption(tweet, username: str) -> str:
    text = tweet.full_text or tweet.text or ""
    text = re.sub(r'https://t\.co/\S+', '', text).strip()

    tweet_url = f"https://twitter.com/{username}/status/{tweet.id}"

    try:
        # twikit returns created_at as string like "Thu Apr 24 01:01:00 +0000 2026"
        dt = datetime.strptime(tweet.created_at, "%a %b %d %H:%M:%S +0000 %Y")
        dt_ist = dt + timedelta(hours=5, minutes=30)
        timestamp = dt_ist.strftime("%d %b %Y, %I:%M %p IST")
    except:
        timestamp = str(tweet.created_at)

    return (
        f"{CUSTOM_PREFIX}\n\n"
        f"👤 @{username}\n"
        f"🕐 {timestamp}\n\n"
        f"{text}\n\n"
        f"View on X: {tweet_url}"
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

# ─── Send to Telegram ─────────────────────────────────────────────────────────
async def send_tweet(tweet, username: str):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    caption = format_caption(tweet, username)
    tweet_url = f"https://twitter.com/{username}/status/{tweet.id}"

    # Check for media
    photo_url = None
    has_video = False
    
    if hasattr(tweet, 'media') and tweet.media:
        for m in tweet.media:
            mtype = getattr(m, 'type', '')
            if mtype == 'photo' and not photo_url:
                photo_url = getattr(m, 'media_url_https', None) or getattr(m, 'url', None)
            elif mtype in ('video', 'animated_gif'):
                has_video = True

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
            log.warning(f"⚠️ Photo failed: {e}")

    # Text fallback
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, disable_web_page_preview=False)
        log.info("✅ Sent as text!")
    except Exception as e:
        log.error(f"❌ Send failed: {e}")

# ─── Check One User ───────────────────────────────────────────────────────────
async def check_user(username: str):
    tweets = await fetch_tweets(username)
    new_count = 0
    for tweet in reversed(list(tweets)):
        tid = str(tweet.id)
        if tid in seen_ids:
            continue
        if not INCLUDE_RETWEETS and hasattr(tweet, 'retweeted_tweet') and tweet.retweeted_tweet:
            seen_ids.add(tid)
            continue
        await send_tweet(tweet, username)
        seen_ids.add(tid)
        new_count += 1
        await asyncio.sleep(1)
    if new_count:
        log.info(f"📨 @{username}: {new_count} new!")
    else:
        log.info(f"😴 @{username}: no new tweets")

# ─── Bot Commands ─────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    usernames_list = "\n".join([f"• @{u}" for u in TWITTER_USERNAMES])
    msg = (
        f"👋 Welcome!\n\n"
        f"🤖 Twitter to Telegram Auto-Forward Bot\n\n"
        f"🐦 Monitor ho rahe hain:\n{usernames_list}\n\n"
        f"⚡ Features:\n"
        f"• Tweets, Photos, Videos forward\n"
        f"• Har {POLL_INTERVAL}s mein check\n\n"
        f"📌 /start - Yeh message\n"
        f"📌 /status - Bot status\n\n"
        f"✅ Bot active hai!"
    )
    await update.message.reply_text(msg)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"🟢 Bot Active\n\n"
        f"👥 Accounts: {len(TWITTER_USERNAMES)}\n"
        f"⏱ Poll: {POLL_INTERVAL}s\n"
        f"📨 Tracked: {len(seen_ids)}\n\n"
        f"✅ Sab theek!"
    )
    await update.message.reply_text(msg)

# ─── Main Loop ────────────────────────────────────────────────────────────────
async def forward_loop():
    await init_twitter()

    # Seed
    log.info("🌱 Seeding...")
    for username in TWITTER_USERNAMES:
        try:
            tweets = await fetch_tweets(username)
            for t in tweets:
                seen_ids.add(str(t.id))
            log.info(f"✓ @{username} seeded {len(tweets)}")
            await asyncio.sleep(2)  # small delay between users while seeding
        except Exception as e:
            log.warning(f"⚠️ @{username}: {e}")

    log.info("✅ Watching for new tweets!\n")

    while True:
        # Check users one by one with small delay — avoids rate limiting
        for username in TWITTER_USERNAMES:
            await check_user(username)
            await asyncio.sleep(3)  # 3s between each account
        log.info(f"💤 Sleeping {POLL_INTERVAL}s...")
        await asyncio.sleep(POLL_INTERVAL)

async def main():
    log.info("🚀 Bot starting (twikit mode)...")
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
