import os, io, asyncio, tempfile, time, shutil, traceback, threading, multiprocessing
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

from dotenv import load_dotenv
load_dotenv()  # Load .env file

from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeFilename

# Files/Media libs
from PIL import Image
from PyPDF2 import PdfMerger, PdfReader, PdfWriter
from pydub import AudioSegment
import moviepy.editor as mp
import zipfile

# Keep-alive web
from flask import Flask
import aiohttp

# DB helpers (same style as your previous bots)
from db import init_db, add_user, get_all_users

# ---------------- CONFIG ----------------
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "FileUtilityBot")
RENDER_URL = os.environ.get("RENDER_URL", "https://your-app.onrender.com")  # change to your Render URL

# init DB
init_db()

# Telethon client
client = TelegramClient('file_utility_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ---------------- STATE (per chat+user FSM) ----------------
@dataclass
class Session:
    step: str = "idle"                 # idle | main_menu | convert_menu | compress_menu | pdf_menu | zip_menu | collect_pdfs | collect_zip | await_split_ranges
    last_file_path: Optional[str] = None
    last_file_name: Optional[str] = None
    collected_paths: List[str] = field(default_factory=list)  # for merge zip, etc.
    created_at: float = field(default_factory=time.time)

# Use (chat_id, user_id) as key so group chats don't mix states between users.
SESSIONS: Dict[Tuple[int, int], Session] = {}


def _key(event) -> Tuple[int, int]:
    return (event.chat_id, event.sender_id)


def ses(event) -> Session:
    key = _key(event)
    s = SESSIONS.get(key)
    if not s:
        s = Session()
        SESSIONS[key] = s
    return s


def reset_session(event):
    key = _key(event)
    s = SESSIONS.get(key)
    if not s:
        SESSIONS[key] = Session()
        return
    # cleanup temp files
    try:
        if s.last_file_path and os.path.exists(s.last_file_path):
            os.remove(s.last_file_path)
        for p in s.collected_paths:
            if p and os.path.exists(p):
                os.remove(p)
    except Exception:
        pass
    SESSIONS[key] = Session()

# ---------------- UTIL ----------------
TMP_ROOT = tempfile.gettempdir()


def ffmpeg_available() -> bool:
    # moviepy and pydub both rely on ffmpeg; make the error explicit if missing
    return shutil.which("ffmpeg") is not None


async def download_to_tmp(event) -> tuple[str, str]:
    """Download incoming media to a temp file and return (path, filename)."""
    msg = event.message
    name = None
    if msg.file and msg.file.name:
        name = msg.file.name
    elif msg.file:
        for a in msg.file.attributes or []:
            if isinstance(a, DocumentAttributeFilename):
                name = a.file_name
                break
    if not name:
        name = "file"

    suffix = ""
    if "." in name:
        suffix = "." + name.split(".")[-1].lower()

    fd, path = tempfile.mkstemp(prefix="tg_", suffix=suffix, dir=TMP_ROOT)
    os.close(fd)
    await event.client.download_media(msg, file=path)
    return path, name


def safe_out_path(ext: str, base: str = "output") -> str:
    fd, path = tempfile.mkstemp(prefix="tg_out_", suffix=f".{ext}", dir=TMP_ROOT)
    os.close(fd)
    return path


async def send_doc(event, path: str, name: Optional[str] = None, caption: Optional[str] = None):
    if not name:
        name = os.path.basename(path)
    await client.send_file(event.chat_id, path, caption=caption or "", force_document=True, file_name=name)


def human_err(e: Exception) -> str:
    return f"❌ Error: {str(e) or type(e).__name__}"

# ---------------- MENUS (text-only, no buttons) ----------------
MAIN_MENU = (
    "📂 **File Utility Bot**\n"
    "Send **1/2/3/4** as text to choose.\n\n"
    "1) 🔄 Convert\n"
    "2) 📉 Compress\n"
    "3) 📑 PDF Tools\n"
    "4) 📦 Zip / Unzip\n\n"
    "Commands: /cancel (reset), /help"
)

CONVERT_MENU = (
    "🔄 **Convert** — Send a number:\n"
    "1) Image ⇢ PNG\n"
    "2) Image ⇢ JPG\n"
    "3) Image(s) ⇢ PDF\n"
    "4) Audio ⇢ MP3\n"
    "5) Audio ⇢ WAV\n"
    "6) Video ⇢ MP4\n"
    "7) Video ⇢ GIF\n"
    "8) Back"
)

COMPRESS_MENU = (
    "📉 **Compress** — Send a number:\n"
    "1) Image compress (quality ~70)\n"
    "2) Video compress (lower bitrate)\n"
    "3) PDF (re-save/linearize*)\n"
    "4) Back\n\n"
    "_*True PDF compression needs external tools; this will re-save and may reduce size modestly._"
)

PDF_MENU = (
    "📑 **PDF Tools** — Send a number:\n"
    "1) Merge PDFs (send multiple PDFs then type: done)\n"
    "2) Split PDF (ranges e.g. 1-3,5,7)\n"
    "3) Extract Text\n"
    "4) Back"
)

ZIP_MENU = (
    "📦 **Zip/Unzip** — Send a number:\n"
    "1) Create ZIP (send multiple files then type: done)\n"
    "2) Extract ZIP\n"
    "3) Back"
)

HELP_TEXT = (
    "📝 **How to use (no buttons, only text):**\n"
    "• Bas koi file bhejo → main menu aayega.\n"
    "• 1/2/3/4 likho to category select hoti hai.\n"
    "• /cancel se reset.\n"
    "• Merge/Zip create ke liye multiple files bhejo aur 'done' type karo.\n"
    "• Split ke liye page ranges: 1-3,5,9\n"
)

# ---------------- COMMANDS ----------------
@client.on(events.NewMessage(pattern=fr"^/start(@{BOT_USERNAME})?$"))
async def start_cmd(event):
    add_user(event.sender_id)
    s = ses(event)
    s.step = "idle"  # reset to a known state but don't wipe files here
    await event.respond(
        "👋 **Welcome to File Utility Bot** (text menu, no buttons!)\n\n"
        "Just send a file. I support convert/compress/pdf/zip tools.\n\n"
        + MAIN_MENU
    )


@client.on(events.NewMessage(pattern=r"^/help$"))
async def help_cmd(event):
    await event.respond(HELP_TEXT)


@client.on(events.NewMessage(pattern=r"^/cancel$"))
async def cancel_cmd(event):
    reset_session(event)
    await event.respond("✅ Session cleared.\n" + MAIN_MENU)


# Owner-only broadcast
@client.on(events.NewMessage(pattern=r"^/broadcast"))
async def broadcast_cmd(event):
    if event.sender_id != OWNER_ID:
        return await event.respond("❌ Only the bot owner can use this command.")
    args = event.raw_text.split(" ", 1)
    if len(args) < 2 or not args[1].strip():
        return await event.respond("⚠️ Usage: /broadcast <message>")
    msg = args[1].strip()
    users = get_all_users()
    sent = 0
    failed = 0
    for uid in users:
        try:
            await client.send_message(uid, msg)
            sent += 1
            await asyncio.sleep(0.1)
        except Exception:
            failed += 1
    await event.respond(f"✅ Broadcast done.\n📨 Sent: {sent}\n❌ Failed: {failed}")


# ---------------- FILE ENTRY ----------------
@client.on(events.NewMessage(func=lambda e: e.file))
async def on_file_unified(event):
    s = ses(event)

    # If in collection mode, add to collection
    if s.step in ("collect_pdfs", "collect_zip"):
        path, name = await download_to_tmp(event)
        s.collected_paths.append(path)
        await event.respond(f"➕ Added: **{name}**\nSend more or type **done**.")
        return

    # Otherwise, start fresh for this chat+user
    reset_session(event)
    s = ses(event)  # new session object
    path, name = await download_to_tmp(event)
    s.last_file_path = path
    s.last_file_name = name
    s.step = "main_menu"
    await event.respond(f"✅ Received **{name}**\n\n" + MAIN_MENU)


# ---------------- TEXT MENU HANDLER ----------------
@client.on(events.NewMessage(func=lambda e: not e.file))
async def on_text(event):
    text = (event.raw_text or "").strip()

    # Ignore commands here so we don't double-reply (fixes the duplicate \"First send a file\" after /help & /cancel)
    if text.startswith('/'):
        return

    s = ses(event)
    low = text.lower()

    # Global: 'done' for collections
    if low == "done":
        if s.step == "collect_pdfs":
            return await do_merge_pdfs(event, s)
        if s.step == "collect_zip":
            return await do_zip_create(event, s)

    # While collecting, only accept files or 'done'
    if s.step in ("collect_pdfs", "collect_zip"):
        if low in ("/cancel", "cancel", "back", "4", "3"):
            reset_session(event)
            return await event.respond("❎ Cancelled.\n" + MAIN_MENU)
        return await event.respond("↪️ Send more files (PDFs for merge / any files for zip), or type **done**.\nType /cancel to abort.")

    # Awaiting split ranges input
    if s.step == "await_split_ranges":
        try:
            await event.respond("⏳ Splitting...")
            out = await asyncio.to_thread(split_pdf_by_ranges, s, text)
            for p, n in out:
                await send_doc(event, p, n)
            s.step = "pdf_menu"
            return await event.respond("✅ Done.\n" + PDF_MENU)
        except Exception as e:
            return await event.respond(human_err(e) + "\nTry again or /cancel.")

    # Normal menu routing
    if s.step == "idle":
        return await event.respond("📥 First send a file, then choose options.\n" + MAIN_MENU)

    if s.step == "main_menu":
        if low == "1":
            s.step = "convert_menu"
            return await event.respond(CONVERT_MENU)
        elif low == "2":
            s.step = "compress_menu"
            return await event.respond(COMPRESS_MENU)
        elif low == "3":
            s.step = "pdf_menu"
            return await event.respond(PDF_MENU)
        elif low == "4":
            s.step = "zip_menu"
            return await event.respond(ZIP_MENU)
        else:
            return await event.respond("❓ Send 1/2/3/4.\n" + MAIN_MENU)

    if s.step == "convert_menu":
        if low == "1":   # image -> PNG
            return await run_wrapper(event, convert_image, s, "PNG")
        if low == "2":   # image -> JPG
            return await run_wrapper(event, convert_image, s, "JPEG")
        if low == "3":   # images -> PDF (single image ok)
            return await run_wrapper(event, images_to_pdf, s)
        if low == "4":   # audio -> mp3
            return await run_wrapper(event, convert_audio, s, "mp3")
        if low == "5":   # audio -> wav
            return await run_wrapper(event, convert_audio, s, "wav")
        if low == "6":   # video -> mp4
            return await run_wrapper(event, convert_video, s, "mp4")
        if low == "7":   # video -> gif
            return await run_wrapper(event, video_to_gif, s)
        if low == "8" or low == "back":
            s.step = "main_menu"
            return await event.respond(MAIN_MENU)
        return await event.respond("❓ Send 1-8.")

    if s.step == "compress_menu":
        if low == "1":
            return await run_wrapper(event, compress_image, s, quality=70)
        if low == "2":
            return await run_wrapper(event, compress_video, s)
        if low == "3":
            return await run_wrapper(event, resave_pdf_maybe_smaller, s)
        if low == "4" or low == "back":
            s.step = "main_menu"
            return await event.respond(MAIN_MENU)
        return await event.respond("❓ Send 1-4.")

    if s.step == "pdf_menu":
        if low == "1":
            s.step = "collect_pdfs"
            s.collected_paths = []
            return await event.respond("📥 Send multiple **PDF files** (2 or more). Type **done** when finished. Use /cancel to abort.")
        if low == "2":
            if not s.last_file_path or not _is_pdf(s.last_file_path):
                return await event.respond("Send a PDF first (then choose Split).")
            s.step = "await_split_ranges"
            return await event.respond("✂️ Send page ranges, e.g. `1-3,5,7` (1-indexed).")
        if low == "3":
            return await run_wrapper(event, extract_pdf_text, s)
        if low == "4" or low == "back":
            s.step = "main_menu"
            return await event.respond(MAIN_MENU)
        return await event.respond("❓ Send 1-4.")

    if s.step == "zip_menu":
        if low == "1":
            s.step = "collect_zip"
            s.collected_paths = []
            return await event.respond("📥 Send files to include in ZIP. Type **done** to build the archive. /cancel to abort.")
        if low == "2":
            return await run_wrapper(event, unzip_archive, s)
        if low == "3" or low == "back":
            s.step = "main_menu"
            return await event.respond(MAIN_MENU)
        return await event.respond("❓ Send 1-3.")


# ---------------- RUN WRAPPER ----------------
async def run_wrapper(event, func, s: Session, *args, **kwargs):
    try:
        await event.respond("⏳ Working...")
        out = await asyncio.to_thread(func, s, *args, **kwargs)
        if isinstance(out, list):
            for p, n in out:
                await send_doc(event, p, n)
        elif isinstance(out, tuple):
            p, n = out
            await send_doc(event, p, n)
        elif isinstance(out, str):
            await event.respond(out)
        else:
            await event.respond("✅ Done.")
    except Exception as e:
        traceback.print_exc()
        await event.respond(human_err(e) + "\nTry /cancel and re-start.")


# ---------------- FEATURE IMPLEMENTATIONS ----------------
# Helpers

def _ensure_image(path: str) -> Image.Image:
    img = Image.open(path)
    img.load()
    return img


def _is_pdf(path: str) -> bool:
    return path.lower().endswith(".pdf")


def _is_zip(path: str) -> bool:
    return path.lower().endswith(".zip")


# Convert: Image -> PNG/JPG

def convert_image(s: Session, target_fmt: str):
    if not s.last_file_path:
        return "Send an image first."
    img = _ensure_image(s.last_file_path)
    if target_fmt.upper() in ("JPEG", "JPG"):
        img = img.convert("RGB")
    ext = "jpg" if target_fmt.upper() in ("JPEG", "JPG") else "png"
    out = safe_out_path(ext, "image")
    if target_fmt.upper() in ("JPEG", "JPG"):
        img.save(out, "JPEG", quality=95, optimize=True)
    else:
        img.save(out, "PNG", optimize=True)
    return out, f"converted.{ext}"


# Convert: Images -> PDF (works with single image too)

def images_to_pdf(s: Session):
    paths = s.collected_paths[:] if s.collected_paths else ([s.last_file_path] if s.last_file_path else [])
    if not paths:
        return "Send image(s) first."

    images = []
    for p in paths:
        img = _ensure_image(p).convert("RGB")
        images.append(img)
    out = safe_out_path("pdf", "images")
    if len(images) == 1:
        images[0].save(out, "PDF")
    else:
        first, rest = images[0], images[1:]
        first.save(out, "PDF", save_all=True, append_images=rest)
    return out, "images.pdf"


# Convert: Audio -> target (requires ffmpeg)

def convert_audio(s: Session, target: str):
    if not s.last_file_path:
        return "Send audio first."
    if not ffmpeg_available():
        return "❌ ffmpeg not found on server. Install ffmpeg for audio/video features."
    src = s.last_file_path
    out = safe_out_path(target)
    audio = AudioSegment.from_file(src)
    audio.export(out, format=target)
    return out, f"audio.{target}"


# Convert: Video -> MP4 (requires ffmpeg)

def convert_video(s: Session, target: str = "mp4"):
    if not s.last_file_path:
        return "Send video first."
    if not ffmpeg_available():
        return "❌ ffmpeg not found on server. Install ffmpeg for audio/video features."
    src = s.last_file_path
    out = safe_out_path(target)
    clip = mp.VideoFileClip(src)
    clip.write_videofile(out, codec="libx264", audio_codec="aac", verbose=False, logger=None)
    clip.close()
    return out, f"video.{target}"


# Convert: Video -> GIF (requires ffmpeg)

def video_to_gif(s: Session):
    if not s.last_file_path:
        return "Send video first."
    if not ffmpeg_available():
        return "❌ ffmpeg not found on server. Install ffmpeg for audio/video features."
    src = s.last_file_path
    out = safe_out_path("gif")
    clip = mp.VideoFileClip(src)
    clip.write_gif(out, program="ffmpeg", logger=None)
    clip.close()
    return out, "video.gif"


# Compress: Image (JPEG quality)

def compress_image(s: Session, quality: int = 70):
    if not s.last_file_path:
        return "Send image first."
    img = _ensure_image(s.last_file_path).convert("RGB")
    out = safe_out_path("jpg")
    img.save(out, "JPEG", quality=quality, optimize=True)
    return out, "compressed.jpg"


# Compress: Video (reduce bitrate / size) (requires ffmpeg)

def compress_video(s: Session):
    if not s.last_file_path:
        return "Send video first."
    if not ffmpeg_available():
        return "❌ ffmpeg not found on server. Install ffmpeg for audio/video features."
    src = s.last_file_path
    out = safe_out_path("mp4")
    clip = mp.VideoFileClip(src)
    width, height = clip.w, clip.h
    if max(width, height) > 1080:
        new_w = int(width * 1080 / max(width, height))
        new_h = int(height * 1080 / max(width, height))
        clip = clip.resize(newsize=(new_w, new_h))
    clip.write_videofile(out, codec="libx264", audio_codec="aac", bitrate="1200k", verbose=False, logger=None)
    clip.close()
    return out, "compressed.mp4"


# Compress: PDF (re-save / small chance of smaller size)

def resave_pdf_maybe_smaller(s: Session):
    if not s.last_file_path or not _is_pdf(s.last_file_path):
        return "Send a PDF first."
    reader = PdfReader(s.last_file_path)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.add_metadata({})
    out = safe_out_path("pdf")
    with open(out, "wb") as f:
        writer.write(f)
    return out, "resaved.pdf"


# PDF: Merge (expects collect_pdfs + done)
async def do_merge_pdfs(event, s: Session):
    if len(s.collected_paths) < 2:
        return await event.respond("Need at least 2 PDFs. Keep sending or /cancel.")
    merger = PdfMerger()
    for p in s.collected_paths:
        if not _is_pdf(p):
            return await event.respond("All files must be PDF. /cancel and retry.")
        merger.append(p)
    out = safe_out_path("pdf")
    with open(out, "wb") as f:
        merger.write(f)
    merger.close()
    await send_doc(event, out, "merged.pdf", "✅ Merged PDF")
    reset_session(event)
    await event.respond(MAIN_MENU)


# PDF: Split by ranges

def split_pdf_by_ranges(s: Session, ranges_str: str):
    if not s.last_file_path or not _is_pdf(s.last_file_path):
        raise RuntimeError("Send PDF first.")
    reader = PdfReader(s.last_file_path)
    total = len(reader.pages)

    wanted: List[int] = []
    parts = [p.strip() for p in ranges_str.split(",") if p.strip()]
    for part in parts:
        if "-" in part:
            a, b = part.split("-", 1)
            a = int(a); b = int(b)
            if a < 1 or b < a:
                raise ValueError("Invalid range.")
            for i in range(a, b + 1):
                if 1 <= i <= total:
                    wanted.append(i)
        else:
            i = int(part)
            if 1 <= i <= total:
                wanted.append(i)
    if not wanted:
        raise ValueError("No valid pages.")

    writer = PdfWriter()
    for i in wanted:
        writer.add_page(reader.pages[i - 1])
    out = safe_out_path("pdf")
    with open(out, "wb") as f:
        writer.write(f)
    return [(out, "split.pdf")]


# PDF: Extract text

def extract_pdf_text(s: Session):
    if not s.last_file_path or not _is_pdf(s.last_file_path):
        return "Send a PDF first."
    reader = PdfReader(s.last_file_path)
    texts = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")
    text = "\n\n".join(texts).strip()
    if not text:
        return "No extractable text (maybe scanned images)."
    out = safe_out_path("txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    return out, "extracted.txt"


# ZIP: Create from collected files
async def do_zip_create(event, s: Session):
    files = s.collected_paths[:] or ([s.last_file_path] if s.last_file_path else [])
    if not files:
        return await event.respond("Send files first.")
    out = safe_out_path("zip")
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            arcname = os.path.basename(p)
            zf.write(p, arcname)
    await send_doc(event, out, "archive.zip", "✅ ZIP created")
    reset_session(event)
    await event.respond(MAIN_MENU)


# ZIP: Extract

def unzip_archive(s: Session):
    if not s.last_file_path or not _is_zip(s.last_file_path):
        return "Send a .zip file first."
    out_dir = tempfile.mkdtemp(prefix="unz_", dir=TMP_ROOT)
    out_files = []
    with zipfile.ZipFile(s.last_file_path, "r") as zf:
        names = zf.namelist()[:20]  # avoid spamming too many files
        for nm in names:
            if nm.endswith("/"):
                continue
            dest = os.path.join(out_dir, os.path.basename(nm))
            with zf.open(nm) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out_files.append((dest, os.path.basename(dest)))
    if not out_files:
        return "Archive empty."
    return out_files


# ---------------- FLASK KEEP-ALIVE + MAIN ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "File Utility Bot is running!"

def run_web():
    app.run(host="0.0.0.0", port=10000)

async def keep_alive():
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                await session.get(RENDER_URL)
                print("🌍 Keep-alive ping sent.")
        except Exception as e:
            print("⚠️ Keep-alive failed:", e)
        await asyncio.sleep(300)

if __name__ == "__main__":
    multiprocessing.Process(target=run_web, daemon=True).start()
    client.loop.create_task(keep_alive())
    print("🤖 Connecting bot...")
    client.run_until_disconnected()