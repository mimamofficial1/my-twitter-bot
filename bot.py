import os
import json
import asyncio
import logging
import requests
import instaloader
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
IG_USERNAMES       = [u.strip() for u in os.environ.get("IG_USERNAMES", "").split(",") if u.strip()]
IG_USERNAME        = os.environ.get("IG_USERNAME", "")   # Apna Instagram account
IG_PASSWORD        = os.environ.get("IG_PASSWORD", "")   # Apna Instagram password
SEEN_IDS_FILE      = "seen_ids.json"
MAX_POSTS          = 5   # Har account ke latest kitne posts check karein

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
def send_photo(image_url: str, caption: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    resp = requests.post(api, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": image_url,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }, timeout=20)
    if not resp.ok:
        logger.warning(f"sendPhoto failed: {resp.status_code}")
        send_text(caption)

def send_media_group(media_urls: list, caption: str):
    """Album — multiple images ek saath bhejo."""
    if not media_urls:
        return
    if len(media_urls) == 1:
        send_photo(media_urls[0], caption)
        return

    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
    media = []
    for i, url in enumerate(media_urls[:10]):  # Max 10 images
        item = {"type": "photo", "media": url}
        if i == 0:
            item["caption"] = caption[:1024]
            item["parse_mode"] = "HTML"
        media.append(item)

    resp = requests.post(api, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "media": media,
    }, timeout=30)

    if not resp.ok:
        logger.warning(f"sendMediaGroup failed: {resp.status_code} — single photo try kar raha hoon")
        send_photo(media_urls[0], caption)

def send_video(video_url: str, caption: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
    resp = requests.post(api, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "video": video_url,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }, timeout=30)
    if not resp.ok:
        send_text(caption)

def send_text(text: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(api, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }, timeout=15)
    if not resp.ok:
        logger.error(f"sendMessage failed: {resp.status_code}")

def send_status(new_count: int, checked: int, errors: int):
    now = datetime.now(timezone.utc).strftime("%d %b %Y, %I:%M %p UTC")
    icon = "🟢" if new_count > 0 else "🔵"
    post_line = f"📨 <b>{new_count}</b> naye posts forward kiye" if new_count > 0 else "💤 Koi naya post nahi mila"
    msg = (
        f"{icon} <b>Bot Status — Active</b>\n"
        f"🕐 {now}\n"
        f"━━━━━━━━━━━━━━\n"
        f"{post_line}\n"
        f"📸 Instagram → Telegram\n"
        f"👀 {checked} accounts check kiye\n"
        f"❌ {errors} errors\n"
        f"⏭️ Agla run ~15 min mein"
    )
    send_text(msg)

def format_caption(username: str, post) -> str:
    caption_text = post.caption or ""
    # Caption 500 chars tak
    if len(caption_text) > 500:
        caption_text = caption_text[:500] + "..."

    try:
        time_str = post.date_utc.strftime("%d %b %Y, %I:%M %p UTC")
    except Exception:
        time_str = ""

    post_url = f"https://www.instagram.com/p/{post.shortcode}/"

    return (
        f"📸 <b>@{username}</b>\n"
        f"🕐 {time_str}\n\n"
        f"{caption_text}\n\n"
        f"🔗 <a href='{post_url}'>Instagram pe dekho</a>"
    )

# ─── INSTAGRAM ────────────────────────────────────────────────────────────────
def process_account(loader: instaloader.Instaloader, username: str, seen_ids: set) -> tuple:
    """Ek Instagram account ke naye posts fetch karo."""
    new_count = 0
    try:
        profile = instaloader.Profile.from_username(loader.context, username)
        posts = []

        for post in profile.get_posts():
            if len(posts) >= MAX_POSTS:
                break
            posts.append(post)

        # Reverse karo — purane pehle
        for post in reversed(posts):
            post_id = str(post.mediaid)
            if post_id in seen_ids:
                continue

            seen_ids.add(post_id)
            caption = format_caption(username, post)

            try:
                if post.typename == "GraphSidecar":
                    # Album post — saari images nikalo
                    media_urls = []
                    for node in post.get_sidecar_nodes():
                        if node.is_video:
                            # Video node — skip ya alag bhejo
                            pass
                        else:
                            media_urls.append(node.display_url)

                    if media_urls:
                        logger.info(f"🖼️ Album ({len(media_urls)} images) @{username}")
                        send_media_group(media_urls, caption)
                    else:
                        send_text(caption)

                elif post.is_video:
                    logger.info(f"🎥 Video @{username}")
                    send_video(post.video_url, caption)

                else:
                    # Single image
                    logger.info(f"📸 Photo @{username}")
                    send_photo(post.url, caption)

                new_count += 1
                import time
                time.sleep(2)  # Telegram rate limit

            except Exception as e:
                logger.error(f"❌ Post send error @{username}: {e}")

        return new_count, True

    except instaloader.exceptions.ProfileNotExistsException:
        logger.error(f"❌ Profile not found: @{username}")
        return 0, False
    except instaloader.exceptions.LoginRequiredException:
        logger.error(f"❌ Login required for @{username}")
        return 0, False
    except Exception as e:
        logger.error(f"❌ Error @{username}: {e}")
        return 0, False

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logger.error("❌ TELEGRAM_BOT_TOKEN aur TELEGRAM_CHAT_ID set karo!")
        return

    if not IG_USERNAMES:
        logger.error("❌ IG_USERNAMES set karo!")
        return

    seen_ids = load_seen_ids()
    logger.info(f"📂 {len(seen_ids)} seen IDs loaded")

    # Instaloader setup
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )

    # Login karo agar credentials hain
    if IG_USERNAME and IG_PASSWORD:
        try:
            loader.login(IG_USERNAME, IG_PASSWORD)
            logger.info(f"✅ Instagram login: @{IG_USERNAME}")
        except Exception as e:
            logger.warning(f"⚠️ Login failed: {e} — bina login ke try kar raha hoon")
    else:
        logger.info("ℹ️ Bina login ke chal raha hoon (sirf public accounts)")

    new_total   = 0
    checked     = 0
    error_count = 0

    for username in IG_USERNAMES:
        logger.info(f"🔍 Checking @{username}...")
        count, success = process_account(loader, username, seen_ids)
        new_total += count
        if success:
            checked += 1
        else:
            error_count += 1
        import time
        time.sleep(3)  # Instagram rate limit avoid

    save_seen_ids(seen_ids)
    logger.info(f"✅ Done! {new_total} naye posts. {len(seen_ids)} total seen.")
    send_status(new_total, checked, error_count)

if __name__ == "__main__":
    main()
