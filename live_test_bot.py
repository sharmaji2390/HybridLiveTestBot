import os
import re
import json
import sqlite3
import asyncio
import unicodedata
from datetime import datetime
from io import BytesIO

from pyrogram import Client, filters, raw, idle
import pyrogram.utils as pyrogram_utils
from pyrogram.enums import PollType, ChatType, ChatMemberStatus
from pyrogram.errors import FloodWait

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether, PageBreak, Image as RLImage, Flowable
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# =========================================================
# TRUE DEVANAGARI SHAPING
# =========================================================
# ReportLab can embed a Devanagari TTF, but drawing Unicode codepoints
# directly does NOT perform Indic GSUB/GPOS shaping on all Android builds.
# HarfBuzz performs the real Devanagari shaping; glyph outlines are then
# drawn as vectors through ReportLab, so no Pillow/RAQM is involved.
try:
    import uharfbuzz as hb
except Exception as _hb_error:
    raise RuntimeError(
        "True Hindi PDF shaping ke liye uharfbuzz required hai.\n"
        "Pydroid me ek baar run karein:\n"
        "pip install uharfbuzz fonttools\n\n"
        f"Original error: {_hb_error}"
    )

from fontTools.ttLib import TTFont as FTFont
from fontTools.pens.basePen import BasePen
from reportlab.pdfgen import canvas as pdf_canvas
# PDF merger: pypdf is preferred, PyPDF2 is supported on older Android/Pydroid.
try:
    from pypdf import PdfReader, PdfWriter
except ModuleNotFoundError:
    try:
        from PyPDF2 import PdfReader, PdfWriter
    except ModuleNotFoundError as _pdf_merge_error:
        raise RuntimeError(
            "PDF merge library missing. Pydroid me ek baar run karein:\n"
            "pip install PyPDF2\n\n"
            f"Original error: {_pdf_merge_error}"
        )

try:
    from PIL import Image as PILImage
    from PIL import ImageDraw, ImageFont, features
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False


# =========================================================
# STEP 10 FINAL v14 - USERBOT + CONSOLIDATED RESULT + TRUE DEVANAGARI ANSWER KEY
# =========================================================
# NOTE:
# 1) API_ID/API_HASH below are placeholders from the code you
#    supplied. Keep your own Telegram API credentials.
# 2) This is USERBOT mode: no BOT_TOKEN is required.
# 3) First run creates the persistent Pyrogram session.
# 4) Subsequent runs reuse the same session file.
# =========================================================


# =========================================================
# PYROGRAM PEER-ID COMPATIBILITY FIX
# =========================================================

def _fixed_get_peer_type(peer_id: int) -> str:
    peer_id = int(peer_id)
    text = str(peer_id)

    if text.startswith("-100"):
        return "channel"
    if peer_id < 0:
        return "chat"
    if peer_id > 0:
        return "user"
    raise ValueError(f"Peer id invalid: {peer_id}")


pyrogram_utils.get_peer_type = _fixed_get_peer_type


# =========================================================
# TELEGRAM SETTINGS
# =========================================================

# =========================================================
# TELEGRAM BOT SETTINGS
# =========================================================
# This version uses a real Telegram BOT account, not a user session.
# Set these in Pterodactyl Environment Variables:
#   BOT_TOKEN = token from @BotFather
#   API_ID    = Telegram API ID
#   API_HASH  = Telegram API HASH
#
# Do NOT put the bot token in this source file.
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

SESSION_NAME = "live_test_bot"
WORKDIR = "/home/container"

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN environment variable missing. "
        "Create a bot with @BotFather and set BOT_TOKEN in Pterodactyl."
    )

if not API_ID or not API_HASH:
    raise RuntimeError(
        "API_ID/API_HASH environment variables missing. "
        "Set both in Pterodactyl."
    )

app = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workdir=WORKDIR
)

SELF_USER_ID = 0

# =========================================================
# SETTINGS
# =========================================================

DB_FILE = "/home/container/question_bank.db"
PDF_FOLDER = "/home/container/LiveTestPDF"
FONT_DIR = "/home/container/fonts"

os.makedirs(PDF_FOLDER, exist_ok=True)
os.makedirs(FONT_DIR, exist_ok=True)

QUESTION_TIME = 30
NEGATIVE_MARK = 0.33
MAX_QUESTIONS = 200
IMPORT_SCAN_LIMIT = 2000

# =========================================================
# TEST PLAY PERMISSION
# =========================================================
# Sirf in Telegram USER IDs ko /test start karne ki permission hogi.
# User ID + group ADMIN/OWNER dono required hain.
#
# Example:
# Sirf in 3 Telegram USER IDs ko /test start karne ki permission hogi.
# IMPORTANT: Ye GROUP CHAT IDs nahi hain; ye admins ke PERSONAL Telegram USER IDs hain.
# Saath hi, /test chalane wale ko us group ka ADMIN/OWNER hona zaroori hai.
# Any Telegram group ADMIN/OWNER can use /import, /test and /stoptest.
# Normal members cannot control the test.
TEST_PLAY_ALLOWED_USER_IDS = set()

# HYBRID SOURCE READER
# A separate user-session process reads source chats and sends imported
# question JSON to this real Bot account. Only these user IDs may submit
# hybrid import packages.
SOURCE_READER_USER_IDS = {
    int(x.strip()) for x in os.getenv("SOURCE_READER_USER_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
HYBRID_MODE = os.getenv("HYBRID_MODE", "1").strip() == "1"

POLL_CLOSE_GRACE = 2.0
PENDING_RETRY_DELAY = 0.5
PENDING_MAX_RETRIES = 20


# =========================================================
# HINDI FONT
# =========================================================

FONT_DOWNLOAD_PATH = "/home/container/fonts/NotoSansDevanagari-Regular.ttf"

# Static, non-variable Noto Sans Devanagari.  If the user has not copied the
# font manually, V16 downloads the static TTF automatically.  This avoids the
# Android NotoSansDevanagari-VF.ttf -> glyph-outline mismatch that produced □.
FONT_PATHS = [
    FONT_DOWNLOAD_PATH,
    "/home/container/fonts/NotoSansDevanagari-Regular.ttf",
    "/home/container/fonts/NotoSansDevanagari-Regular.ttf",
    "/system/fonts/NotoSansDevanagari-Regular.ttf",
]

STATIC_FONT_URLS = [
    # Google/Noto static hinted TTF.
    "https://raw.githubusercontent.com/googlefonts/noto-fonts/main/"
    "hinted/ttf/NotoSansDevanagari/NotoSansDevanagari-Regular.ttf",
    # Stable Android Noto source mirror.
    "https://android.googlesource.com/platform/external/noto-fonts/"
    "+/refs/tags/android-cts-5.1_r13/NotoSansDevanagari-Regular.ttf"
    "?format=TEXT",
]

def _font_is_static_and_has_devanagari_coverage(path):
    try:
        ft = FTFont(path, lazy=False)
        # Variable fonts contain an fvar table. Reject them.
        if "fvar" in ft:
            return False
        cmap = ft.getBestCmap() or {}
        required = [0x0926, 0x094B, 0x0928, 0x0902, 0x0930, 0x093E]
        return all(cp in cmap for cp in required)
    except Exception:
        return False


def _download_static_devanagari_font():
    """Download a static TTF once if it is not already installed."""
    if os.path.exists(FONT_DOWNLOAD_PATH):
        if _font_is_static_and_has_devanagari_coverage(FONT_DOWNLOAD_PATH):
            return FONT_DOWNLOAD_PATH

    try:
        import urllib.request
        import base64

        os.makedirs(os.path.dirname(FONT_DOWNLOAD_PATH), exist_ok=True)
        last_error = None

        for url in STATIC_FONT_URLS:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "LiveTestBot/17"}
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()

                # The android.googlesource ?format=TEXT endpoint returns
                # base64; raw GitHub returns binary TTF.
                if data[:16].strip().startswith(b"PD") or b"\n" not in data[:100]:
                    try:
                        decoded = base64.b64decode(data, validate=False)
                        if decoded[:4] in (b"\\x00\\x01\\x00\\x00", b"OTTO"):
                            data = decoded
                    except Exception:
                        pass

                if not data.startswith((b"\\x00\\x01\\x00\\x00", b"OTTO")):
                    # Try base64 decoding once more for the Android endpoint.
                    try:
                        decoded = base64.b64decode(data, validate=False)
                        if decoded.startswith((b"\\x00\\x01\\x00\\x00", b"OTTO")):
                            data = decoded
                    except Exception:
                        pass

                tmp = FONT_DOWNLOAD_PATH + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, FONT_DOWNLOAD_PATH)

                if _font_is_static_and_has_devanagari_coverage(
                    FONT_DOWNLOAD_PATH
                ):
                    print(
                        "⬇️ STATIC DEVANAGARI FONT DOWNLOADED | "
                        f"{FONT_DOWNLOAD_PATH}"
                    )
                    return FONT_DOWNLOAD_PATH

                try:
                    os.remove(FONT_DOWNLOAD_PATH)
                except Exception:
                    pass

            except Exception as exc:
                last_error = exc

        print(f"⚠️ Static font auto-download failed: {last_error}")
    except Exception as exc:
        print(f"⚠️ Font downloader unavailable: {exc}")

    return None



# ---------------------------------------------------------
# STATIC LATIN FONT
# ---------------------------------------------------------
# Noto Sans Devanagari is intentionally script-specific; it does NOT
# contain the complete Latin/punctuation set. Using it for English,
# URLs and ASCII punctuation is what caused missing words in the PDF.
LATIN_FONT_DOWNLOAD_PATH = "/home/container/fonts/NotoSans-Regular.ttf"
LATIN_FONT_PATHS = [
    LATIN_FONT_DOWNLOAD_PATH,
    "/home/container/fonts/NotoSans-Regular.ttf",
    "/home/container/fonts/NotoSans-Regular.ttf",
    "/system/fonts/NotoSans-Regular.ttf",
]

LATIN_FONT_URLS = [
    "https://raw.githubusercontent.com/notofonts/noto-fonts/main/"
    "hinted/ttf/NotoSans/NotoSans-Regular.ttf",
    "https://cdn.jsdelivr.net/gh/notofonts/notofonts.github.io/fonts/"
    "NotoSans/hinted/ttf/NotoSans-Regular.ttf",
]

def _font_is_static_and_has_latin_coverage(path):
    try:
        ft = FTFont(path, lazy=False)
        if "fvar" in ft:
            return False
        cmap = ft.getBestCmap() or {}
        required = [ord("A"), ord("a"), ord("0"), ord(" "), ord("."), ord("?")]
        return all(cp in cmap for cp in required)
    except Exception:
        return False

def _download_static_latin_font():
    if os.path.exists(LATIN_FONT_DOWNLOAD_PATH):
        if _font_is_static_and_has_latin_coverage(LATIN_FONT_DOWNLOAD_PATH):
            return LATIN_FONT_DOWNLOAD_PATH
    try:
        import urllib.request
        os.makedirs(os.path.dirname(LATIN_FONT_DOWNLOAD_PATH), exist_ok=True)
        last_error = None
        for url in LATIN_FONT_URLS:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "LiveTestBot/17"}
                )
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                if not data.startswith((b"\x00\x01\x00\x00", b"OTTO")):
                    continue
                tmp = LATIN_FONT_DOWNLOAD_PATH + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, LATIN_FONT_DOWNLOAD_PATH)
                if _font_is_static_and_has_latin_coverage(
                    LATIN_FONT_DOWNLOAD_PATH
                ):
                    print(
                        "⬇️ STATIC LATIN FONT DOWNLOADED | "
                        f"{LATIN_FONT_DOWNLOAD_PATH}"
                    )
                    return LATIN_FONT_DOWNLOAD_PATH
            except Exception as exc:
                last_error = exc
        print(f"⚠️ Static Latin font auto-download failed: {last_error}")
    except Exception as exc:
        print(f"⚠️ Latin font downloader unavailable: {exc}")
    return None

LATIN_FONT_PATH = next(
    (
        p for p in LATIN_FONT_PATHS
        if os.path.exists(p)
        and _font_is_static_and_has_latin_coverage(p)
    ),
    None
)
if LATIN_FONT_PATH is None:
    LATIN_FONT_PATH = _download_static_latin_font()

if LATIN_FONT_PATH is None:
    raise FileNotFoundError(
        "\nSTATIC Noto Sans Latin TTF nahi mila/download nahi ho saka.\n"
        "Internet ON karke program dobara run karein.\n"
    )

pdfmetrics.registerFont(TTFont("LatinFont", LATIN_FONT_PATH))
LATIN_FONT_NAME = "LatinFont"

FONT_PATH = next(
    (
        p for p in FONT_PATHS
        if os.path.exists(p)
        and _font_is_static_and_has_devanagari_coverage(p)
    ),
    None
)

if FONT_PATH is None:
    FONT_PATH = _download_static_devanagari_font()

if FONT_PATH is None:
    raise FileNotFoundError(
        "\nSTATIC Noto Sans Devanagari TTF nahi mila/download nahi ho saka.\n"
        "Internet ON karke program dobara run karein, ya manually put:\n"
        f"{FONT_DOWNLOAD_PATH}\n"
        "\nVariable Noto fonts use na karein; static TTF only.\n"
    )

pdfmetrics.registerFont(TTFont("HindiFont", FONT_PATH))
FONT_NAME = "HindiFont"

