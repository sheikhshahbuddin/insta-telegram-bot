#!/usr/bin/env python3

import os
import re
import logging
import tempfile
import uuid
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

# ───────── TOKEN (use env var, never hardcode) ─────────
BOT_TOKEN = 8618407509:AAGf2IZ7ubM3wnL9SaccFDMl4_UrfVYf7Nk

# ───────── CONFIG ─────────
MAX_SIZE_MB    = 50       # Max file size in megabytes
MAX_URLS       = 5        # Max URLs per message (DoS protection)
MAX_WORKERS    = 5        # Thread pool size
RATE_LIMIT_SEC = 10       # Seconds between requests per user
DOWNLOAD_DIR   = Path(tempfile.gettempdir()) / "insta_bot"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# ───────── LOGGING ─────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ───────── URL REGEX ─────────
INSTAGRAM_URL_RE = re.compile(
    r'https?://(?:www\.)?instagram\.com/(?:reel|reels|p|tv|stories)/[A-Za-z0-9._-]+/?'
)

# ───────── THREAD POOL ─────────
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ───────── CLEANUP ─────────
def cleanup(_=None):
    """Delete temp files older than 1 hour."""
    now = time.time()
    deleted = 0
    for f in DOWNLOAD_DIR.glob("*"):
        try:
            if now - f.stat().st_mtime > 3600:
                f.unlink()
                deleted += 1
        except Exception as e:
            logger.warning("Cleanup error for %s: %s", f, e)
    if deleted:
        logger.info("Cleanup: removed %d old file(s)", deleted)

# ───────── DOWNLOAD ─────────
def download_video(url: str, path: str):
    """
    Download video from URL to path.
    Returns (actual_filepath, size_mb) or (None, None) on failure.
    yt-dlp may append an extension, so we resolve the real path via prepare_filename.
    """
    try:
        ydl_opts = {
            "format": "best[ext=mp4]/best",
            "outtmpl": path,
            "quiet": True,
            "noplaylist": True,
            "retries": 5,
            "socket_timeout": 30,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # Resolve the actual filename yt-dlp used
            real_path = ydl.prepare_filename(info)

        # yt-dlp sometimes produces a slightly different extension
        # Try the resolved name first, then fall back to original
        for candidate in [real_path, path]:
            if candidate and os.path.exists(candidate):
                size_mb = os.path.getsize(candidate) / (1024 * 1024)
                return candidate, size_mb

        logger.error("Downloaded file not found at %s or %s", real_path, path)

    except yt_dlp.utils.DownloadError as e:
        logger.error("yt-dlp DownloadError for %s: %s", url, e)
    except Exception as e:
        logger.error("Unexpected download error for %s: %s", url, e)

    return None, None

# ───────── RATE LIMIT HELPER ─────────
def is_rate_limited(context, user_id: int) -> bool:
    """Return True if this user sent a request too recently."""
    last = context.user_data.get("last_request", 0)
    now = time.time()
    if now - last < RATE_LIMIT_SEC:
        return True
    context.user_data["last_request"] = now
    return False

# ───────── HANDLERS ─────────
def start(update: Update, context):
    update.message.reply_text(
        "🚀 *Instagram Downloader Bot*\n\n"
        "Send me Instagram links (reels, posts, stories) and I'll download them for you.\n\n"
        f"⚠️ Max {MAX_URLS} links per message | Max {MAX_SIZE_MB}MB per file",
        parse_mode="Markdown"
    )

def help_cmd(update: Update, context):
    update.message.reply_text(
        "📖 *How to use:*\n"
        "Just paste one or more Instagram URLs in a message.\n\n"
        "*Supported:*\n"
        "• Reels\n• Posts\n• IGTV\n• Stories\n\n"
        f"Limit: {MAX_URLS} links per message, {MAX_SIZE_MB}MB per file.",
        parse_mode="Markdown"
    )

def handle(update: Update, context):
    user_id = update.effective_user.id

    # ── Rate limit check ──
    if is_rate_limited(context, user_id):
        update.message.reply_text(
            f"⏳ Slow down! Wait {RATE_LIMIT_SEC}s between requests."
        )
        return

    text = update.message.text or ""
    urls = INSTAGRAM_URL_RE.findall(text)

    if not urls:
        update.message.reply_text("❌ No valid Instagram links found.")
        return

    # Cap to prevent abuse
    if len(urls) > MAX_URLS:
        update.message.reply_text(
            f"⚠️ Too many links. Processing first {MAX_URLS} only."
        )
        urls = urls[:MAX_URLS]

    msg = update.message.reply_text(f"📥 Starting download for {len(urls)} link(s)...")
    success = 0

    for i, url in enumerate(urls, 1):
        try:
            msg.edit_text(f"⏳ Downloading {i}/{len(urls)}...")
        except Exception:
            pass  # Non-fatal if edit fails

        path = str(DOWNLOAD_DIR / f"{uuid.uuid4().hex}.mp4")
        file, size_mb = download_video(url, path)

        try:
            if file is None or size_mb is None:
                update.message.reply_text(f"❌ Download failed:\n{url}")
                continue

            if size_mb > MAX_SIZE_MB:
                update.message.reply_text(
                    f"❌ File too large ({size_mb:.1f}MB > {MAX_SIZE_MB}MB):\n{url}"
                )
                continue

            # Send the file
            with open(file, "rb") as f:
                try:
                    update.message.reply_video(
                        f,
                        caption=f"✅ [{i}/{len(urls)}] {url[:60]}..."
                    )
                except Exception:
                    f.seek(0)
                    update.message.reply_document(
                        f,
                        caption=f"✅ [{i}/{len(urls)}] Sent as file (too large for video player)"
                    )
            success += 1

        except Exception as e:
            logger.error("Send error for %s: %s", url, e)
            update.message.reply_text(f"❌ Failed to send: {url}")

        finally:
            # Always delete temp file regardless of outcome
            if file and os.path.exists(file):
                try:
                    os.remove(file)
                except Exception as e:
                    logger.warning("Could not delete temp file %s: %s", file, e)

    try:
        msg.edit_text(f"✅ Done! {success}/{len(urls)} downloaded successfully.")
    except Exception:
        pass

def error_handler(update, context):
    logger.error("Unhandled error: %s", context.error, exc_info=context.error)
    try:
        if update and update.message:
            update.message.reply_text("⚠️ An unexpected error occurred. Please try again.")
    except Exception:
        pass

# ───────── MAIN ─────────
def main():
    cleanup()  # Clean up old files on startup

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Schedule periodic cleanup every hour
    updater.job_queue.run_repeating(cleanup, interval=3600, first=3600)

    # Handlers
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle))

    # Global error handler
    dp.add_error_handler(error_handler)

    logger.info("Bot started successfully.")
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
      
