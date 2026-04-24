import asyncio, os, logging, re, json, tempfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import telegram
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import twikit

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Env vars ──────────────────────────────────────────────────────────────────
def env(key):
    v = os.environ.get(key)
    if not v:
        raise EnvironmentError(f"Missing env var: {key}")
    return v

BOT_TOKEN      = env("TELEGRAM_BOT_TOKEN")
CHAT_ID        = env("TELEGRAM_CHAT_ID")
USERNAMES      = [u.strip().lstrip("@") for u in env("TWITTER_USERNAMES").split(",") if u.strip()]
COOKIES_JSON   = env("TWITTER_COOKIES")
POLL           = max(30, int(os.environ.get("POLL_INTERVAL_SECONDS", "60")))
SKIP_RT        = os.environ.get("INCLUDE_RETWEETS", "false").lower() != "true"
PREFIX         = os.environ.get("CUSTOM_PREFIX", "🐦 New Tweet")

seen: set = set()
pool = ThreadPoolExecutor(max_workers=4)
client: twikit.Client = None

# ── Twitter init ──────────────────────────────────────────────────────────────
async def init_client():
    global client
    raw = json.loads(COOKIES_JSON)
    # Support both list [{name,value,...}] and dict {name:value}
    if isinstance(raw, list):
        cdict = {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
    else:
        cdict = raw
    path = "/tmp/cookies.json"
    with open(path, "w") as f:
        json.dump(cdict, f)
    client = twikit.Client(language="en-US")
    client.load_cookies(path)
    log.info(f"✅ Cookies loaded ({len(cdict)} cookies)")

# ── Fetch user tweets ─────────────────────────────────────────────────────────
async def get_tweets(username: str):
    try:
        user = await client.get_user_by_screen_name(username)
        raw_tweets = await user.get_tweets("Tweets", count=10)
        return list(raw_tweets)
    except Exception as e:
        log.error(f"❌ @{username} fetch error: {e}")
        return []

# ── Safe tweet parser ─────────────────────────────────────────────────────────
def parse_tweet(tw):
    """Extract fields safely from any twikit Tweet object version"""
    def g(*names, default=None):
        for n in names:
            try:
                v = getattr(tw, n, None)
                if v is not None:
                    return v
            except Exception:
                pass
        return default

    # ID
    tid = str(g("id", "rest_id", default="") or "")

    # Text — try multiple sources
    text = g("full_text", "text") or ""
    if not isinstance(text, str):
        text = ""
    # Also check legacy dict
    if not text:
        leg = g("legacy") or {}
        if isinstance(leg, dict):
            text = leg.get("full_text") or leg.get("text") or ""
    text = re.sub(r"https://t\.co/\S+", "", text).strip()

    # Timestamp
    ts = g("created_at") or ""
    if not ts:
        leg = g("legacy") or {}
        if isinstance(leg, dict):
            ts = leg.get("created_at", "")
    try:
        dt = datetime.strptime(str(ts), "%a %b %d %H:%M:%S +0000 %Y")
        timestamp = (dt + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p IST")
    except Exception:
        timestamp = str(ts)

    # Media
    photo = None
    has_vid = False
    try:
        media = g("media") or []
        if not media:
            leg = g("legacy") or {}
            if isinstance(leg, dict):
                ee = leg.get("extended_entities") or {}
                media = ee.get("media") or []
        for m in media:
            if isinstance(m, dict):
                t2 = m.get("type", "")
                if t2 == "photo" and not photo:
                    photo = m.get("media_url_https") or m.get("media_url")
                elif t2 in ("video", "animated_gif"):
                    has_vid = True
            else:
                t2 = getattr(m, "type", "")
                if t2 == "photo" and not photo:
                    photo = getattr(m, "media_url_https", None) or getattr(m, "url", None)
                elif t2 in ("video", "animated_gif"):
                    has_vid = True
    except Exception:
        pass

    # Is retweet?
    is_rt = g("retweeted_tweet") is not None or text.startswith("RT @")

    return tid, text, timestamp, photo, has_vid, is_rt

# ── Caption formatter ─────────────────────────────────────────────────────────
def make_caption(text, timestamp, username, tid):
    url = f"https://twitter.com/{username}/status/{tid}"
    return f"{PREFIX}\n\n👤 @{username}\n🕐 {timestamp}\n\n{text}\n\nView on X: {url}"

# ── Video downloader ──────────────────────────────────────────────────────────
def dl_video(tweet_url):
    try:
        with tempfile.TemporaryDirectory() as d:
            opts = {
                "outtmpl": f"{d}/v.%(ext)s",
                "format": "best[filesize<50M]/best",
                "quiet": True, "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.extract_info(tweet_url, download=True)
            files = os.listdir(d)
            if files:
                with open(f"{d}/{files[0]}", "rb") as f:
                    return f.read()
    except Exception as e:
        log.warning(f"Video dl failed: {e}")
    return None

# ── Telegram sender ───────────────────────────────────────────────────────────
async def send(text, timestamp, username, tid, photo, has_vid):
    bot = telegram.Bot(token=BOT_TOKEN)
    cap = make_caption(text, timestamp, username, tid)
    turl = f"https://twitter.com/{username}/status/{tid}"

    # Try video
    if has_vid:
        loop = asyncio.get_event_loop()
        vdata = await loop.run_in_executor(pool, dl_video, turl)
        if vdata:
            try:
                await bot.send_video(chat_id=CHAT_ID, video=vdata, caption=cap, supports_streaming=True)
                log.info(f"✅ Video → @{username}")
                return
            except Exception as e:
                log.warning(f"Video send err: {e}")

    # Try photo
    if photo:
        try:
            await bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=cap)
            log.info(f"✅ Photo → @{username}")
            return
        except Exception as e:
            log.warning(f"Photo send err: {e}")

    # Text fallback
    try:
        await bot.send_message(chat_id=CHAT_ID, text=cap, disable_web_page_preview=False)
        log.info(f"✅ Text → @{username}")
    except Exception as e:
        log.error(f"❌ Send failed @{username}: {e}")

# ── Check one user ────────────────────────────────────────────────────────────
async def check(username: str):
    tweets = await get_tweets(username)
    new = 0
    for tw in reversed(tweets):
        try:
            tid, text, timestamp, photo, has_vid, is_rt = parse_tweet(tw)
            if not tid or tid in seen:
                continue
            if SKIP_RT and is_rt:
                seen.add(tid)
                continue
            await send(text, timestamp, username, tid, photo, has_vid)
            seen.add(tid)
            new += 1
            await asyncio.sleep(1)
        except Exception as e:
            log.error(f"❌ Parse/send @{username}: {e}")
    if new:
        log.info(f"📨 @{username}: {new} new tweet(s)!")
    else:
        log.info(f"😴 @{username}: no new")

# ── Bot commands ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ul = "\n".join(f"• @{u}" for u in USERNAMES)
    await update.message.reply_text(
        f"👋 Welcome!\n\n🤖 Twitter → Telegram Bot\n\n"
        f"🐦 Monitoring:\n{ul}\n\n"
        f"⚡ Checking every {POLL}s\n\n"
        f"/start — This message\n/status — Bot status\n\n✅ Bot is active!"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🟢 Active\n👥 Accounts: {len(USERNAMES)}\n"
        f"⏱ Interval: {POLL}s\n📨 Tracked tweets: {len(seen)}\n✅ All good!"
    )

# ── Main forward loop ─────────────────────────────────────────────────────────
async def loop():
    await init_client()

    # Seed — don't send old tweets
    log.info("🌱 Seeding...")
    for u in USERNAMES:
        try:
            tweets = await get_tweets(u)
            for tw in tweets:
                try:
                    tid, *_ = parse_tweet(tw)
                    if tid:
                        seen.add(tid)
                except Exception:
                    pass
            log.info(f"✓ @{u} seeded {len(tweets)} tweets")
        except Exception as e:
            log.warning(f"Seed @{u}: {e}")
        await asyncio.sleep(1)

    log.info(f"✅ Now watching {len(USERNAMES)} accounts!\n")

    while True:
        for u in USERNAMES:
            await check(u)
            await asyncio.sleep(2)
        log.info(f"💤 Sleeping {POLL}s...")
        await asyncio.sleep(POLL)

# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    log.info(f"🚀 Bot starting | {len(USERNAMES)} accounts | Poll: {POLL}s")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await loop()
        await app.updater.stop()
        await app.stop()

if __name__ == "__main__":
    asyncio.run(main())