print(f"🅰️ DEVANAGARI FONT SELECTED | {FONT_PATH}")
print(f"🔤 LATIN FONT SELECTED | {LATIN_FONT_PATH}")
print("🧩 MIXED HARFBUZZ SHAPING | Deva=Hindi | Latn=English/URLs/digits")
print("🧵 LONG TOKEN WRAP: ENABLED | URLs/IDs are never dropped")
print("📄 PDF PAGE SIZE | A4 PORTRAIT (595 x 842 pt)")
print("🛰 COMMAND FIX v20 | outgoing handler + raw update fallback enabled")

# A bold Devanagari font is optional; the regular font is always required.
BOLD_FONT_PATHS = [
    "/home/container/fonts/NotoSansDevanagari-Bold.ttf",
    "/home/container/fonts/NotoSansDevanagari-Bold.ttf",
    "/home/container/fonts/NotoSansDevanagari-Bold.ttf",
]
BOLD_FONT_PATH = next(
    (
        p for p in BOLD_FONT_PATHS
        if os.path.exists(p)
        and _font_is_static_and_has_devanagari_coverage(p)
    ),
    FONT_PATH,
)


def _pil_clean_text(text):
    """Remove only characters that are unsafe/unhelpful for the Devanagari TTF."""
    if text is None:
        return ""
    s = unicodedata.normalize("NFC", str(text))
    out = []
    for ch in s:
        cp = ord(ch)
        # Drop emoji/supplementary symbols; keep Devanagari, Latin, punctuation,
        # combining marks and ordinary symbols.
        if cp > 0xFFFF:
            out.append(" ")
        else:
            out.append(ch)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _pil_wrap_text(text, font, max_width, draw):
    """Word-wrap text using Pillow's real font metrics."""
    text = _pil_clean_text(text)
    if not text:
        return [""]

    lines = []
    for paragraph in text.split("\n"):
        words = paragraph.split(" ")
        if not words:
            lines.append("")
            continue

        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue

            if current:
                lines.append(current)
                current = ""

            # Break a very long token (e.g. a URL) by character width.
            token = word
            piece = ""
            for ch in token:
                cand = piece + ch
                if piece and draw.textlength(cand, font=font) > max_width:
                    lines.append(piece)
                    piece = ch
                else:
                    piece = cand
            current = piece

        if current:
            lines.append(current)

    return lines or [""]


def _make_answer_key_image(question, options, correct_option, width_px=2200):
    """
    Build one Official Answer Key block.

    Android/Pydroid Pillow wheels commonly have Pillow installed WITHOUT
    libraqm/HarfBuzz.  Therefore RAQM is treated as OPTIONAL here.

    - If RAQM is available: render Hindi through Pillow + RAQM.
    - If RAQM is NOT available: DO NOT abort the whole result PDF.
      Fall back to ReportLab's registered Devanagari TTF.

    This is intentionally fail-safe: the consolidated Result.pdf must still
    be generated even when the Android Pillow build lacks RAQM.
    """

    def clean_text(value):
        if value is None:
            return ""
        s = unicodedata.normalize("NFC", str(value))
        # Remove supplementary-plane emoji which the supplied Noto font
        # normally cannot render.
        s = "".join(ch if ord(ch) <= 0xFFFF else " " for ch in s)
        return s

    # ---------------------------------------------------------
    # Try RAQM first.
    # ---------------------------------------------------------
    has_raqm = False
    raqm_layout = None

    if PIL_AVAILABLE:
        try:
            has_raqm = bool(features.check("raqm"))
        except Exception:
            has_raqm = False

        if has_raqm:
            try:
                raqm_layout = ImageFont.Layout.RAQM
            except Exception:
                has_raqm = False

    if has_raqm:
        try:
            regular = ImageFont.truetype(
                FONT_PATH, 36, layout_engine=raqm_layout
            )
            bold = ImageFont.truetype(
                BOLD_FONT_PATH, 36, layout_engine=raqm_layout
            )

            width_px = max(1200, int(width_px))
            margin_x = 42
            margin_y = 30
            inner_width = width_px - (2 * margin_x)
            line_gap = 11

            measure_img = PILImage.new(
                "RGB", (10, 10), "white"
            )
            measure_draw = ImageDraw.Draw(measure_img)

            def measure(text_value, font):
                bbox = measure_draw.textbbox(
                    (0, 0),
                    text_value or "अ",
                    font=font,
                    direction="ltr",
                    language="hi"
                )
                return bbox[2] - bbox[0], bbox[3] - bbox[1]

            def wrap(text_value, font):
                text_value = clean_text(text_value)
                if not text_value:
                    return [""]

                words = text_value.split(" ")
                lines = []
                current = ""

                for word in words:
                    candidate = (
                        word if not current
                        else current + " " + word
                    )

                    if (
                        measure(candidate, font)[0]
                        <= inner_width
                    ):
                        current = candidate
                        continue

                    if current:
                        lines.append(current)
                        current = ""

                    # Break long tokens (URLs etc.) safely.
                    piece = ""
                    for ch in word:
                        cand = piece + ch
                        if (
                            piece
                            and measure(cand, font)[0]
                            > inner_width
                        ):
                            lines.append(piece)
                            piece = ch
                        else:
                            piece = cand

                    current = piece

                if current:
                    lines.append(current)

                return lines or [""]

            lines = []

            for line in wrap(
                f"Q. {clean_text(question)}",
                bold
            ):
                lines.append(
                    ("question", line, bold)
                )

            for i, option in enumerate(options):
                kind = (
                    "correct"
                    if i == int(correct_option)
                    else "option"
                )

                option_lines = wrap(
                    f"{option_letter(i)}) "
                    f"{clean_text(option)}",
                    regular
                )

                for line in option_lines:
                    lines.append(
                        (kind, line, bold if kind == "correct" else regular)
                    )

            correct_text = ""
            if 0 <= int(correct_option) < len(options):
                correct_text = clean_text(
                    options[int(correct_option)]
                )

            for line in wrap(
                f"Correct Answer: "
                f"{option_letter(correct_option)} — {correct_text}",
                bold
            ):
                lines.append(
                    ("answer", line, bold)
                )

            text_height = 0
            line_sizes = []

            for _, line, font in lines:
                _, h = measure(line, font)
                line_sizes.append(h)
                text_height += h + line_gap

            height_px = (
                margin_y * 2
                + text_height
                + 24
            )

            img = PILImage.new(
                "RGB",
                (width_px, max(180, height_px)),
                "#F7FBFF"
            )
            draw = ImageDraw.Draw(img)

            # Border.
            draw.rectangle(
                (
                    2,
                    2,
                    width_px - 3,
                    max(180, height_px) - 3
                ),
                outline="#B8C9D8",
                width=3
            )

            y = margin_y

            for (kind, line, font), h in zip(
                lines,
                line_sizes
            ):
                if kind in ("correct", "answer"):
                    fill = "#168447"
                elif kind == "question":
                    fill = "#17212B"
                else:
                    fill = "#263238"

                bbox = draw.textbbox(
                    (margin_x, y),
                    line or "अ",
                    font=font,
                    direction="ltr",
                    language="hi"
                )

                draw.text(
                    (
                        margin_x,
                        y - bbox[1]
                    ),
                    line,
                    font=font,
                    fill=fill,
                    direction="ltr",
                    language="hi"
                )

                y += h + line_gap

            bio = BytesIO()
            img.save(
                bio,
                format="PNG",
                optimize=True
            )
            bio.seek(0)

            target_width_pt = 520
            target_height_pt = (
                target_width_pt
                * img.height
                / img.width
            )

            return RLImage(
                bio,
                width=target_width_pt,
                height=target_height_pt
            )

        except Exception as exc:
            # RAQM exists but something went wrong during rendering.
            # Continue to the ReportLab fallback instead of killing the
            # complete result PDF.
            print(
                "⚠️ RAQM answer-key rendering failed; "
                f"using ReportLab fallback: {exc!r}"
            )

    else:
        print(
            "ℹ️ Pillow RAQM/HarfBuzz unavailable on this Android build; "
            "using ReportLab Devanagari fallback."
        )

    # ---------------------------------------------------------
    # ReportLab fallback
    # ---------------------------------------------------------
    # This path requires NO RAQM/HarfBuzz and therefore works with the
    # standard Pydroid Pillow wheel.  The supplied Devanagari TTF is still
    # embedded in the PDF.
    title_style = ParagraphStyle(
        "FallbackAnswerQuestion",
        fontName=FONT_NAME,
        fontSize=13,
        leading=19,
        textColor=colors.HexColor("#17212B"),
        spaceAfter=6
    )

    option_style = ParagraphStyle(
        "FallbackAnswerOption",
        fontName=FONT_NAME,
        fontSize=12,
        leading=18,
        textColor=colors.HexColor("#263238"),
        spaceAfter=3
    )

    correct_style = ParagraphStyle(
        "FallbackAnswerCorrect",
        fontName=FONT_NAME,
        fontSize=12,
        leading=18,
        textColor=colors.HexColor("#168447"),
        spaceAfter=3
    )

    safe_q = pdf_text(clean_text(question))

    content = [
        Paragraph(
            f"<b>Q. {safe_q}</b>",
            title_style
        )
    ]

    for i, option in enumerate(options):
        safe_option = pdf_text(
            clean_text(option)
        )

        if i == int(correct_option):
            content.append(
                Paragraph(
                    f"<b>{option_letter(i)}) "
                    f"{safe_option}</b>",
                    correct_style
                )
            )
        else:
            content.append(
                Paragraph(
                    f"{option_letter(i)}) "
                    f"{safe_option}",
                    option_style
                )
            )

    correct_text = ""
    if 0 <= int(correct_option) < len(options):
        correct_text = pdf_text(
            clean_text(options[int(correct_option)])
        )

    content.append(
        Paragraph(
            f"<b>Correct Answer: "
            f"{option_letter(correct_option)} — "
            f"{correct_text}</b>",
            correct_style
        )
    )

    block = Table(
        [[content]],
        colWidths=[520]
    )

    block.setStyle(
        TableStyle([
            (
                "BACKGROUND",
                (0, 0),
                (-1, -1),
                colors.HexColor("#F7FBFF")
            ),
            (
                "BOX",
                (0, 0),
                (-1, -1),
                0.8,
                colors.HexColor("#B8C9D8")
            ),
            (
                "LEFTPADDING",
                (0, 0),
                (-1, -1),
                12
            ),
            (
                "RIGHTPADDING",
                (0, 0),
                (-1, -1),
                12
            ),
            (
                "TOPPADDING",
                (0, 0),
                (-1, -1),
                10
            ),
            (
                "BOTTOMPADDING",
                (0, 0),
                (-1, -1),
                10
            ),
            (
                "VALIGN",
                (0, 0),
                (-1, -1),
                "TOP"
            )
        ])
    )

    return KeepTogether(block)


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imported_tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_chat TEXT NOT NULL,
            source_start_message INTEGER NOT NULL,
            question_count INTEGER NOT NULL,
            timer_seconds INTEGER NOT NULL,
            negative_mark REAL NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS imported_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            question_no INTEGER NOT NULL,
            question TEXT NOT NULL,
            options_json TEXT NOT NULL,
            correct_option INTEGER NOT NULL,
            source_message_id INTEGER NOT NULL,
            UNIQUE(test_id, question_no),
            FOREIGN KEY(test_id) REFERENCES imported_tests(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS live_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            test_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT NOT NULL DEFAULT '',
            answers_json TEXT NOT NULL,
            correct INTEGER NOT NULL DEFAULT 0,
            wrong INTEGER NOT NULL DEFAULT 0,
            score REAL NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE(chat_id, test_id, user_id)
        )
    """)
    conn.commit()
    return conn


db_connect().close()


def _row_to_test(row):
    return {
        "id": row[0],
        "name": row[1],
        "source_chat": row[2],
        "source_start_message": row[3],
        "question_count": row[4],
        "timer_seconds": row[5],
        "negative_mark": float(NEGATIVE_MARK),
        "created_at": row[7],
        "questions": []
    }


def get_imported_test(test_id):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, source_chat, source_start_message,
               question_count, timer_seconds, negative_mark, created_at
        FROM imported_tests
        WHERE id = ?
    """, (int(test_id),))

    row = cur.fetchone()
    if not row:
        conn.close()
        return None

    test = _row_to_test(row)

    cur.execute("""
        SELECT question_no, question, options_json,
               correct_option, source_message_id
        FROM imported_questions
        WHERE test_id = ?
        ORDER BY question_no
    """, (int(test_id),))

    for q in cur.fetchall():
        test["questions"].append({
            "question_no": q[0],
            "question": q[1],
            "options": json.loads(q[2]),
            "correct": q[3],
            "source_message_id": q[4]
        })

    conn.close()
    return test


def get_latest_imported_test():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, source_chat, source_start_message,
               question_count, timer_seconds, negative_mark, created_at
        FROM imported_tests
        ORDER BY id DESC
        LIMIT 1
    """)

    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return get_imported_test(row[0])


def save_imported_test(name, source_chat, source_start_message,
                       timer_seconds, negative_mark, questions):
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO imported_tests
        (name, source_chat, source_start_message, question_count,
         timer_seconds, negative_mark, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        name,
        str(source_chat),
        int(source_start_message),
        len(questions),
        int(timer_seconds),
        float(negative_mark),
        datetime.now().isoformat(timespec="seconds")
    ))

    test_id = cur.lastrowid

    for no, q in enumerate(questions, start=1):
        cur.execute("""
            INSERT INTO imported_questions
            (test_id, question_no, question, options_json,
             correct_option, source_message_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            test_id,
            no,
            q["question"],
            json.dumps(q["options"], ensure_ascii=False),
            int(q.get("correct", -1)),
            int(q["source_message_id"])
        ))

    conn.commit()
    conn.close()
    return test_id



