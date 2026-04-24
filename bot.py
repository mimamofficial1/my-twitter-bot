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
        raise EnvironmentError(f"❌ Missing: {key}")
    return val

TELEGRAM_BOT_TOKEN = require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = require_env("TELEGRAM_CHAT_ID")
TWITTER_USERNAMES  = [u.strip().lstrip("@") for u in require_env("TWITTER_USERNAMES").split(",") if u.strip()]
TWITTER_COOKIES    = require_env("TWITTER_COOKIES")
POLL_INTERVAL      = max(30, int(os.environ.get("POLL_INTERVAL_SECONDS", "60")))
INCLUDE_RETWEETS   = os.environ.get("INCLUDE_RETWEETS", "false").lower() == "true"
CUSTOM_PREFIX      = os.environ.get("CUSTOM_PREFIX", "🐦 New Tweet")

seen_ids: set = set()
executor = ThreadPoolExecutor(max_workers=5)
twitter_client = None

# ─── Init Twitter ─────────────────────────────────────────────────────────────
async def init_twitter():
    global twitter_client
    client = twikit.Client(language="en-US")
    cookies_raw = json.loads(TWITTER_COOKIES)
    if isinstance(cookies_raw, list):
        cookies_dict = {c["name"]: c["value"] for c in cookies_raw if "name" in c}
    else:
        cookies_dict = cookies_raw
    cookies_file = "/tmp/tw_cookies.json"
    with open(cookies_file, "w") as f:
        json.dump(cookies_dict, f)
    client.load_cookies(cookies_file)
    log.info(f"✅ Cookies loaded: {list(cookies_dict.keys())}")
    twitter_client = client

# ─── Safe attribute getter ────────────────────────────────────────────────────
def safe_get(obj, *attrs, default=None):
    for attr in attrs:
        try:
            val = getattr(obj, attr, None)
            if val is not None:
                return val
        except:
            pass
    return default

# ─── Fetch tweets ─────────────────────────────────────────────────────────────
async def fetch_tweets(username: str):
    try:
        user = await twitter_client.get_user_by_screen_name(username)
        tweets = await user.get_tweets('Tweets', count=10)
        result = list(tweets)
        if result:
            # Debug: log first tweet's attributes once
            t = result[0]
            attrs = [a for a in dir(t) if not a.startswith('_')]
            log.info(f"🔍 @{username} tweet attrs: {attrs[:15]}")
            log.info(f"🔍 tweet.text = {getattr(t, 'text', 'N/A')[:80]}")
        return result
    except Exception as e:
        log.error(f"❌ fetch @{username}: {e}")
        return []

# ─── Extract tweet data safely ────────────────────────────────────────────────
def extract_tweet_data(tweet):
    """Safely extract id, text, created_at, media from any twikit tweet object"""
    # Get ID
    tid = str(safe_get(tweet, 'id', 'rest_id', default=''))
    
    # Get text
    text = safe_get(tweet, 'full_text', 'text', default='')
    if not text:
        # Try from legacy dict
        legacy = safe_get(tweet, 'legacy', default={})
        if isinstance(legacy, dict):
            text = legacy.get('full_text') or legacy.get('text') or ''
    text = re.sub(r'https://t\.co/\S+', '', str(text)).strip()

    # Get created_at
    created_at = safe_get(tweet, 'created_at', default='')
    if not created_at:
        legacy = safe_get(tweet, 'legacy', default={})
        if isinstance(legacy, dict):
            created_at = legacy.get('created_at', '')

    # Get media
    photo_url = None
    has_video = False
    try:
        media_list = safe_get(tweet, 'media', default=[]) or []
        if not media_list:
            legacy = safe_get(tweet, 'legacy', default={})
            if isinstance(legacy, dict):
                media_list = (legacy.get('extended_entities') or {}).get('media', [])
        for m in (media_list or []):
            mtype = m.get('type', '') if isinstance(m, dict) else safe_get(m, 'type', default='')
            if mtype == 'photo' and not photo_url:
                photo_url = (m.get('media_url_https') if isinstance(m, dict) else safe_get(m, 'media_url_https', default=None))
            elif mtype in ('video', 'animated_gif'):
                has_video = True
    except Exception as e:
        log.warning(f"Media extract error: {e}")

    # Is retweet?
    is_rt = safe_get(tweet, 'retweeted_tweet', default=None) is not None

    return tid, text, created_at, photo_url, has_video, is_rt

