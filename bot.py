import asyncio, os, logging, re, json, tempfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import telegram, yt_dlp, twikit

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

def env(k):
    v = os.environ.get(k)
    if not v: raise EnvironmentError(f"Missing: {k}")
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
client: twikit.Client = None

# ── Init ──────────────────────────────────────────────────────────────────────
async def init():
    global client
    raw = json.loads(COOKIES_JSON)
    cdict = {c["name"]: c["value"] for c in raw} if isinstance(raw, list) else raw
    with open("/tmp/ck.json", "w") as f: json.dump(cdict, f)
    client = twikit.Client(language="en-US")
    client.load_cookies("/tmp/ck.json")
    log.info(f"✅ Cookies loaded ({len(cdict)})")

# ── Fetch ─────────────────────────────────────────────────────────────────────
async def get_tweets(username):
    try:
        user = await client.get_user_by_screen_name(username)
        tweets = await user.get_tweets("Tweets", count=10)
        return list(tweets)
    except Exception as e:
        log.error(f"❌ @{username}: {e}")
        return []

# ── Parse tweet safely ────────────────────────────────────────────────────────
def parse(tw):
    # ID
    tid = str(getattr(tw, "id", "") or getattr(tw, "rest_id", "") or "")

    # Text — try all possible attributes
    text = ""
    for attr in ("full_text", "text"):
        try:
            v = getattr(tw, attr, None)
            if isinstance(v, str) and len(v) > 0:
                text = v
                break
        except: pass
    text = re.sub(r"https://t\.co/\S+", "", text).strip()

    # Time
    ts = ""
    try:
        ca = str(getattr(tw, "created_at", "") or "")
        dt = datetime.strptime(ca, "%a %b %d %H:%M:%S +0000 %Y")
        ts = (dt + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p IST")
    except: pass

    # Photo/video
    photo, has_vid = None, False
    try:
        for m in (getattr(tw, "media", None) or []):
            mt = getattr(m, "type", "") if hasattr(m, "type") else m.get("type", "")
            if mt == "photo" and not photo:
                photo = getattr(m, "media_url_https", None) or (m.get("media_url_https") if isinstance(m, dict) else None)
            elif mt in ("video", "animated_gif"):
                has_vid = True
    except: pass

    # Retweet check
    is_rt = bool(getattr(tw, "retweeted_tweet", None)) or text.startswith("RT @")

    return tid, text, ts, photo, has_vid, is_rt

# ── Send ──────────────────────────────────────────────────────────────────────
async def send(tid, text, ts, username, photo, has_vid):
    bot = telegram.Bot(token=BOT_TOKEN)
    url = f"https://twitter.com/{username}/status/{tid}"
    cap = f"{PREFIX}\n\n👤 @{username}\n🕐 {ts}\n\n{text}\n\nView on X: {url}"

    if has_vid:
        try:
            def dl():
                with tempfile.TemporaryDirectory() as d:
                    with yt_dlp.YoutubeDL({"outtmpl": f"{d}/v.%(ext)s", "format": "best[filesize<50M]/best", "quiet": True}) as y:
                        y.extract_info(url, download=True)
                    fs = os.listdir(d)
                    if fs:
                        with open(f"{d}/{fs[0]}", "rb") as f: return f.read()
                return None
            vdata = await asyncio.get_event_loop().run_in_executor(pool, dl)
            if vdata:
                await bot.send_video(chat_id=CHAT_ID, video=vdata, caption=cap, supports_streaming=True)
                log.info(f"✅ Video @{username}"); return
        except Exception as e:
            log.warning(f"Video fail: {e}")

    if photo:
        try:
            await bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=cap)
            log.info(f"✅ Photo @{username}"); return
        except Exception as e:
            log.warning(f"Photo fail: {e}")

    try:
        await bot.send_message(chat_id=CHAT_ID, text=cap, disable_web_page_preview=False)
        log.info(f"✅ Text @{username}")
    except Exception as e:
        log.error(f"❌ @{username}: {e}")

# ── Check user ────────────────────────────────────────────────────────────────
async def check(username):
    tweets = await get_tweets(username)
    new = 0
    for tw in reversed(tweets):
        try:
            tid, text, ts, photo, has_vid, is_rt = parse(tw)
            if not tid or tid in seen: continue
            if SKIP_RT and is_rt:
                seen.add(tid); continue
            seen.add(tid)
            await send(tid, text, ts, username, photo, has_vid)
            new += 1
            await asyncio.sleep(1)
        except Exception as e:
            log.error(f"❌ parse @{username}: {e}")
    log.info(f"📨 @{username}: {new} new!" if new else f"😴 @{username}: no new")

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    log.info(f"🚀 Bot starting | {len(USERNAMES)} accounts | {POLL}s poll")
    await init()

    log.info("🌱 Seeding...")
    for u in USERNAMES:
        try:
            tws = await get_tweets(u)
            for t in tws:
                try:
                    tid, *_ = parse(t)
                    if tid: seen.add(tid)
                except: pass
            log.info(f"✓ @{u} seeded {len(tws)}")
        except Exception as e:
            log.warning(f"Seed @{u}: {e}")
        await asyncio.sleep(1)

    log.info(f"✅ Watching!\n")
    while True:
        for u in USERNAMES:
            await check(u)
            await asyncio.sleep(2)
        log.info(f"💤 {POLL}s...")
        await asyncio.sleep(POLL)

if __name__ == "__main__":
    asyncio.run(main())