# =========================================================
# RESULT PERSISTENCE
# =========================================================

def _serialize_answers(answers):
    return json.dumps(
        answers,
        ensure_ascii=False,
        separators=(",", ":")
    )


def persist_result(chat_id, user_id, result, user_name=""):
    """Persist the current in-memory result after every accepted vote."""
    conn = db_connect()
    conn.execute("""
        INSERT INTO live_results
        (chat_id, test_id, user_id, user_name, answers_json,
         correct, wrong, score, started_at, finished_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, test_id, user_id)
        DO UPDATE SET
            user_name=excluded.user_name,
            answers_json=excluded.answers_json,
            correct=excluded.correct,
            wrong=excluded.wrong,
            score=excluded.score,
            started_at=excluded.started_at,
            finished_at=excluded.finished_at
    """, (
        int(chat_id),
        int(result["test_id"]),
        int(user_id),
        str(user_name or ""),
        _serialize_answers(result.get("answers", {})),
        int(result.get("correct", 0)),
        int(result.get("wrong", 0)),
        float(result.get("score", 0.0)),
        result["start_time"].isoformat(timespec="seconds")
        if hasattr(result.get("start_time"), "isoformat")
        else str(result.get("start_time", "")),
        result.get("finished_at")
    ))
    conn.commit()
    conn.close()


def load_latest_result(chat_id, user_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT test_id, user_name, answers_json, correct, wrong,
               score, started_at, finished_at
        FROM live_results
        WHERE chat_id = ? AND user_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (int(chat_id), int(user_id)))
    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    try:
        started = datetime.fromisoformat(row[6])
    except Exception:
        started = datetime.now()

    try:
        answers = json.loads(row[2]) if row[2] else {}
    except Exception:
        answers = {}

    # JSON object keys are strings; normalize question indexes back to ints.
    normalized_answers = {}
    for k, v in answers.items():
        try:
            normalized_answers[int(k)] = v
        except Exception:
            normalized_answers[k] = v

    return {
        "chat_id": int(chat_id),
        "user_id": int(user_id),
        "test_id": int(row[0]),
        "user_name": row[1] or "",
        "answers": normalized_answers,
        "correct": int(row[3]),
        "wrong": int(row[4]),
        "score": float(row[5]),
        "start_time": started,
        "finished": bool(row[7]),
        "finished_at": row[7]
    }



def load_results_for_chat_test(chat_id, test_id):
    """Return saved results for one exact test only."""
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT test_id, user_id, user_name, answers_json, correct,
               wrong, score, started_at, finished_at
        FROM live_results
        WHERE chat_id = ? AND test_id = ?
        ORDER BY score DESC, correct DESC, id ASC
    """, (int(chat_id), int(test_id)))
    rows = cur.fetchall()
    conn.close()

    results = []
    for row in rows:
        try:
            answers = json.loads(row[3]) if row[3] else {}
        except Exception:
            answers = {}

        normalized = {}
        for k, v in answers.items():
            try:
                normalized[int(k)] = v
            except Exception:
                normalized[k] = v

        try:
            started = datetime.fromisoformat(row[7])
        except Exception:
            started = datetime.now()

        results.append({
            "chat_id": int(chat_id),
            "user_id": int(row[1]),
            "test_id": int(row[0]),
            "user_name": row[2] or "",
            "answers": normalized,
            "correct": int(row[4]),
            "wrong": int(row[5]),
            "score": float(row[6]),
            "start_time": started,
            "finished": bool(row[8]),
            "finished_at": row[8]
        })

    return results


def get_latest_result_test_id(chat_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT test_id
        FROM live_results
        WHERE chat_id = ?
          AND answers_json IS NOT NULL
          AND answers_json != '{}'
        ORDER BY id DESC
        LIMIT 1
    """, (int(chat_id),))
    row = cur.fetchone()
    conn.close()

    if row:
        return int(row[0])

    latest = get_latest_imported_test()
    return int(latest["id"]) if latest else None


async def resolve_stats_user_id(message):
    """Explicit user id > replied user > command sender."""
    try:
        command = getattr(message, "command", []) or []
        if len(command) >= 2 and command[1].strip().lstrip("-").isdigit():
            return int(command[1].strip())
    except Exception:
        pass

    try:
        reply = getattr(message, "reply_to_message", None)
        if reply and getattr(reply, "from_user", None):
            return int(reply.from_user.id)
    except Exception:
        pass

    try:
        if message.from_user:
            return int(message.from_user.id)
    except Exception:
        pass

    try:
        return int((await app.get_me()).id)
    except Exception:
        return 0


def load_all_results_for_chat(chat_id):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("""
        SELECT test_id, user_id, user_name, answers_json, correct,
               wrong, score, started_at, finished_at
        FROM live_results
        WHERE chat_id = ?
        ORDER BY id ASC
    """, (int(chat_id),))
    rows = cur.fetchall()
    conn.close()

    results = []
    for row in rows:
        try:
            answers = json.loads(row[3]) if row[3] else {}
        except Exception:
            answers = {}

        normalized_answers = {}
        for k, v in answers.items():
            try:
                normalized_answers[int(k)] = v
            except Exception:
                normalized_answers[k] = v

        try:
            started = datetime.fromisoformat(row[7])
        except Exception:
            started = datetime.now()

        results.append({
            "chat_id": int(chat_id),
            "user_id": int(row[1]),
            "test_id": int(row[0]),
            "user_name": row[2] or "",
            "answers": normalized_answers,
            "correct": int(row[4]),
            "wrong": int(row[5]),
            "score": float(row[6]),
            "start_time": started,
            "finished": bool(row[8]),
            "finished_at": row[8]
        })

    return results


# =========================================================
# RUNTIME STATE
# =========================================================

group_tests = {}
user_results = {}
poll_map = {}
processed_votes = set()
pending_votes = {}
pending_tasks = {}
group_locks = {}
vote_lock = asyncio.Lock()


def get_group_lock(chat_id):
    if chat_id not in group_locks:
        group_locks[chat_id] = asyncio.Lock()
    return group_locks[chat_id]


# =========================================================
# HELPERS
# =========================================================

def is_group_chat(message):
    return bool(
        message and message.chat and
        message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    )


def option_letter(index):
    return chr(65 + int(index))


def get_user_name(user):
    if not user:
        return "User"

    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    if name:
        return name

    if user.username:
        return "@" + user.username

    return str(user.id)


def pdf_text(text):
    if text is None:
        return ""
    text = unicodedata.normalize("NFC", str(text))
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def pdf_display_name(name):
    """
    Keep normal Hindi/Latin names intact, but convert Telegram decorative
    Unicode styles that are not present in NotoSansDevanagari into readable
    ASCII equivalents so the name never becomes blank in the PDF.

    Example:
      🦋⃟ᴠͥɪͣᴘͫ⃝ 𝑺𝒉𝒊𝒗𝒊 𝑹𝒂𝒋𝒑𝒖𝒕𝄟
      -> VIP Shivi Rajput
    """
    if name is None:
        return ""

    s = unicodedata.normalize("NFKC", str(name))

    # Common Telegram decorative/modifier letters.
    modifier_map = {
        "ᴠ": "V", "ᴡ": "W", "ʟ": "L", "ᴍ": "M", "ɴ": "N",
        "ɪ": "I", "ᴛ": "T", "ᴘ": "P", "ʀ": "R", "s": "s",
        "ͥ": "", "ͣ": "", "ͫ": "",
    }
    for old, new in modifier_map.items():
        s = s.replace(old, new)

    # Mathematical alphanumeric styles -> normal Latin letters.
    # This covers the common bold/italic/script styled names used in Telegram.
    math_ranges = [
        (0x1D400, 0x1D419, ord("A")),  # Mathematical Bold
        (0x1D41A, 0x1D433, ord("a")),
        (0x1D434, 0x1D44D, ord("A")),  # Mathematical Italic
        (0x1D44E, 0x1D467, ord("a")),
        (0x1D468, 0x1D481, ord("A")),  # Bold Italic
        (0x1D482, 0x1D49B, ord("a")),
        (0x1D4D0, 0x1D4E9, ord("A")),  # Fraktur
        (0x1D4EA, 0x1D503, ord("a")),
        (0x1D504, 0x1D51D, ord("A")),  # Double-struck
        (0x1D51E, 0x1D537, ord("a")),
        (0x1D538, 0x1D551, ord("A")),
        (0x1D552, 0x1D56B, ord("a")),
        (0x1D56C, 0x1D585, ord("A")),  # Sans
        (0x1D586, 0x1D59F, ord("a")),
        (0x1D5A0, 0x1D5B9, ord("A")),  # Sans Bold
        (0x1D5BA, 0x1D5D3, ord("a")),
        (0x1D5D4, 0x1D5ED, ord("A")),
        (0x1D5EE, 0x1D607, ord("a")),
        (0x1D608, 0x1D621, ord("A")),
        (0x1D622, 0x1D63B, ord("a")),
        (0x1D63C, 0x1D655, ord("A")),
        (0x1D656, 0x1D66F, ord("a")),
        (0x1D670, 0x1D689, ord("A")),  # Monospace
        (0x1D68A, 0x1D6A3, ord("a")),
        (0x1D6A4, 0x1D6BD, ord("A")),
        (0x1D6BE, 0x1D6D7, ord("a")),
        (0x1D6E2, 0x1D6FB, ord("A")),
        (0x1D6FC, 0x1D715, ord("a")),
        (0x1D71A, 0x1D733, ord("A")),
        (0x1D734, 0x1D74D, ord("a")),
        (0x1D756, 0x1D76F, ord("A")),
        (0x1D770, 0x1D789, ord("a")),
        (0x1D78A, 0x1D7A3, ord("A")),
        (0x1D7A4, 0x1D7BD, ord("a")),
        (0x1D7CE, 0x1D7D7, ord("0")),  # Mathematical digits
        (0x1D7D8, 0x1D7E1, ord("0")),
        (0x1D7E2, 0x1D7EB, ord("0")),
        (0x1D7EC, 0x1D7F5, ord("0")),
        (0x1D7F6, 0x1D7FF, ord("0")),
    ]

    out_chars = []
    for ch in s:
        cp = ord(ch)
        replaced = False
        for lo, hi, base in math_ranges:
            if lo <= cp <= hi:
                out_chars.append(chr(base + (cp - lo)))
                replaced = True
                break
        if replaced:
            continue

        # Remove combining enclosing circles/marks and musical/decorative
        # symbols that NotoSansDevanagari does not contain.
        if cp in {
            0x20DD, 0x20DF, 0x1D11F,  # ⃝ ⃟ 𝄟
            0x1F300,  # start of emoji block (handled more generally below)
        }:
            out_chars.append(" ")
            continue

        # Emoji/supplementary-plane symbols are not safely supported by the
        # configured Devanagari TTF. Replace them with a readable marker.
        if cp > 0xFFFF:
            # Mathematical letters were already handled above.
            out_chars.append(" ")
            continue

        # Keep ordinary Devanagari, Latin, digits, punctuation and spaces.
        out_chars.append(ch)

    cleaned = "".join(out_chars)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # If a decorative VIP prefix collapsed, preserve the useful readable name.
    if not cleaned:
        return "User"

    return cleaned


async def safe_send_message(chat_id, text, **kwargs):
    while True:
        try:
            return await app.send_message(chat_id, text, **kwargs)
        except FloodWait as e:
            await asyncio.sleep(int(getattr(e, "value", 5)) + 1)


async def safe_delete_message(chat_id, message_id):
    while True:
        try:
            await app.delete_messages(chat_id, message_id)
            return True
        except FloodWait as e:
            await asyncio.sleep(int(getattr(e, "value", 5)) + 1)
        except Exception as e:
            print("Delete error:", repr(e))
            return False


async def safe_send_document(chat_id, path, caption=""):
    while True:
        try:
            return await app.send_document(
                chat_id,
                document=path,
                caption=caption
            )
        except FloodWait as e:
            await asyncio.sleep(int(getattr(e, "value", 5)) + 1)


async def safe_send_poll(chat_id, question, options, correct_option, timer):
    """Send quiz polls when keyed, otherwise regular non-anonymous polls."""
    timer = max(5, min(int(timer), 600))
    correct_option = int(correct_option) if correct_option is not None else -1

    while True:
        try:
            kwargs = {
                "chat_id": chat_id,
                "question": question,
                "options": options,
                "is_anonymous": False,
                "allows_multiple_answers": False,
                "open_period": timer,
            }

            if correct_option >= 0:
                kwargs.update({
                    "type": PollType.QUIZ,
                    "correct_option_id": correct_option,
                    "explanation": (
                        f"Correct Answer: {option_letter(correct_option)}"
                    )
                })
            else:
                kwargs.update({"type": PollType.REGULAR})

            return await app.send_poll(**kwargs)

        except FloodWait as e:
            await asyncio.sleep(int(getattr(e, "value", 5)) + 1)


