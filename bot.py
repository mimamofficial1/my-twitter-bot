import asyncio, os, logging, re, json, tempfile
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import telegram, yt_dlp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger(__name__)

# ── Patch twikit User to fix 'urls' crash ─────────────────────────────────────
import twikit.user as _um
_orig = _um.User.__init__
def _safe(self, client, data, *args, **kwargs):
    try:
        leg = data.get('legacy', {})
        if isinstance(leg, dict):
            ent = leg.setdefault('entities', {})
            ent.setdefault('description', {}).setdefault('urls', [])
            leg.setdefault('pinned_tweet_ids_str', [])
            for f in ('possibly_sensitive','can_dm','can_media_tag','want_retweets','has_custom_timelines'):
                leg.setdefault(f, False)
    except: pass
    try:
        _orig(self, client, data, *args, **kwargs)
    except Exception as e:
        leg = data.get('legacy', {}) if isinstance(data, dict) else {}
        self.id = data.get('rest_id', '') if isinstance(data, dict) else ''
        self.name = leg.get('name', '')
        self.screen_name = leg.get('screen_name', '')
        self._client = client
_um.User.__init__ = _safe

import twikit

def env(k):
    v = os.environ.get(k)
    if not v: raise EnvironmentError(f"Missing: {k}")
    return v

BOT_TOKEN    = env("TELEGRAM_BOT_TOKEN")
CHAT_ID      = env("TELEGRAM_CHAT_ID")
USERNAMES    = [u.strip().lstrip("@") for u in env("TWITTER_USERNAMES").split(",") if u.strip()]
COOKIES_JSON = env("TWITTER_COOKIES")
POLL         = max(60, int(os.environ.get("POLL_INTERVAL_SECONDS", "120")))
SKIP_RT      = os.environ.get("INCLUDE_RETWEETS", "false").lower() != "true"
PREFIX       = os.environ.get("CUSTOM_PREFIX", "🐦 New Tweet")

seen: set = set()
pool = ThreadPoolExecutor(max_workers=4)
client: twikit.Client = None
user_cache: dict = {}  # username → user object (cache to save API calls)

async def init():
    global client
    raw = json.loads(COOKIES_JSON)
    cdict = {c["name"]: c["value"] for c in raw} if isinstance(raw, list) else raw
    with open("/tmp/ck.json", "w") as f: json.dump(cdict, f)
    client = twikit.Client(language="en-US")
    client.load_cookies("/tmp/ck.json")
    log.info(f"✅ {len(cdict)} cookies loaded")

async def get_user(username):
    """Get user with caching — saves 1 API call per account per cycle"""
    if username not in user_cache:
        user_cache[username] = await client.get_user_by_screen_name(username)
        log.info(f"📦 Cached @{username}")
        await asyncio.sleep(2)  # small delay after getting user
    return user_cache[username]

async def get_tweets(username):
    for attempt in range(3):
        try:
            user = await get_user(username)
            tweets = list(await user.get_tweets("Tweets", count=10))
            return tweets
        except Exception as e:
            err = str(e)
            if '429' in err or 'Rate limit' in err:
                wait = 60 * (attempt + 1)  # 60s, 120s, 180s
                log.warning(f"⏳ Rate limit @{username} — waiting {wait}s...")
                await asyncio.sleep(wait)
                # Clear user cache on rate limit so fresh fetch next time
                user_cache.pop(username, None)
            else:
                log.error(f"❌ @{username}: {e}")
                return []
    return []

def parse(tw):
    tid = str(getattr(tw, "id", "") or "")
    text = ""
    for a in ("full_text", "text"):
        try:
            v = getattr(tw, a, None)
            if isinstance(v, str) and v:
                text = v; break
        except: pass
    text = re.sub(r"https://t\.co/\S+", "", text).strip()

    ts = ""
    try:
        ca = str(getattr(tw, "created_at", "") or "")
        dt = datetime.strptime(ca, "%a %b %d %H:%M:%S +0000 %Y")
        ts = (dt + timedelta(hours=5, minutes=30)).strftime("%d %b %Y, %I:%M %p IST")
    except: pass

    photo, has_vid = None, False
    try:
        for m in (getattr(tw, "media", None) or []):
            mt = getattr(m, "type", "") if hasattr(m, "type") else m.get("type","")
            if mt == "photo" and not photo:
                photo = getattr(m, "media_url_https", None) or (m.get("media_url_https") if isinstance(m,dict) else None)
            elif mt in ("video", "animated_gif"):
                has_vid = True
    except: pass

    is_rt = bool(getattr(tw, "retweeted_tweet", None)) or text.startswith("RT @")
    return tid, text, ts, photo, has_vid, is_rt

async def send(tid, text, ts, username, photo, has_vid):
    bot = telegram.Bot(token=BOT_TOKEN)
    url = f"https://twitter.com/{username}/status/{tid}"
    cap = f"{PREFIX}\n\n👤 @{username}\n🕐 {ts}\n\n{text}\n\nView on X: {url}"

    if has_vid:
        try:
            def dl():
                with tempfile.TemporaryDirectory() as d:
                    with yt_dlp.YoutubeDL({"outtmpl":f"{d}/v.%(ext)s","format":"best[filesize<50M]/best","quiet":True}) as y:
                        y.extract_info(url, download=True)
                    fs = os.listdir(d)
                    return open(f"{d}/{fs[0]}","rb").read() if fs else None
            vdata = await asyncio.get_event_loop().run_in_executor(pool, dl)
            if vdata:
                await bot.send_video(chat_id=CHAT_ID, video=vdata, caption=cap, supports_streaming=True)
                log.info(f"✅ Video @{username}"); return
        except Exception as e: log.warning(f"Video: {e}")

    if photo:
        try:
            await bot.send_photo(chat_id=CHAT_ID, photo=photo, caption=cap)
            log.info(f"✅ Photo @{username}"); return
        except Exception as e: log.warning(f"Photo: {e}")

    try:
        await bot.send_message(chat_id=CHAT_ID, text=cap, disable_web_page_preview=False)
        log.info(f"✅ Text @{username}")
    except Exception as e: log.error(f"❌ @{username}: {e}")

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
            log.error(f"❌ @{username} tweet: {e}")
    log.info(f"📨 @{username}: {new} new!" if new else f"😴 @{username}: no new")

async def main():
    log.info(f"🚀 Bot | {len(USERNAMES)} accounts | {POLL}s poll")
    await init()

    # Cache all users upfront with delays to avoid rate limit
    log.info("📦 Caching users...")
    for u in USERNAMES:
        try:
            await get_user(u)
        except Exception as e:
            log.warning(f"Cache @{u}: {e}")
        await asyncio.sleep(3)

    # Seed
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
        await asyncio.sleep(5)  # 5s between each account during seeding

    log.info("✅ Watching!\n")
    while True:
        for u in USERNAMES:
            await check(u)
            await asyncio.sleep(5)  # 5s between each account
        log.info(f"💤 {POLL}s...")
        await asyncio.sleep(POLL)

if __name__ == "__main__":
    asyncio.run(main())
