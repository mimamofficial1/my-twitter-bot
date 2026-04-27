import asyncio, os, logging, re, json, tempfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import telegram, yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import twikit

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

def env(key):
    v = os.environ.get(key)
    if not v: raise EnvironmentError(f"Missing: {key}")
    return v

BOT_TOKEN    = env("TELEGRAM_BOT_TOKEN")
CHAT_ID      = env("TELEGRAM_CHAT_ID")
USERNAMES    = [u.strip().lstrip("@") for u in env("TWITTER_USERNAMES").split(",") if u.strip()]
COOKIES_JSON = env("TWITTER_COOKIES")
POLL         = max(30, int(os.environ.get("POLL_INTERVAL_SECONDS", "60")))
SKIP_RT      = os.environ.get("INCLUDE_RETWEETS", "false").lower() != "true"
PREFIX       = os.environ.get("CUSTOM_PREFIX", "🐦 New Tweet")

seen: set = set()
pool = ThreadPoolExecutor(max_workers=4)
tw: twikit.Client = None

async def init():
    global tw
    raw = json.loads(COOKIES_JSON)
    cdict = {c["name"]: c["value"] for c in raw} if isinstance(raw, list) else raw
    with open("/tmp/ck.json", "w") as f: json.dump(cdict, f)
    tw = twikit.Client(language="en-US")
    tw.load_cookies("/tmp/ck.json")
    log.info(f"✅ {len(cdict)} cookies loaded")

async def get_tweets(username):
    try:
        user = await tw.get_user_by_screen_name(username)
        return list(await user.get_tweets("Tweets", count=10))
    except Exception as e:
        log.error(f"❌ @{username}: {e}")
        return []

def tweet_id(tw_obj):
    for a in ("id", "rest_id"):
        v = getattr(tw_obj, a, None)
        if v: return str(v)
    return ""

def tweet_text(tw_obj):
    # Try direct attributes
    for a in ("full_text", "text"):
        v = getattr(tw_obj, a, None)
        if isinstance(v, str) and v:
            return re.sub(r"https://t\.co/\S+", "", v).strip()
    return ""

def tweet_time(tw_obj):
    ca = getattr(tw_obj, "created_at", None) or ""
    try:
        dt = datetime.strptime(str(ca), "%a %b %d %H:%M:%S +0000 %Y")
        return (dt + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p IST")
    except: return str(ca)

def tweet_media(tw_obj):
    photo, video = None, False
    try:
        ml = getattr(tw_obj, "media", None) or []
        for m in ml:
            mt = getattr(m, "type", "") if not isinstance(m, dict) else m.get("type","")
            if mt == "photo" and not photo:
                photo = getattr(m, "media_url_https", None) or (m.get("media_url_https") if isinstance(m, dict) else None)
            elif mt in ("video", "animated_gif"):
                video = True
    except: pass
    return photo, video

def is_retweet(tw_obj):
    return getattr(tw_obj, "retweeted_tweet", None) is not None

def caption(text, timestamp, username, tid):
    url = f"https://twitter.com/{username}/status/{tid}"
    return f"{PREFIX}\n\n👤 @{username}\n🕐 {timestamp}\n\n{text}\n\nView on X: {url}"

def dl_video(url):
    try:
        with tempfile.TemporaryDirectory() as d:
            with yt_dlp.YoutubeDL({"outtmpl": f"{d}/v.%(ext)s", "format": "best[filesize<50M]/best", "quiet": True}) as y:
                y.extract_info(url, download=True)
            fs = os.listdir(d)
            if fs:
                with open(f"{d}/{fs[0]}", "rb") as f: return f.read()
    except: pass
    return None

async def send(text, ts, username, tid, photo, has_vid):
    bot = telegram.Bot(token=BOT_TOKEN)
    cap = caption(text, ts, username, tid)
    turl = f"https://twitter.com/{username}/status/{tid}"

    if has_vid:
        vdata = await asyncio.get_event_loop().run_in_executor(pool, dl_video, turl)
        if vdata:
            try:
                await bot.send_video(chat_id=CHAT_ID, video=vdata, caption=cap, supports_streaming=True)
                log.info(f"✅ Video @{username}")
                return
            except Exception as e:
                log.warning(f"Video fail: {e}")

    if photo:
        try:
            await bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=cap)
            log.info(f"✅ Photo @{username}")
            return
        except Exception as e:
            log.warning(f"Photo fail: {e}")

    try:
        await bot.send_message(chat_id=CHAT_ID, text=cap, disable_web_page_preview=False)
        log.info(f"✅ Text @{username}")
    except Exception as e:
        log.error(f"❌ @{username}: {e}")

async def check(username):
    tweets = await get_tweets(username)
    new = 0
    for t in reversed(tweets):
        try:
            tid = tweet_id(t)
            if not tid or tid in seen: continue
            if SKIP_RT and is_retweet(t):
                seen.add(tid); continue
            seen.add(tid)  # add before send to prevent duplicates
            text = tweet_text(t)
            ts   = tweet_time(t)
            photo, has_vid = tweet_media(t)
            await send(text, ts, username, tid, photo, has_vid)
            new += 1
            await asyncio.sleep(1)
        except Exception as e:
            log.error(f"❌ tweet @{username}: {e}")
    log.info(f"📨 @{username}: {new} new!" if new else f"😴 @{username}: no new")

async def cmd_start(update: Update, ctx):
    ul = "\n".join(f"• @{u}" for u in USERNAMES)
    await update.message.reply_text(f"👋 Twitter → Telegram Bot\n\n🐦 Monitoring:\n{ul}\n\n⚡ Every {POLL}s\n\n/status — bot status\n✅ Active!")

async def cmd_status(update: Update, ctx):
    await update.message.reply_text(f"🟢 Active\n👥 {len(USERNAMES)} accounts\n⏱ {POLL}s\n📨 {len(seen)} tracked\n✅ Theek hai!")

async def main_loop():
    await init()
    log.info("🌱 Seeding...")
    for u in USERNAMES:
        try:
            tweets = await get_tweets(u)
            for t in tweets:
                tid = tweet_id(t)
                if tid: seen.add(tid)
            log.info(f"✓ @{u} seeded {len(tweets)}")
        except Exception as e:
            log.warning(f"Seed @{u}: {e}")
        await asyncio.sleep(1)

    log.info(f"✅ Watching {len(USERNAMES)} accounts!\n")
    while True:
        for u in USERNAMES:
            await check(u)
            await asyncio.sleep(2)
        log.info(f"💤 {POLL}s...")
        await asyncio.sleep(POLL)

async def main():
    log.info(f"🚀 Bot | {len(USERNAMES)} accounts | {POLL}s")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await main_loop()
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