async def is_admin(chat_id, user_id):
    try:
        check_id = int(user_id or SELF_USER_ID)
        if not check_id:
            me = await app.get_me()
            check_id = int(me.id)
        member = await app.get_chat_member(chat_id, check_id)
        return member.status in (
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        )
    except Exception:
        return False


async def resolve_command_user_id(message):
    """Resolve sender ID for normal Telegram bot messages."""
    try:
        if message and getattr(message, "from_user", None):
            uid = int(message.from_user.id)
            if uid:
                return uid
    except Exception:
        pass

    try:
        return int((await app.get_me()).id)
    except Exception:
        return int(SELF_USER_ID or 0)


async def can_start_test(chat_id, user_id):
    """Any group admin/owner can start the test."""
    return await is_admin(chat_id, user_id)


# =========================================================
# SOURCE LINK / MESSAGE PARSER
# =========================================================

def parse_source_reference(reference, current_chat_id):
    reference = reference.strip().rstrip("/")

    if reference.isdigit():
        return current_chat_id, int(reference)

    # https://t.me/c/1234567890/123
    match = re.match(
        r"^https?://t\.me/c/(\d+)/(\d+)$",
        reference
    )
    if match:
        return int("-100" + match.group(1)), int(match.group(2))

    # https://t.me/username/123
    match = re.match(
        r"^https?://t\.me/([^/]+)/(\d+)$",
        reference
    )
    if match:
        return match.group(1), int(match.group(2))

    raise ValueError("Invalid Telegram message link/message ID.")


# =========================================================
# GENERIC POLL EXTRACTION
# =========================================================

def extract_poll_from_message(message):
    """
    Import quiz and regular Telegram polls. Anonymous/closed status is not
    used as a filter, so closed and anonymous polls are accepted too.
    Regular polls have no Telegram answer key and are stored with correct=-1.
    """
    if not message or not getattr(message, "poll", None):
        return None

    poll = message.poll
    options = [
        str(getattr(x, "text", "") or "").strip()
        for x in (poll.options or [])
    ]
    options = [x for x in options if x]

    if len(options) < 2:
        return None

    question = str(getattr(poll, "question", "") or "").strip()
    if not question:
        return None

    poll_type = getattr(poll, "type", None)
    is_quiz = poll_type == PollType.QUIZ or str(poll_type).lower() == "quiz"

    correct = -1
    if is_quiz:
        raw_correct = getattr(poll, "correct_option_id", None)
        if raw_correct is None:
            ids = getattr(poll, "correct_option_ids", None)
            if ids:
                raw_correct = list(ids)[0]
        try:
            correct = int(raw_correct) if raw_correct is not None else -1
        except Exception:
            correct = -1

        if not (0 <= correct < len(options)):
            correct = -1

    source_timer = getattr(poll, "open_period", None)

    return {
        "question": question[:300],
        "options": [x[:100] for x in options[:10]],
        "correct": correct,
        "source_message_id": int(message.id),
        "source_timer": int(source_timer) if source_timer else None,
        "source_poll_type": "quiz" if is_quiz else "regular",
        "source_anonymous": bool(getattr(poll, "is_anonymous", False)),
        "source_closed": bool(getattr(poll, "is_closed", False)),
    }


extract_quiz_from_message = extract_poll_from_message


async def import_poll_questions(source_chat, start_message_id, question_count):
    if not (1 <= question_count <= MAX_QUESTIONS):
        raise ValueError(
            f"Questions 1 se {MAX_QUESTIONS} ke beech hone chahiye."
        )

    questions = []
    current_id = int(start_message_id)
    end_limit = int(start_message_id) + IMPORT_SCAN_LIMIT

    while len(questions) < question_count and current_id < end_limit:
        batch_end = min(current_id + 100, end_limit)
        ids = list(range(current_id, batch_end))

        try:
            messages = await app.get_messages(source_chat, ids)
        except Exception as e:
            raise RuntimeError(
                "Source messages read nahi ho paaye. "
                "Bot account ko source chat/channel access hona chahiye.\n"
                f"Telegram error: {e}"
            )

        for message in messages:
            q = extract_poll_from_message(message)
            if not q:
                continue

            questions.append(q)
            flags = []
            if q["source_anonymous"]:
                flags.append("anonymous")
            if q["source_closed"]:
                flags.append("closed")
            flag_text = ", ".join(flags) if flags else "open/non-anonymous"

            print(
                f"Imported Q{len(questions)} | Message {message.id} | "
                f"{q['source_poll_type'].upper()} | {flag_text} | "
                f"correct={q['correct']}"
            )

            if len(questions) >= question_count:
                break

        current_id = batch_end

    if not questions:
        raise RuntimeError(
            "Starting message se koi readable Telegram Poll nahi mila."
        )

    if len(questions) < question_count:
        raise RuntimeError(
            f"Sirf {len(questions)} Poll mile, "
            f"{question_count} requested the."
        )

    return questions


import_quiz_questions = import_poll_questions


# =========================================================
# /START
# =========================================================

async def start_command(client, message):
    await safe_send_message(
        message.chat.id,
        "👋 LIVE TEST BOT STEP 10 READY\n\n"
        "/import <link_or_msg_id> <questions> [timer|auto]\n"
        "/test [test_id]\n"
        "/mystats\n"
        "/leaderboard\n"
        "/teststatus\n"
        "/stoptest"
    )


# =========================================================
# /IMPORT
# =========================================================

async def import_command(client, message):
    """Hybrid mode: the user-session source reader performs /import.

    The real bot intentionally does not attempt to read arbitrary source
    history. The source reader sees the same command, reads the source using
    the user's Telegram access, and uploads a JSON import package to this bot.
    """
    if not is_group_chat(message):
        return
    await safe_send_message(
        message.chat.id,
        "📥 /import request received. Source-reader user session is importing "
        "the source polls; test package will arrive here automatically."
    )


# =========================================================
# /TEST
# =========================================================

async def start_test(client, message):
    if not is_group_chat(message):
        await safe_send_message(message.chat.id, "⚠️ /test group me chalega.")
        return

    chat_id = int(message.chat.id)
    user_id = await resolve_command_user_id(message)

    if not await is_admin(chat_id, user_id):
        await safe_send_message(
            chat_id,
            "⚠️ /test sirf group admin/owner start kar sakta hai."
        )
        return

    if chat_id in group_tests:
        await safe_send_message(chat_id, "⚠️ Test already active hai.")
        return

    test = None
    if len(message.command) >= 2:
        try:
            test = get_imported_test(int(message.command[1]))
        except Exception:
            test = None
    else:
        test = get_latest_imported_test()

    if not test or not test["questions"]:
        await safe_send_message(
            chat_id,
            "❌ Imported test nahi mila.\n\n"
            "Latest test ke liye /test\n"
            "Specific test ke liye /test <test_id>"
        )
        return

    async with get_group_lock(chat_id):
        if chat_id in group_tests:
            return

        group_tests[chat_id] = {
            "test_id": test["id"],
            "name": test["name"],
            "questions": test["questions"],
            "timer": test["timer_seconds"],
            "negative": float(NEGATIVE_MARK),
            "started_by": user_id,
            "started_at": datetime.now(),
            "current_index": -1,
            "current_poll_id": None,
            "current_message_id": None,
            "participants": set(),
            "runner": None,
            "finished": False
        }

    await safe_send_message(
        chat_id,
        "🚀 LIVE TEST STARTED\n\n"
        f"📚 Total: {len(test['questions'])}\n"
        f"⏱ Timer: {test['timer_seconds']} sec/question\n"
        "✅ Correct: +1\n"
        f"❌ Wrong: -{test['negative_mark']}\n"
        "⭕ Unattempted: 0\n\n"
        "एक समय में केवल 1 question visible रहेगा."
    )

    group_tests[chat_id]["runner"] = asyncio.create_task(
        run_group_test(chat_id)
    )


# =========================================================
# TEST RUNNER
# =========================================================

async def run_group_test(chat_id):
    state = group_tests.get(chat_id)
    if not state:
        return

    questions = state["questions"]
    timer = state["timer"]

    try:
        for index, q in enumerate(questions):
            state = group_tests.get(chat_id)
            if not state or state["finished"]:
                return

            state["current_index"] = index

            poll_message = await safe_send_poll(
                chat_id,
                f"📝 Q{index + 1}/{len(questions)}\n\n{q['question']}",
                q["options"],
                q["correct"],
                timer
            )

            poll_id = str(poll_message.poll.id)

            state["current_poll_id"] = poll_id
            state["current_message_id"] = poll_message.id

            # PollOption.data contains the real Telegram option identifier.
            # We need this for messages.GetPollVotes(option=...) fallback.
            option_data = []
            try:
                for opt in (poll_message.poll.options or []):
                    data = getattr(opt, "data", None)
                    option_data.append(bytes(data) if data is not None else None)
            except Exception as e:
                print(f"⚠️ POLL OPTION DATA READ FAILED: {e!r}")
                option_data = [None] * len(q["options"])

            poll_map[poll_id] = {
                "chat_id": chat_id,
                "poll_id": poll_id,
                "index": index,
                "question": q["question"],
                "options": q["options"],
                "correct": q["correct"],
                "source_message_id": q["source_message_id"],
                "message_id": poll_message.id,
                "option_data": option_data
            }

            print(
                f"📤 Question sent | Q={len(questions)-index} "
                f"| Message={poll_message.id} | Poll={poll_id}"
            )
            print(
                f"🔐 Poll option IDs captured: "
                f"{len(poll_map[poll_id].get('option_data') or [])}"
            )
            print(
                f"⏱ TIMER START | Q={index + 1} | {timer}s"
            )

            # Bot accounts receive every non-anonymous vote for polls
            # sent by this bot through UpdateMessagePollVote/PollAnswer.
            # Therefore we do NOT call messages.GetPollVotes here.
            # That method is user-only and was the source of the old
            # selfbot-specific reconciliation dependency.
            await asyncio.sleep(timer + 0.20)

            # Delete immediately after the timer. Vote updates already
            # received have been persisted by process_vote().
            await safe_delete_message(
                chat_id,
                poll_message.id
            )
            print(
                f"🗑 POLL DELETED | Q={index + 1} "
                f"| Message={poll_message.id}"
            )

            # Cancel any pending worker only after reconciliation.
            task = pending_tasks.get(poll_id)
            if task and not task.done():
                task.cancel()

            pending_tasks.pop(poll_id, None)
            pending_votes.pop(poll_id, None)
            poll_map.pop(poll_id, None)

            state["current_poll_id"] = None
            state["current_message_id"] = None

            print(f"⏱ TIMEOUT | Q={index + 1}")

        await finish_group_test(chat_id)

    except asyncio.CancelledError:
        print(f"TEST CANCELLED | chat={chat_id}")
    except Exception as e:
        print("TEST RUNNER ERROR:", repr(e))
        await safe_send_message(
            chat_id,
            f"⚠️ Test runner error: {e}"
        )


# =========================================================
# VOTE PARSING
# =========================================================

def parse_selected_option(options):
    """
    Telegram poll options are bytes containing the option index
    (0, 1, 2, ...). Handle both bytes and int-like values.
    """
    if options is None:
        return None

    # Single-option raw update: bytes / bytearray / int / list.
    if isinstance(options, (bytes, bytearray)):
        if not options:
            return None
        return int(options[0])

    if isinstance(options, int):
        return int(options)

    if isinstance(options, (list, tuple)):
        if not options:
            return None
        value = options[0]
        if isinstance(value, (bytes, bytearray)):
            return int(value[0]) if value else None
        if isinstance(value, int):
            return int(value)
        try:
            return int(value)
        except Exception:
            return None

    try:
        return int(options)
    except Exception:
        return None


def extract_vote_option(vote):
    """
    messages.getPollVotes returns MessageUserVote for a normal
    single-choice poll with .option, and MessageUserVoteMultiple
    with .options for multiple-choice polls.
    """
    if vote is None:
        return None

    if hasattr(vote, "option"):
        return parse_selected_option(vote.option)

    if hasattr(vote, "options"):
        return parse_selected_option(vote.options)

    return None


async def reconcile_poll_votes(poll_id):
    # Kept only as a compatibility stub for old saved/runtime references.
    # Real bot mode uses PollAnswer/UpdateMessagePollVote events.
    print(
        f"ℹ️ BOT MODE: skipping GetPollVotes reconciliation | poll={poll_id}"
    )
    return 0


# =========================================================
# BOT POLL ANSWER
# =========================================================
# Real bot accounts receive non-anonymous answers for polls sent by
# the bot itself. This is the authoritative vote path in bot mode.
# It avoids messages.GetPollVotes entirely (that method is user-only).
# =========================================================

@app.on_raw_update()
async def bot_poll_vote_handler(client, update, users, chats):
    if not isinstance(update, raw.types.UpdateMessagePollVote):
        return

    poll_id = str(update.poll_id)
    user_id = int(update.user_id)
    options = update.options

    print(
        f"📥 BOT POLL ANSWER | poll={poll_id} "
        f"user={user_id} options={options}"
    )

    poll_data = poll_map.get(poll_id)

    # Race protection: queue a vote until the poll metadata has been
    # registered. In normal operation poll_map is created immediately
    # after send_poll(), but this keeps startup/update races safe.
    if poll_data is None:
        pending_votes.setdefault(poll_id, []).append({
            "user_id": user_id,
            "options": options
        })
        if poll_id not in pending_tasks:
            pending_tasks[poll_id] = asyncio.create_task(
                process_pending_votes(poll_id)
            )
        return

    await process_vote(
        poll_data,
        user_id,
        options,
        users
    )