# ─── Format caption ───────────────────────────────────────────────────────────
def format_caption(text, created_at, username, tweet_id):
    try:
        dt = datetime.strptime(created_at, "%a %b %d %H:%M:%S +0000 %Y")
        timestamp = (dt + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p IST")
    except:
        timestamp = created_at or ''
    tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"
    return (
        f"{CUSTOM_PREFIX}\n\n"
        f"👤 @{username}\n"
        f"🕐 {timestamp}\n\n"
        f"{text}\n\n"
        f"View on X: {tweet_url}"
    )

# ─── Download video ───────────────────────────────────────────────────────────
def download_video(tweet_url):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {"outtmpl": f"{tmpdir}/video.%(ext)s", "format": "best[filesize<50M]/best", "quiet": True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.extract_info(tweet_url, download=True)
                for f in os.listdir(tmpdir):
                    with open(f"{tmpdir}/{f}", "rb") as vf:
                        return vf.read()
    except Exception as e:
        log.warning(f"Video dl failed: {e}")
    return None

# ─── Send to Telegram ─────────────────────────────────────────────────────────
async def send_to_telegram(text, created_at, username, tweet_id, photo_url, has_video):
    bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
    caption = format_caption(text, created_at, username, tweet_id)
    tweet_url = f"https://twitter.com/{username}/status/{tweet_id}"

    if has_video:
        loop = asyncio.get_event_loop()
        vdata = await loop.run_in_executor(executor, download_video, tweet_url)
        if vdata:
            try:
                await bot.send_video(chat_id=TELEGRAM_CHAT_ID, video=vdata, caption=caption, supports_streaming=True)
                log.info(f"✅ Video sent @{username}")
                return
            except Exception as e:
                log.warning(f"Video send fail: {e}")

    if photo_url:
        try:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=photo_url, caption=caption)
            log.info(f"✅ Photo sent @{username}")
            return
        except Exception as e:
            log.warning(f"Photo send fail: {e}")

    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=caption, disable_web_page_preview=False)
        log.info(f"✅ Text sent @{username}")
    except Exception as e:
        log.error(f"❌ Send fail @{username}: {e}")

# ─── Check user ───────────────────────────────────────────────────────────────
async def check_user(username: str):
    tweets = await fetch_tweets(username)
    new_count = 0
    for tweet in reversed(tweets):
        try:
            tid, text, created_at, photo_url, has_video, is_rt = extract_tweet_data(tweet)
            if not tid or tid in seen_ids:
                continue
            if not INCLUDE_RETWEETS and is_rt:
                seen_ids.add(tid)
                continue
            await send_to_telegram(text, created_at, username, tid, photo_url, has_video)
            seen_ids.add(tid)
            new_count += 1
            await asyncio.sleep(1)
        except Exception as e:
            log.error(f"❌ tweet error @{username}: {e}")
    log.info(f"📨 @{username}: {new_count} new!" if new_count else f"😴 @{username}: no new")

# ─── Bot commands ─────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ulist = "\n".join([f"• @{u}" for u in TWITTER_USERNAMES])
    await update.message.reply_text(
        f"👋 Welcome!\n\n🤖 Twitter → Telegram Bot\n\n"
        f"🐦 Monitoring:\n{ulist}\n\n"
        f"⚡ Har {POLL_INTERVAL}s check\n\n"
        f"/start /status\n\n✅ Active!"
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🟢 Active\n👥 Accounts: {len(TWITTER_USERNAMES)}\n"
        f"⏱ Poll: {POLL_INTERVAL}s\n📨 Tracked: {len(seen_ids)}\n✅ Theek hai!"
    )

# ─── Main loop ────────────────────────────────────────────────────────────────
async def forward_loop():
    await init_twitter()

    log.info("🌱 Seeding...")
    for username in TWITTER_USERNAMES:
        try:
            tweets = await fetch_tweets(username)
            for t in tweets:
                tid, *_ = extract_tweet_data(t)
                if tid:
                    seen_ids.add(tid)
            log.info(f"✓ @{username} seeded {len(tweets)}")
        except Exception as e:
            log.warning(f"Seed @{username}: {e}")
        await asyncio.sleep(1)

    log.info(f"✅ Watching {len(TWITTER_USERNAMES)} accounts!\n")
    while True:
        for username in TWITTER_USERNAMES:
            await check_user(username)
            await asyncio.sleep(2)
        log.info(f"💤 Sleeping {POLL_INTERVAL}s...")
        await asyncio.sleep(POLL_INTERVAL)

async def main():
    log.info("🚀 Bot starting...")
    log.info(f"📋 {len(TWITTER_USERNAMES)} accounts | {POLL_INTERVAL}s poll")
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