# =========================================================
# PROCESS VOTE
# =========================================================

async def process_vote(poll_data, user_id, options, users=None):
    poll_id = str(poll_data["poll_id"])
    chat_id = poll_data["chat_id"]

    selected = parse_selected_option(options)
    if selected is None:
        return

    if not 0 <= selected < len(poll_data["options"]):
        return

    vote_key = (poll_id, user_id)

    async with vote_lock:
        if vote_key in processed_votes:
            return
        processed_votes.add(vote_key)

    state = group_tests.get(chat_id)
    if not state or state.get("current_poll_id") != poll_id:
        return

    user_key = (chat_id, user_id)

    if user_key not in user_results:
        user_results[user_key] = {
            "chat_id": chat_id,
            "user_id": user_id,
            "test_id": state["test_id"],
            "answers": {},
            "correct": 0,
            "wrong": 0,
            "score": 0.0,
            "start_time": state["started_at"],
            "finished": False
        }

    result = user_results[user_key]
    state["participants"].add(user_id)

    index = poll_data["index"]

    if index in result["answers"]:
        return

    correct = int(poll_data.get("correct", -1))
    is_keyed = correct >= 0
    is_correct = is_keyed and selected == correct

    result["answers"][index] = {
        "question_no": index + 1,
        "question": poll_data["question"],
        "options": poll_data["options"],
        "selected": selected,
        "correct": correct,
        "source_message_id": poll_data["source_message_id"]
    }

    if not is_keyed:
        status = "UNKEYED"
    elif is_correct:
        result["correct"] += 1
        result["score"] += 1.0
        status = "CORRECT"
    else:
        result["wrong"] += 1
        result["score"] -= state["negative"]
        status = "WRONG"

    try:
        user_name = ""
        if users:
            try:
                u = users.get(user_id)
                user_name = get_user_name(u)
            except Exception:
                pass
        if not user_name:
            try:
                u = await app.get_users(user_id)
                user_name = get_user_name(u)
            except Exception:
                user_name = str(user_id)

        persist_result(
            chat_id,
            user_id,
            result,
            user_name
        )
    except Exception as e:
        print(
            f"⚠️ RESULT DB SAVE FAILED user={user_id}: {e!r}"
        )

    print(
        f"ANSWER | user={user_id} | Q={index+1} "
        f"| selected={option_letter(selected)} "
        f"| correct={option_letter(correct)} "
        f"| {status} | score={result['score']:.2f}"
    )



# =========================================================
# =========================================================
# MIXED-FONT HARFBUZZ VECTOR TEXT ENGINE
# =========================================================
# Noto Sans Devanagari is script-specific, while Noto Sans contains the
# Latin/ASCII side. Every line is split into Devanagari and non-Devanagari
# runs, and each run is shaped with the correct font. This prevents Hindi
# words, English words, URLs, digits and punctuation from disappearing.

_HB_CONTEXTS = {}


class _HBReportLabPen(BasePen):
    """BasePen that writes one font glyph outline into a ReportLab path."""

    def __init__(self, glyphSet, path, ox=0.0, oy=0.0, scale=1.0):
        super().__init__(glyphSet)
        self.path = path
        self.ox = float(ox)
        self.oy = float(oy)
        self.scale = float(scale)

    def _p(self, p):
        return (
            self.ox + p[0] * self.scale,
            self.oy + p[1] * self.scale
        )

    def _moveTo(self, p):
        x, y = self._p(p)
        self.path.moveTo(x, y)

    def _lineTo(self, p):
        x, y = self._p(p)
        self.path.lineTo(x, y)

    def _curveToOne(self, p1, p2, p3):
        x1, y1 = self._p(p1)
        x2, y2 = self._p(p2)
        x3, y3 = self._p(p3)
        self.path.curveTo(x1, y1, x2, y2, x3, y3)

    def _closePath(self):
        self.path.close()


def _hb_init_context(role):
    """Initialize a HarfBuzz + FontTools context for one static TTF."""
    if role in _HB_CONTEXTS:
        return _HB_CONTEXTS[role]

    if role == "deva":
        path = FONT_PATH
    else:
        path = LATIN_FONT_PATH

    with open(path, "rb") as fh:
        font_bytes = fh.read()

    face = hb.Face(font_bytes)
    font = hb.Font(face)
    try:
        hb.ot_font_set_funcs(font)
    except Exception:
        pass

    ft = FTFont(path, lazy=False)
    glyph_set = ft.getGlyphSet()
    glyph_order = ft.getGlyphOrder()
    upm = int(face.upem or 1000)

    if not glyph_order or glyph_order[0] != ".notdef":
        raise RuntimeError(
            f"Invalid glyph order in selected {role} font."
        )

    ctx = {
        "bytes": font_bytes,
        "face": face,
        "font": font,
        "glyph_set": glyph_set,
        "glyph_order": glyph_order,
        "upm": upm,
        "path": path,
    }
    _HB_CONTEXTS[role] = ctx
    return ctx


def _is_deva_char(ch):
    cp = ord(ch)
    # Devanagari block + combining marks used by Hindi.
    return 0x0900 <= cp <= 0x097F


def _split_script_runs(text):
    """
    Split text without dropping anything:
      - Devanagari block -> deva font / Deva shaping
      - everything else -> Latin font / Latn shaping
    Spaces stay with their neighboring run.
    """
    s = unicodedata.normalize("NFC", str(text or ""))
    if not s:
        return []

    runs = []
    current_role = None
    current = []

    for ch in s:
        role = "deva" if _is_deva_char(ch) else "latin"
        if role != current_role and current:
            runs.append((current_role, "".join(current)))
            current = []
        current_role = role
        current.append(ch)

    if current:
        runs.append((current_role, "".join(current)))

    return runs


def _hb_shape_run(text, role):
    ctx = _hb_init_context(role)
    s = unicodedata.normalize("NFC", str(text or ""))

    # Do not turn supplementary characters into missing-glyph boxes.
    # They are handled by the existing name/text cleanup elsewhere.
    s = "".join(ch if ord(ch) <= 0xFFFF else " " for ch in s)

    buf = hb.Buffer()
    buf.add_str(s)

    try:
        if role == "deva":
            buf.direction = "ltr"
            buf.script = "Deva"
            buf.language = "hi"
        else:
            buf.direction = "ltr"
            buf.script = "Latn"
            buf.language = "en"
    except Exception:
        buf.guess_segment_properties()

    hb.shape(
        ctx["font"],
        buf,
        {
            "kern": True,
            "liga": True,
            "clig": True,
            "calt": True
        }
    )

    out = []
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        out.append({
            "role": role,
            "gid": int(info.codepoint),
            "cluster": int(info.cluster),
            "x_advance": int(pos.x_advance),
            "y_advance": int(pos.y_advance),
            "x_offset": int(pos.x_offset),
            "y_offset": int(pos.y_offset),
        })
    return out


def _hb_shape(text):
    """Shape the full string with the correct font for each script run."""
    out = []
    for role, run_text in _split_script_runs(text):
        out.extend(_hb_shape_run(run_text, role))
    return out


def _hb_width(text, font_size):
    total = 0.0
    for role, run_text in _split_script_runs(text):
        ctx = _hb_init_context(role)
        glyphs = _hb_shape_run(run_text, role)
        total += sum(g["x_advance"] for g in glyphs)
        total = float(total)
    # Width conversion is safe because both Noto fonts use the same
    # units-per-em in normal releases; calculate each run separately if not.
    # Recalculate exactly in case UPM differs.
    total_pt = 0.0
    for role, run_text in _split_script_runs(text):
        ctx = _hb_init_context(role)
        total_pt += (
            sum(g["x_advance"] for g in _hb_shape_run(run_text, role))
            * float(font_size) / float(ctx["upm"])
        )
    return total_pt


def _hb_draw_text(c, text, x, baseline_y, font_size, color=colors.black):
    """
    Draw mixed Devanagari + Latin text directly as vector outlines.
    No Pillow, no RAQM, and no ReportLab Unicode shaping dependency.
    """
    c.saveState()
    c.setFillColor(color)

    cursor_x = float(x)
    cursor_y = float(baseline_y)

    for role, run_text in _split_script_runs(text):
        ctx = _hb_init_context(role)
        glyphs = _hb_shape_run(run_text, role)
        scale = float(font_size) / float(ctx["upm"])

        for g in glyphs:
            gid = g["gid"]
            glyph_order = ctx["glyph_order"]
            glyph_set = ctx["glyph_set"]

            if gid < 0 or gid >= len(glyph_order):
                cursor_x += g["x_advance"] * scale
                cursor_y += g["y_advance"] * scale
                continue

            glyph_name = glyph_order[gid]

            # Never draw .notdef. A missing character should not create a box.
            if (
                gid == 0
                or glyph_name == ".notdef"
                or glyph_name not in glyph_set
            ):
                cursor_x += g["x_advance"] * scale
                cursor_y += g["y_advance"] * scale
                continue

            dx = g["x_offset"] * scale
            dy = g["y_offset"] * scale

            path = c.beginPath()
            pen = _HBReportLabPen(
                glyph_set,
                path,
                ox=cursor_x + dx,
                oy=cursor_y + dy,
                scale=scale
            )
            glyph_set[glyph_name].draw(pen)
            c.drawPath(path, fill=1, stroke=0)

            cursor_x += g["x_advance"] * scale
            cursor_y += g["y_advance"] * scale

    c.restoreState()
    return cursor_x


def _hb_wrap(text, font_size, max_width):
    """
    Wrap mixed-font text without ever dropping a token.
    Normal words wrap at spaces; long URLs/IDs are additionally broken
    character-by-character so they cannot run outside the table column.
    """
    text = unicodedata.normalize("NFC", str(text or ""))

    def break_long_token(token):
        if not token:
            return [""]
        if _hb_width(token, font_size) <= max_width:
            return [token]

        pieces = []
        piece = ""
        for ch in token:
            candidate = piece + ch
            if piece and _hb_width(candidate, font_size) > max_width:
                pieces.append(piece)
                piece = ch
            else:
                piece = candidate
        if piece:
            pieces.append(piece)
        return pieces or [""]

    result = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            result.append("")
            continue

        current = ""
        for word in words:
            # A very long URL/ID gets split before it is placed.
            word_parts = break_long_token(word)

            for part in word_parts:
                candidate = part if not current else current + " " + part

                if (
                    not current
                    or _hb_width(candidate, font_size) <= max_width
                ):
                    current = candidate
                    continue

                result.append(current)
                current = part

        if current:
            result.append(current)

    return result or [""]


def _hb_draw_wrapped(
    c,
    text,
    x,
    y,
    font_size,
    max_width,
    leading=None,
    color=colors.black
):
    if leading is None:
        leading = font_size * 1.45

    lines = _hb_wrap(text, font_size, max_width)

    for line in lines:
        _hb_draw_text(
            c,
            line,
            x,
            y,
            font_size,
            color=color
        )
        y -= leading

    return y


class _HBTextFlowable(Flowable):
    """A ReportLab flowable that renders mixed Hindi/Latin text with HarfBuzz."""

    def __init__(
        self,
        text,
        font_size=9,
        leading=None,
        color=colors.black,
        align="LEFT",
    ):
        Flowable.__init__(self)
        self.text = unicodedata.normalize("NFC", str(text or ""))
        self.font_size = float(font_size)
        self.leading = float(leading or (self.font_size * 1.35))
        self.color = color
        self.align = align.upper()
        self._width = 0.0
        self._height = self.leading

    def wrap(self, availWidth, availHeight):
        self._width = min(
            float(availWidth),
            max(1.0, _hb_width(self.text, self.font_size))
        )
        self._height = self.leading
        return self._width, self._height

    def draw(self):
        if self.align == "CENTER":
            x = 0
        elif self.align == "RIGHT":
            x = max(0.0, self._width - _hb_width(self.text, self.font_size))
        else:
            x = 0

        # ReportLab Flowable origin is lower-left; draw baseline near top.
        baseline = self._height * 0.28
        _hb_draw_text(
            self.canv,
            self.text,
            x,
            baseline,
            self.font_size,
            color=self.color,
        )


def _hb_draw_boxed_answer_key(state, filepath):
    """
    Create the Official Answer Key as a separate vector PDF using
    HarfBuzz-shaped Devanagari, then merge it with the result PDF.

    This avoids ReportLab's raw-Unicode Devanagari limitation while also
    avoiding Pillow/RAQM completely.
    """
    from reportlab.lib.pagesizes import A4

    page_w, page_h = A4

    c = pdf_canvas.Canvas(
        filepath,
        pagesize=(page_w, page_h),
        pageCompression=1
    )

    # Compact 3-column official-answer-key layout.
    left = 22
    right = 22
    top = page_h - 32
    bottom = 30
    table_left = left
    table_right = page_w - right

    q_col = 32
    answer_col = 100
    middle_left = table_left + q_col
    answer_left = table_right - answer_col
    middle_width = answer_left - middle_left

    # Header.
    _hb_draw_text(
        c,
        "OFFICIAL ANSWER KEY",
        page_w / 2 - _hb_width("OFFICIAL ANSWER KEY", 19) / 2,
        top,
        19,
        color=colors.HexColor("#123B66")
    )

    subtitle = (
        f"Imported Test {datetime.now().strftime('%Y-%m-%d %H:%M')} | "
        f"Questions: {len(state['questions'])} | "
        f"Max Marks: {len(state['questions'])}"
    )
    _hb_draw_text(
        c,
        subtitle,
        page_w / 2 - _hb_width(subtitle, 9.5) / 2,
        top - 27,
        9.5,
        color=colors.HexColor("#4B5D73")
    )

    table_top = top - 48

    def header_cell(x0, x1, label):
        c.setFillColor(colors.HexColor("#1769AA"))
        c.rect(
            x0,
            table_top - 25,
            x1 - x0,
            25,
            fill=1,
            stroke=0
        )
        w = _hb_width(label, 8.5)
        _hb_draw_text(
            c,
            label,
            x0 + (x1 - x0 - w) / 2,
            table_top - 17,
            8.5,
            color=colors.white
        )

    header_cell(table_left, middle_left, "Q.")
    header_cell(middle_left, answer_left, "QUESTION / OPTIONS")
    header_cell(answer_left, table_right, "CORRECT ANSWER")

    y = table_top - 25
    font_q = 8.3
    font_opt = 7.8
    font_ans = 8.2
    lead_q = 11.0
    lead_opt = 10.3
    row_pad = 6

    for idx, q in enumerate(state["questions"], start=1):
        # Calculate row height before drawing.
        q_lines = _hb_wrap(
            f"{idx}. {q['question']}",
            font_q,
            middle_width - 14
        )
        option_lines = []
        for i, option in enumerate(q["options"]):
            option_lines.extend(
                _hb_wrap(
                    f"{option_letter(i)}) {option}",
                    font_opt,
                    middle_width - 14
                )
            )

        correct_idx = int(q.get("correct", -1))
        if 0 <= correct_idx < len(q["options"]):
            correct_label = (
                f"{option_letter(correct_idx)} - "
                f"{q['options'][correct_idx]}"
            )
        else:
            correct_label = "NO ANSWER KEY (SOURCE REGULAR POLL)"

        ans_lines = _hb_wrap(
            correct_label,
            font_ans,
            answer_col - 12
        )

        middle_lines_height = (
            len(q_lines) * lead_q +
            len(option_lines) * lead_opt
        )
        ans_height = len(ans_lines) * (font_ans * 1.4)
        row_h = max(
            middle_lines_height,
            ans_height,
            24
        ) + row_pad * 2

        # New page if necessary.
        if y - row_h < bottom:
            c.showPage()

            # Re-draw header on continuation page.
            top = page_h - 32
            table_top = top - 48
            header_cell(table_left, middle_left, "Q.")
            header_cell(middle_left, answer_left, "QUESTION / OPTIONS")
            header_cell(answer_left, table_right, "CORRECT ANSWER")
            y = table_top - 25

        # Row background.
        if idx % 2 == 0:
            c.setFillColor(colors.HexColor("#F3F8FC"))
        else:
            c.setFillColor(colors.white)
        c.rect(
            table_left,
            y - row_h,
            table_right - table_left,
            row_h,
            fill=1,
            stroke=0
        )

        # Correct-answer green cell.
        c.setFillColor(colors.HexColor("#EAF7EF"))
        c.rect(
            answer_left,
            y - row_h,
            answer_col,
            row_h,
            fill=1,
            stroke=0
        )

        # Grid.
        c.setStrokeColor(colors.HexColor("#9FB6CC"))
        c.setLineWidth(0.45)
        c.rect(
            table_left,
            y - row_h,
            table_right - table_left,
            row_h,
            fill=0,
            stroke=1
        )
        c.line(middle_left, y, middle_left, y - row_h)
        c.line(answer_left, y, answer_left, y - row_h)

        # Q number.
        q_num = str(idx)
        q_num_w = _hb_width(q_num, 8.5)
        _hb_draw_text(
            c,
            q_num,
            table_left + (q_col - q_num_w) / 2,
            y - 16,
            8.5
        )

        # Question + options.
        ty = y - row_pad - font_q
        for line in q_lines:
            _hb_draw_text(
                c,
                line,
                middle_left + 7,
                ty,
                font_q
            )
            ty -= lead_q

        for i, option in enumerate(q["options"]):
            option_lines_i = _hb_wrap(
                f"{option_letter(i)}) {option}",
                font_opt,
                middle_width - 14
            )
            for line in option_lines_i:
                if 0 <= correct_idx < len(q["options"]) and i == correct_idx:
                    _hb_draw_text(
                        c,
                        line,
                        middle_left + 7,
                        ty,
                        font_opt,
                        color=colors.HexColor("#158447")
                    )
                else:
                    _hb_draw_text(
                        c,
                        line,
                        middle_left + 7,
                        ty,
                        font_opt
                    )
                ty -= lead_opt

        # Correct answer.
        ay = y - row_pad - font_ans
        for line in ans_lines:
            _hb_draw_text(
                c,
                line,
                answer_left + 6,
                ay,
                font_ans,
                color=colors.HexColor("#168447")
            )
            ay -= font_ans * 1.4

        y -= row_h

    c.save()





# =========================================================
# FINISH GROUP TEST
# =========================================================

async def create_consolidated_result_pdf(
    chat_id,
    state,
    participants
):
    """
    Create ONE final PDF:
      1) rank-wise result page(s)
      2) official answer key page(s)

    Hindi in the answer key is shaped by HarfBuzz and drawn as vector
    outlines. Pillow/RAQM is not used.
    """
    total = len(state["questions"])
    max_marks = total

    saved = load_all_results_for_chat(chat_id)

    by_user = {}
    for row in saved:
        if int(row["test_id"]) != int(state["test_id"]):
            continue
        if not row.get("answers"):
            continue
        by_user[int(row["user_id"])] = row

    for uid in participants:
        uid = int(uid)
        if uid in by_user:
            continue
        runtime = user_results.get((chat_id, uid))
        if runtime and runtime.get("answers"):
            by_user[uid] = runtime

    rows = []

    for uid, result in by_user.items():
        correct = int(result.get("correct", 0))
        wrong = int(result.get("wrong", 0))
        attempted = len(result.get("answers", {}))
        skip = max(0, total - attempted)
        obtained = float(result.get("score", 0.0))

        name = result.get("user_name") or str(uid)

        rows.append({
            "user_id": uid,
            "name": name,
            "max_marks": max_marks,
            "obtained": obtained,
            "right": correct,
            "wrong": wrong,
            "skip": skip
        })

    rows.sort(
        key=lambda r: (
            r["obtained"],
            r["right"],
            -r["wrong"],
            r["right"] + r["wrong"]
        ),
        reverse=True
    )

    filename = (
        "Result_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
        + ".pdf"
    )
    final_path = os.path.join(PDF_FOLDER, filename)

    result_only_path = final_path.replace(
        ".pdf",
        "_result_only.pdf"
    )
    answer_only_path = final_path.replace(
        ".pdf",
        "_answer_key.pdf"
    )

    try:
        # ---------------------------------------------------------
        # PART 1: RESULT — ReportLab
        # ---------------------------------------------------------
        doc = SimpleDocTemplate(
            result_only_path,
            pagesize=A4,
            rightMargin=24,
            leftMargin=24,
            topMargin=28,
            bottomMargin=28,
            title="Live Test Result"
        )

        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            "ConsolidatedTitleV15",
            parent=styles["Title"],
            fontName=LATIN_FONT_NAME,
            fontSize=19,
            leading=23,
            alignment=1,
            textColor=colors.HexColor("#123B66"),
            spaceAfter=5
        )

        subtitle_style = ParagraphStyle(
            "ConsolidatedSubtitleV15",
            parent=styles["Normal"],
            fontName=LATIN_FONT_NAME,
            fontSize=10,
            leading=14,
            alignment=1,
            textColor=colors.HexColor("#4B5D73"),
            spaceAfter=12
        )

        header_style = ParagraphStyle(
            "ConsolidatedHeaderV15",
            parent=styles["Normal"],
            fontName=LATIN_FONT_NAME,
            fontSize=8.5,
            leading=10,
            alignment=1,
            textColor=colors.white
        )

        cell_style = ParagraphStyle(
            "ConsolidatedCellV15",
            parent=styles["Normal"],
            fontName=LATIN_FONT_NAME,
            fontSize=8.5,
            leading=10,
            alignment=1,
            textColor=colors.HexColor("#17202A")
        )

        name_style = ParagraphStyle(
            "ConsolidatedNameV15",
            parent=cell_style,
            alignment=0
        )

        story = []
        test_name = state.get("name") or "LIVE TEST"

        story.append(
            _HBTextFlowable(
                f"{test_name} - FINAL RESULT",
                font_size=19,
                leading=24,
                color=colors.HexColor("#123B66"),
                align="CENTER",
            )
        )

        story.append(
            _HBTextFlowable(
                f"Questions: {total} | "
                f"Max Marks: {max_marks} | "
                f"Timer: {state['timer']} sec/question | "
                f"Correct: +1 | "
                f"Wrong: -{state['negative']} | "
                f"Participants: {len(rows)}",
                font_size=10,
                leading=14,
                color=colors.HexColor("#4B5D73"),
                align="CENTER",
            )
        )

        table_data = [[
            Paragraph("<b>Rank</b>", header_style),
            Paragraph("<b>Name</b>", header_style),
            Paragraph("<b>Max Marks</b>", header_style),
            Paragraph("<b>Obtained Marks</b>", header_style),
            Paragraph("<b>Right</b>", header_style),
            Paragraph("<b>Wrong</b>", header_style),
            Paragraph("<b>Skip</b>", header_style)
        ]]

        for rank, row in enumerate(rows, start=1):
            # Names may contain Hindi, Latin, or Telegram decorative Unicode.
            # Render them through the mixed-font vector engine so neither side
            # disappears from the result table.
            display_name = pdf_display_name(row["name"])
            name_flow = _HBTextFlowable(
                display_name,
                font_size=8.5,
                leading=10.5,
                color=colors.HexColor("#17202A"),
                align="LEFT",
            )
            table_data.append([
                Paragraph(str(rank), cell_style),
                name_flow,
                Paragraph(str(row["max_marks"]), cell_style),
                Paragraph(f"{row['obtained']:.2f}", cell_style),
                Paragraph(str(row["right"]), cell_style),
                Paragraph(str(row["wrong"]), cell_style),
                Paragraph(str(row["skip"]), cell_style)
            ])

        result_table = Table(
            table_data,
            colWidths=[34, 180, 58, 82, 48, 48, 48],
            repeatRows=1,
            hAlign="CENTER"
        )

        result_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1769AA")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9FB6CC")),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
                colors.white,
                colors.HexColor("#F3F8FC")
            ]),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))

        story.append(result_table)

        doc.build(story)

        # ---------------------------------------------------------
        # PART 2: OFFICIAL ANSWER KEY — TRUE HINDI SHAPING
        # ---------------------------------------------------------
        print("📄 HARFBUZZ ANSWER KEY START")
        _hb_draw_boxed_answer_key(
            state,
            answer_only_path
        )
        print("📄 HARFBUZZ ANSWER KEY CREATED")

        # ---------------------------------------------------------
        # PART 3: MERGE INTO ONE PDF
        # ---------------------------------------------------------
        writer = PdfWriter()

        for part in (result_only_path, answer_only_path):
            reader = PdfReader(part)
            for page in reader.pages:
                writer.add_page(page)

        with open(final_path, "wb") as out:
            writer.write(out)

        if (
            not os.path.exists(final_path)
            or os.path.getsize(final_path) <= 0
        ):
            raise RuntimeError("Final consolidated PDF create nahi hui.")

        print(
            f"📄 FINAL HARFBUZZ PDF CREATED | {final_path}"
        )

        return final_path

    finally:
        for temp in (result_only_path, answer_only_path):
            try:
                if os.path.exists(temp):
                    os.remove(temp)
            except Exception:
                pass



async def finish_group_test(chat_id, stopped=False):
    state = group_tests.get(chat_id)
    if not state or state["finished"]:
        return

    state["finished"] = True

    if state.get("current_message_id"):
        await safe_delete_message(
            chat_id,
            state["current_message_id"]
        )

    participants = sorted(
        set(state.get("participants", set()))
    )

    # Recover participants from SQLite if any raw/runtime vote state
    # was lost during the test.
    try:
        saved_now = load_all_results_for_chat(chat_id)
        recovered_now = sorted({
            int(r["user_id"])
            for r in saved_now
            if int(r["test_id"]) == int(state["test_id"])
            and bool(r.get("answers"))
        })
        if recovered_now:
            participants = sorted(
                set(participants).union(recovered_now)
            )
            for uid in participants:
                if (chat_id, uid) not in user_results:
                    loaded = load_latest_result(chat_id, uid)
                    if loaded:
                        user_results[(chat_id, uid)] = loaded

            print(
                f"💾 FINAL DB RECOVERY | participants={len(participants)}"
            )
    except Exception as e:
        print(f"⚠️ FINAL DB RECOVERY FAILED: {e!r}")

    if stopped:
        await safe_send_message(
            chat_id,
            "🛑 TEST STOPPED\n\n"
            "अब तक के completed/recorded questions का result तैयार किया जा रहा है।\n"
            f"👥 Participants detected: {len(participants)}\n\n"
            "📊 Partial Result PDF तैयार किया जा रहा है..."
        )
    else:
        await safe_send_message(
            chat_id,
            "🏁 TEST FINISHED\n\n"
            "सभी questions पूरे हो गए हैं।\n"
            f"👥 Participants detected: {len(participants)}\n\n"
            "📊 सभी participants का consolidated Result PDF तैयार किया जा रहा है..."
        )

    # ---------------------------------------------------------
    # Persist every participant before generating the master
    # result PDF. This also keeps /mystats and /leaderboard working.
    # ---------------------------------------------------------
    for user_id in participants:
        try:
            if (chat_id, user_id) not in user_results:
                loaded = load_latest_result(chat_id, user_id)
                if loaded:
                    user_results[(chat_id, user_id)] = loaded

            result = user_results.get((chat_id, user_id))
            if not result:
                continue

            result["finished"] = True
            result["finished_at"] = datetime.now().isoformat(
                timespec="seconds"
            )

            try:
                user_name = get_user_name(
                    await app.get_users(user_id)
                )
            except Exception:
                user_name = result.get("user_name") or str(user_id)

            result["user_name"] = user_name

            persist_result(
                chat_id,
                user_id,
                result,
                user_name
            )

        except Exception as e:
            print(
                f"FINAL DB SAVE ERROR user={user_id}: {e!r}"
            )

    # ---------------------------------------------------------
    # Generate ONE consolidated Result.pdf.
    # No individual result message/PDF is sent here.
    # ---------------------------------------------------------
    result_pdf = None

    try:
        print("📄 FINAL PDF START | ReportLab safe mode | no Pillow/RAQM")
        result_pdf = await create_consolidated_result_pdf(
            chat_id,
            state,
            participants
        )

        if result_pdf:
            print(f"📄 FINAL PDF CREATED | {result_pdf}")
            await safe_send_document(
                chat_id,
                result_pdf,
                caption=(
                    ("🛑 PARTIAL RESULT — TEST STOPPED\n\n"
                     if stopped else "📊 FINAL RESULT\n\n")
                    + f"📚 Questions in test: {len(state['questions'])}\n"
                    + f"👥 Participants: {len(participants)}\n"
                    + f"🎯 +1 Correct | -{float(NEGATIVE_MARK):.2f} Wrong\n\n"
                    + "🏆 Rank-wise result + 📘 Official Answer Key — both are in this ONE PDF."
                )
            )

            print(
                f"📄 CONSOLIDATED RESULT PDF SENT | "
                f"participants={len(participants)}"
            )

    except Exception as e:
        print(
            f"❌ CONSOLIDATED RESULT PDF ERROR: {e!r}"
        )

        await safe_send_message(
            chat_id,
            "⚠️ Result PDF बनाने में error आया.\n"
            f"{type(e).__name__}: {e}"
        )

    finally:
        if result_pdf:
            try:
                os.remove(result_pdf)
            except Exception:
                pass

    # ---------------------------------------------------------
    # Cleanup active poll data only after final PDF generation.
    # ---------------------------------------------------------
    for poll_id, poll_data in list(poll_map.items()):
        if poll_data["chat_id"] == chat_id:
            poll_map.pop(poll_id, None)
            pending_votes.pop(poll_id, None)

            task = pending_tasks.get(poll_id)
            if task and not task.done():
                task.cancel()

            pending_tasks.pop(poll_id, None)

    group_tests.pop(chat_id, None)

    print(
        f"🏁 TEST FINISHED | Participants={len(participants)}"
    )


# =========================================================
# FINISH USER + PDF
# =========================================================

async def finish_user_result(chat_id, user_id, state):
    result = user_results.get((chat_id, user_id))
    if not result or result.get("finished"):
        return

    result["finished"] = True
    result["finished_at"] = datetime.now().isoformat(timespec="seconds")

    total = len(state["questions"])
    attempted = len(result["answers"])
    unattempted = total - attempted
    correct = result["correct"]
    wrong = result["wrong"]
    score = result["score"]
    unkeyed = max(0, attempted - correct - wrong)

    percentage = score / total * 100 if total else 0

    elapsed = datetime.now() - result["start_time"]
    seconds_total = max(0, int(elapsed.total_seconds()))
    minutes, seconds = divmod(seconds_total, 60)

    try:
        user = await app.get_users(user_id)
    except Exception:
        user = None

    name = get_user_name(user)

    await safe_send_message(
        chat_id,
        "🏆 TEST COMPLETE\n\n"
        f"👤 {name}\n"
        f"📚 Total: {total}\n"
        f"📝 Attempted: {attempted}\n"
        f"⭕ Unattempted: {unattempted}\n"
        f"✅ Correct: {correct}\n"
        f"❌ Wrong: {wrong}\n\n"
        f"🎯 Score: {score:.2f}/{total}\n"
        f"📊 Percentage: {percentage:.2f}%\n"
        f"⏱ Time: {minutes} min {seconds} sec"
    )

    pdf_path = None
    try:
        pdf_path = create_answer_pdf(
            user_id,
            name,
            result,
            state
        )

        await safe_send_document(
            chat_id,
            pdf_path,
            caption=(
                "📄 ANSWER KEY\n"
                f"🎯 Score: {score:.2f}/{total}\n"
                f"📊 Percentage: {percentage:.2f}%"
            )
        )
    except Exception as e:
        print("PDF ERROR:", repr(e))
    finally:
        if pdf_path:
            try:
                os.remove(pdf_path)
            except Exception:
                pass


# =========================================================
# PDF
# =========================================================

def create_master_answer_pdf(state):
    """Create an answer-key-only PDF independent of participants."""
    filename = (
        "LiveTest_MASTER_"
        + datetime.now().strftime("%Y%m%d_%H%M%S")
        + ".pdf"
    )
    filepath = os.path.join(PDF_FOLDER, filename)

    doc = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        rightMargin=35,
        leftMargin=35,
        topMargin=35,
        bottomMargin=35
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "MasterTitle",
        parent=styles["Title"],
        fontName=FONT_NAME,
        fontSize=18,
        leading=25,
        alignment=1,
        spaceAfter=15
    )

    normal_style = ParagraphStyle(
        "MasterNormal",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=11,
        leading=19,
        spaceAfter=5
    )

    small_style = ParagraphStyle(
        "MasterSmall",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=10,
        leading=17,
        spaceAfter=3
    )

    story = [
        Paragraph("LIVE TEST - MASTER ANSWER KEY", title_style),
        Paragraph(
            f"Total Questions: {len(state['questions'])}",
            normal_style
        ),
        Paragraph(
            f"Timer: {state['timer']} seconds/question",
            normal_style
        ),
        Paragraph(
            f"Negative Marking: -{float(NEGATIVE_MARK):.2f}",
            normal_style
        ),
        Spacer(1, 12)
    ]

    for index, q in enumerate(state["questions"]):
        correct = int(q.get("correct", -1))

        story.append(
            Paragraph(
                f"<b>Q{index + 1}.</b> {pdf_text(q['question'])}",
                normal_style
            )
        )

        keyed = 0 <= correct < len(q["options"])

        for i, option in enumerate(q["options"]):
            prefix = "✓ " if keyed and i == correct else ""
            if keyed and i == correct:
                text = (
                    f"<b>{prefix}{option_letter(i)}) "
                    f"{pdf_text(option)}</b>"
                )
            else:
                text = f"{option_letter(i)}) {pdf_text(option)}"

            story.append(Paragraph(text, small_style))

        if keyed:
            answer_text = (
                f"<b>Correct Answer:</b> "
                f"{option_letter(correct)} - "
                f"{pdf_text(q['options'][correct])}"
            )
        else:
            answer_text = (
                "<b>Correct Answer:</b> "
                "NOT AVAILABLE (SOURCE REGULAR POLL)"
            )

        story.append(Paragraph(answer_text, small_style))
        story.append(Spacer(1, 8))

    doc.build(story)

    if not os.path.exists(filepath) or os.path.getsize(filepath) <= 0:
        raise RuntimeError("Master PDF create nahi hui.")

    return filepath


def create_answer_pdf(user_id, user_name, result, state):
    filename = (
        f"LiveTest_{user_id}_"
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    )
    filepath = os.path.join(PDF_FOLDER, filename)

    doc = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        rightMargin=35,
        leftMargin=35,
        topMargin=35,
        bottomMargin=35
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "Step10Title",
        parent=styles["Title"],
        fontName=FONT_NAME,
        fontSize=18,
        leading=25,
        alignment=1,
        spaceAfter=15
    )

    heading_style = ParagraphStyle(
        "Step10Heading",
        parent=styles["Heading2"],
        fontName=FONT_NAME,
        fontSize=14,
        leading=21,
        spaceBefore=8,
        spaceAfter=8
    )

    normal_style = ParagraphStyle(
        "Step10Normal",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=11,
        leading=19,
        spaceAfter=5
    )

    small_style = ParagraphStyle(
        "Step10Small",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=10,
        leading=17,
        spaceAfter=3
    )

    story = [
        Paragraph("LIVE TEST - ANSWER KEY", title_style),
        Paragraph(f"User: {pdf_text(user_name)}", normal_style),
        Paragraph(f"User ID: {user_id}", normal_style),
        Spacer(1, 10)
    ]

    total = len(state["questions"])
    attempted = len(result["answers"])
    unattempted = total - attempted
    score = result["score"]
    percentage = score / total * 100 if total else 0

    summary = [
        ["Total Questions", str(total)],
        ["Attempted", str(attempted)],
        ["Unattempted", str(unattempted)],
        ["Correct", str(result["correct"])],
        ["Wrong", str(result["wrong"])],
        ["Score", f"{score:.2f}/{total}"],
        ["Percentage", f"{percentage:.2f}%"]
    ]

    summary_table = Table(summary, colWidths=[210, 170])
    summary_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.6, colors.grey),
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("LEADING", (0, 0), (-1, -1), 16),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    story.append(summary_table)
    story.append(Spacer(1, 20))
    story.append(Paragraph("DETAILED ANSWER KEY", heading_style))

    for index, q in enumerate(state["questions"]):
        answer = result["answers"].get(index)
        correct_option = int(q.get("correct", -1))
        options = q["options"]
        keyed = 0 <= correct_option < len(options)

        block = [
            Paragraph(
                f"<b>Q{index+1}.</b> {pdf_text(q['question'])}",
                normal_style
            )
        ]

        for i, option in enumerate(options):
            prefix = "✓ " if keyed and i == correct_option else ""
            text = (
                f"<b>{prefix}{option_letter(i)}) "
                f"{pdf_text(option)}</b>"
                if keyed and i == correct_option
                else f"{prefix}{option_letter(i)}) {pdf_text(option)}"
            )
            block.append(Paragraph(text, small_style))

        if answer is None:
            block.append(
                Paragraph(
                    "<b>Your Answer:</b> UNATTEMPTED | Marks: 0",
                    small_style
                )
            )
        else:
            selected = answer["selected"]
            selected_text = options[selected]

            if not keyed:
                block.append(
                    Paragraph(
                        f"<b>Your Answer:</b> "
                        f"{option_letter(selected)} - "
                        f"{pdf_text(selected_text)} | UNKEYED | Marks: 0",
                        small_style
                    )
                )
            elif selected == correct_option:
                block.append(
                    Paragraph(
                        f"<b>Your Answer:</b> "
                        f"{option_letter(selected)} - "
                        f"{pdf_text(selected_text)} | +1",
                        small_style
                    )
                )
            else:
                block.append(
                    Paragraph(
                        f"<b>Your Answer:</b> "
                        f"{option_letter(selected)} - "
                        f"{pdf_text(selected_text)} | "
                        f"-{state['negative']}",
                        small_style
                    )
                )

        if keyed:
            correct_text = (
                f"<b>Correct Answer:</b> "
                f"{option_letter(correct_option)} - "
                f"{pdf_text(options[correct_option])}"
            )
        else:
            correct_text = (
                "<b>Correct Answer:</b> "
                "NOT AVAILABLE (SOURCE REGULAR POLL)"
            )

        block.append(Paragraph(correct_text, small_style))

        table = Table([[block]], colWidths=[380])
        table.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.8, colors.grey),
            ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ("RIGHTPADDING", (0, 0), (-1, -1), 10),
            ("TOPPADDING", (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP")
        ]))

        story.append(KeepTogether(table))
        story.append(Spacer(1, 10))

    doc.build(story)

    if not os.path.exists(filepath) or os.path.getsize(filepath) <= 0:
        raise RuntimeError("PDF create nahi hui.")

    return filepath


# Prevent the normal MessageHandler and the raw-update fallback
# from replying twice to the same command message.
processed_command_messages = set()
command_dispatch_lock = asyncio.Lock()


# =========================================================
# COMMANDS: MYSTATS + LEADERBOARD
# =========================================================
# BOT COMMAND DISPATCHER:
# Commands are handled through one plain-text dispatcher.

async def handle_mystats_command(client, message, args):
    if not is_group_chat(message):
        return

    chat_id = int(message.chat.id)

    # /mystats <user_id>
    target_user_id = None
    if args:
        if args[0].lstrip("-").isdigit():
            target_user_id = int(args[0])

    # Reply-to-user support
    if target_user_id is None:
        try:
            reply = message.reply_to_message
            if reply and reply.from_user:
                target_user_id = int(reply.from_user.id)
        except Exception:
            pass

    # Normal /mystats = command sender
    if target_user_id is None:
        try:
            if message.from_user:
                target_user_id = int(message.from_user.id)
        except Exception:
            pass

    # USERBOT outgoing command: from_user may be None.
    if target_user_id is None:
        try:
            target_user_id = int((await app.get_me()).id)
        except Exception:
            target_user_id = 0

    print(
        f"📊 MYSTATS DISPATCH | chat={chat_id} "
        f"| user={target_user_id} "
        f"| text={message.text!r} "
        f"| outgoing={getattr(message, 'outgoing', None)}"
    )

    if not target_user_id:
        await safe_send_message(
            chat_id,
            "❌ User ID detect nahi hua."
        )
        return

    result = load_latest_result(chat_id, target_user_id)

    if not result:
        await safe_send_message(
            chat_id,
            "ℹ️ Is user ka koi saved result nahi mila.\n\n"
            "Agar aap USERBOT se command chala rahe hain to "
            "test dene wale user ka ID use karein:\n"
            "/mystats USER_ID"
        )
        return

    user_results[(chat_id, target_user_id)] = result

    imported = get_imported_test(result["test_id"])
    total = (
        len(imported["questions"])
        if imported
        else len(result["answers"])
    )

    attempted = len(result["answers"])
    unattempted = max(0, total - attempted)
    percentage = (
        result["score"] / total * 100
        if total else 0
    )

    try:
        name = get_user_name(
            await app.get_users(target_user_id)
        )
    except Exception:
        name = result.get("user_name") or str(target_user_id)

    await safe_send_message(
        chat_id,
        "📊 YOUR TEST STATS\n\n"
        f"👤 {name}\n"
        f"🆔 User ID: {target_user_id}\n"
        f"📝 Test ID: {result['test_id']}\n\n"
        f"📚 Total: {total}\n"
        f"📝 Attempted: {attempted}\n"
        f"⭕ Unattempted: {unattempted}\n"
        f"✅ Correct: {result['correct']}\n"
        f"❌ Wrong: {result['wrong']}\n\n"
        f"🎯 Score: {result['score']:.2f}/{total}\n"
        f"📊 Percentage: {percentage:.2f}%"
    )


async def handle_leaderboard_command(client, message, args):
    if not is_group_chat(message):
        return

    chat_id = int(message.chat.id)

    # Optional: /leaderboard <test_id>
    test_id = None
    if args and args[0].isdigit():
        test_id = int(args[0])

    if test_id is None:
        test_id = get_latest_result_test_id(chat_id)

    print(
        f"🏆 LEADERBOARD DISPATCH | chat={chat_id} "
        f"| test={test_id} "
        f"| text={message.text!r} "
        f"| outgoing={getattr(message, 'outgoing', None)}"
    )

    if test_id is None:
        await safe_send_message(
            chat_id,
            "📊 अभी कोई saved test result नहीं है."
        )
        return

    rows = [
        r for r in load_results_for_chat_test(
            chat_id,
            test_id
        )
        if r.get("answers")
    ]

    if not rows:
        await safe_send_message(
            chat_id,
            "📊 इस test की leaderboard अभी खाली है.\n\n"
            f"🆔 Test ID: {test_id}"
        )
        return

    rows.sort(
        key=lambda r: (
            float(r.get("score", 0.0)),
            int(r.get("correct", 0)),
            len(r.get("answers", {}))
        ),
        reverse=True
    )

    test = get_imported_test(test_id)
    total = (
        len(test["questions"])
        if test
        else max(
            [len(r["answers"]) for r in rows] or [0]
        )
    )

    output = (
        "🏆 LIVE TEST LEADERBOARD\n\n"
        f"🆔 Test ID: {test_id}\n"
        f"📚 Total: {total}\n"
        f"👥 Participants: {len(rows)}\n\n"
    )

    for pos, result in enumerate(rows[:20], 1):
        try:
            name = get_user_name(
                await app.get_users(result["user_id"])
            )
        except Exception:
            name = result.get("user_name") or str(
                result["user_id"]
            )

        percentage = (
            result["score"] / total * 100
            if total else 0
        )

        attempted = len(result.get("answers", {}))
        unkeyed = max(
            0,
            attempted
            - int(result.get("correct", 0))
            - int(result.get("wrong", 0))
        )

        output += (
            f"{pos}. {name}\n"
            f"   🆔 {result['user_id']}\n"
            f"   🎯 {result['score']:.2f}/{total} "
            f"({percentage:.2f}%)\n"
            f"   ✅ {result['correct']} | "
            f"❌ {result['wrong']} | "
            f"⚪ {unkeyed} | "
            f"📝 {attempted}\n\n"
        )

    await safe_send_message(
        chat_id,
        output
    )


async def handle_stoptest_command(client, message):
    if not message or not message.chat or not is_group_chat(message):
        return

    chat_id = int(message.chat.id)
    user_id = await resolve_command_user_id(message)

    print(
        f"🛑 STOPTEST DISPATCH | chat={chat_id} "
        f"| user={user_id} "
        f"| text={getattr(message, 'text', '')!r} "
        f"| outgoing={getattr(message, 'outgoing', None)}"
    )


    if not await is_admin(chat_id, user_id):
        await safe_send_message(
            chat_id,
            f"⚠️ /stoptest sirf group admin use kare.\n🆔 Detected User ID: {user_id}"
        )
        return

    state = group_tests.get(chat_id)
    if not state:
        await safe_send_message(chat_id, "ℹ️ Koi active test nahi hai.")
        return

    runner = state.get("runner")
    if runner and not runner.done():
        runner.cancel()

    await finish_group_test(chat_id, stopped=True)


# =========================================================
# EXPLICIT COMMAND DISPATCHER
# =========================================================
# Telegram group/supergroup commands are routed explicitly so that
# /mystats, /leaderboard, /lb, /test, /import and /stoptest are
# reliably handled even when the message is sent by a user account.

@app.on_message(filters.command("mystats"))
async def mystats_command_handler(client, message):
    try:
        args = list(message.command[1:]) if getattr(message, "command", None) else []
    except Exception:
        args = []
    await handle_mystats_command(client, message, args)


@app.on_message(filters.command(["leaderboard", "lb"]))
async def leaderboard_command_handler(client, message):
    try:
        args = list(message.command[1:]) if getattr(message, "command", None) else []
    except Exception:
        args = []
    await handle_leaderboard_command(client, message, args)


@app.on_message(filters.command("test"))
async def test_command_handler(client, message):
    await start_test(client, message)


@app.on_message(filters.command("import"))
async def import_command_handler(client, message):
    await import_command(client, message)


@app.on_message(filters.command("stoptest"))
async def stoptest_command_handler(client, message):
    await stop_test(client, message)


@app.on_message(filters.command("teststatus"))
async def teststatus_command_handler(client, message):
    try:
        await test_status(client, message)
    except NameError:
        # Keep compatibility if this command is not implemented in this build.
        await safe_send_message(message.chat.id, "ℹ️ /teststatus handler available nahi hai.")


@app.on_message(filters.command("start"))
async def start_command_handler(client, message):
    await start_command(client, message)

# =========================================================
# BOT STARTUP
# =========================================================

async def step10_main():
    global SELF_USER_ID

    await app.start()

    try:
        me = await app.get_me()
        SELF_USER_ID = int(me.id)
    except Exception:
        SELF_USER_ID = 0

    print("✅ TELEGRAM BOT CONNECTED")
    print(f"🤖 Bot ID: {SELF_USER_ID}")
    print("🛡 MODE: REAL BOT ACCOUNT (no user session / no selfbot)")
    print("🛰 Native command handlers ACTIVE")
    print("🔗 HYBRID SOURCE READER: ENABLED")
    print(f"🔐 SOURCE_READER_USER_IDS: {sorted(SOURCE_READER_USER_IDS)}")
    print("🗳 Poll votes: UpdateMessagePollVote / PollAnswer")
    print("📥 /import: bot must have access to the source chat/channel")
    print("👮 Control access: ANY GROUP ADMIN/OWNER")
    print("   /import")
    print("   /test")
    print("   /stoptest")
    print("   /mystats")
    print("   /leaderboard")
    print("   /lb")

    try:
        await idle()
    finally:
        await app.stop()


# =========================================================
# HYBRID IMPORT PACKAGE
# =========================================================

@app.on_message(filters.document)
async def hybrid_import_package(client, message):
    if not HYBRID_MODE or not message.from_user:
        return

    sender_id = int(message.from_user.id)
    if not SOURCE_READER_USER_IDS or sender_id not in SOURCE_READER_USER_IDS:
        return

    caption = (message.caption or "").strip()
    if not caption.startswith("HYBRID_IMPORT"):
        return

    try:
        status = await safe_send_message(
            message.chat.id,
            "📥 Hybrid import package receive ho raha hai..."
        )
        local_path = await client.download_media(
            message,
            file_name=f"/home/container/hybrid_import_{message.id}.json"
        )
        with open(local_path, "r", encoding="utf-8") as f:
            package = json.load(f)

        target_chat = int(package["target_chat_id"])
        source_chat = package.get("source_chat", "hybrid-user-session")
        source_start = int(package.get("source_start_message", 0))
        questions = package.get("questions") or []
        timer = int(package.get("timer") or QUESTION_TIME)
        timer = max(5, min(timer, 600))

        if not questions:
            raise ValueError("Import package me questions nahi hain.")
        if len(questions) > MAX_QUESTIONS:
            raise ValueError(f"Maximum {MAX_QUESTIONS} questions allowed hain.")

        # Sanitize and validate every question before inserting it.
        clean_questions = []
        for q in questions:
            opts = [str(x)[:100] for x in (q.get("options") or [])[:10]]
            if len(opts) < 2:
                continue
            correct = int(q.get("correct", -1))
            if not 0 <= correct < len(opts):
                correct = -1
            clean_questions.append({
                "question": str(q.get("question", ""))[:255],
                "options": opts,
                "correct": correct,
                "source_message_id": int(q.get("source_message_id", 0)),
                "source_timer": q.get("source_timer")
            })

        if not clean_questions:
            raise ValueError("Koi valid poll question nahi mila.")

        test_id = save_imported_test(
            f"Hybrid Imported Test {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            source_chat,
            source_start,
            timer,
            NEGATIVE_MARK,
            clean_questions
        )

        # Tell the target group, not the source-reader's private chat.
        await safe_send_message(
            target_chat,
            "✅ HYBRID TEST IMPORTED\\n\\n"
            f"🆔 Test ID: {test_id}\\n"
            f"📚 Polls: {len(clean_questions)}\\n"
            f"⏱ Timer: {timer}s\\n"
            f"❌ Negative: -{NEGATIVE_MARK:.2f}\\n\\n"
            "Ab /test bhejein."
        )
        await safe_delete_message(message.chat.id, status.id)
        try:
            os.remove(local_path)
        except Exception:
            pass

    except Exception as e:
        print("HYBRID IMPORT ERROR:", repr(e))
        try:
            await safe_send_message(
                message.chat.id,
                f"❌ HYBRID IMPORT FAILED\\n\\n{e}"
            )
        except Exception:
            pass


# =========================================================
# /TESTSTATUS
# =========================================================

@app.on_message(filters.command("teststatus"))
async def test_status(client, message):
    if not is_group_chat(message):
        return

    state = group_tests.get(message.chat.id)

    if not state:
        await safe_send_message(
            message.chat.id,
            "ℹ️ Koi active test nahi hai."
        )
        return

    current = state["current_index"] + 1

    await safe_send_message(
        message.chat.id,
        "📊 TEST STATUS\n\n"
        "🟢 ACTIVE\n"
        f"📚 Questions: {len(state['questions'])}\n"
        f"▶️ Current: {current}/{len(state['questions'])}\n"
        f"⏱ Timer: {state['timer']} sec\n"
        f"👥 Participants: {len(state['participants'])}"
    )


# =========================================================
# /STOPTEST
# =========================================================
# Incoming /stoptest compatibility wrapper.
# USERBOT outgoing /stoptest is handled by the common dispatcher.
# =========================================================

async def stop_test(client, message):
    await handle_stoptest_command(client, message)


# =========================================================
# RAW UPDATE HANDLER
# =========================================================

# =========================================================
# STARTUP
# =========================================================

print("=" * 60)
print("🚀 LIVE TEST BOT - REAL TELEGRAM BOT - NEGATIVE MARKING 0.33")
print("ONE QUESTION AT A TIME")
print("=" * 60)
print("MODE: TELEGRAM BOT (bot_token)")
print(f"Session: {SESSION_NAME}")
print(f"Question Timer: {QUESTION_TIME}s")
print(f"Negative Marking: -{NEGATIVE_MARK:.2f}")
print(f"PDF Folder: {PDF_FOLDER}")
print(f"Hindi Font: {FONT_PATH}")
print("Bot session: ENABLED")
print("Reverse order: ENABLED")
print("One question at a time: ENABLED")
print("Previous poll delete: ENABLED")
print("Final poll delete: ENABLED")
print("Native quiz poll: ENABLED")
print("Anonymous: FALSE")
print("Result + PDF answer key: ENABLED")
print("PDF MODE: TRUE DEVANAGARI HARFBUZZ VECTOR SHAPING (NO PILLOW/RAQM)")
print("BOT COMMAND HANDLERS: ENABLED")
print("COMMAND POLLER: DISABLED (real Bot updates)")
print("  - /mystats")
print("  - /mystats <user_id>")
print("  - /leaderboard")
print("  - /leaderboard <test_id>")
print("  - /lb")
print("  - /import <link_or_msg_id> <questions> [timer|auto]")
print("  - /test [test_id]")
print("=" * 60)

app.run(step10_main())