import os
import re
import json
import asyncio
import tempfile
from datetime import datetime

from pyrogram import Client, filters, idle
from pyrogram.enums import PollType, ChatType, ChatMemberStatus

# =========================================================
# HYBRID SOURCE READER
# =========================================================
# This process uses ONE normal Telegram user session only for reading source
# chats that the user account already has access to.
#
# It does NOT send live test polls and does NOT calculate results.
# The real Telegram Bot handles the live test.
#
# Set:
#   API_ID
#   API_HASH
#   SOURCE_BOT_USERNAME   e.g. @YourLiveTestBot
#   SOURCE_ALLOWED_USER_IDS  comma-separated Telegram user IDs
#
# First run creates the user session. Keep this process on a host that allows
# user sessions; do not run it on a host whose ToS prohibits selfbots.
# =========================================================

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_USERNAME = os.getenv("SOURCE_BOT_USERNAME", "").strip()
SESSION_NAME = os.getenv("SOURCE_SESSION_NAME", "live_test_source_reader")
WORKDIR = os.getenv("SOURCE_WORKDIR", "/home/container")

MAX_QUESTIONS = int(os.getenv("MAX_QUESTIONS", "200"))
IMPORT_SCAN_LIMIT = int(os.getenv("IMPORT_SCAN_LIMIT", "2000"))

allowed_raw = os.getenv("SOURCE_ALLOWED_USER_IDS", "").strip()
ALLOWED_USER_IDS = {
    int(x.strip()) for x in allowed_raw.split(",")
    if x.strip().lstrip("-").isdigit()
}

if not API_ID or not API_HASH:
    raise RuntimeError("API_ID/API_HASH missing.")

if not BOT_USERNAME:
    raise RuntimeError(
        "SOURCE_BOT_USERNAME missing. Example: @YourLiveTestBot"
    )

app = Client(
    SESSION_NAME,
    api_id=API_ID,
    api_hash=API_HASH,
    workdir=WORKDIR
)


def parse_source_reference(reference, current_chat_id):
    reference = reference.strip().rstrip("/")

    if reference.isdigit():
        return int(current_chat_id), int(reference)

    match = re.match(r"^https?://t\.me/c/(\d+)/(\d+)$", reference)
    if match:
        return int("-100" + match.group(1)), int(match.group(2))

    match = re.match(r"^https?://t\.me/([^/]+)/(\d+)$", reference)
    if match:
        return match.group(1), int(match.group(2))

    raise ValueError("Invalid Telegram message link/message ID.")


def extract_poll_from_message(message):
    """
    Import ALL poll types:
      - quiz
      - regular
      - anonymous
      - non-anonymous
      - closed
      - open

    Important:
      Regular polls do not carry a correct answer in Telegram. They are
      imported with correct=-1 (UNKEYED). Quiz polls retain correct_option_id.
    """
    if not message or not message.poll:
        return None

    poll = message.poll
    options = [str(x.text) for x in (poll.options or [])]

    if len(options) < 2:
        return None

    question = str(poll.question or "").strip()
    if not question:
        return None

    correct = -1
    if getattr(poll, "type", None) == PollType.QUIZ:
        value = getattr(poll, "correct_option_id", None)
        if value is not None:
            try:
                value = int(value)
                if 0 <= value < len(options):
                    correct = value
            except Exception:
                pass

    source_timer = getattr(poll, "open_period", None)

    return {
        "question": question[:255],
        "options": [x[:100] for x in options[:10]],
        "correct": correct,
        "source_message_id": int(message.id),
        "source_timer": int(source_timer) if source_timer else None,
        "poll_type": str(getattr(poll, "type", "")),
        "is_closed": bool(getattr(poll, "is_closed", False)),
        "is_anonymous": bool(getattr(poll, "is_anonymous", False)),
    }


async def import_polls(source_chat, start_message_id, question_count):
    if not 1 <= question_count <= MAX_QUESTIONS:
        raise ValueError(
            f"Questions 1 se {MAX_QUESTIONS} ke beech hone chahiye."
        )

    questions = []
    current_id = int(start_message_id)
    end_limit = current_id + IMPORT_SCAN_LIMIT

    while len(questions) < question_count and current_id < end_limit:
        batch_end = min(current_id + 100, end_limit)
        ids = list(range(current_id, batch_end))

        try:
            messages = await app.get_messages(source_chat, ids)
        except Exception as e:
            raise RuntimeError(
                "Source messages read nahi ho paaye. "
                "User account ko source chat/channel access hona chahiye.\n"
                f"Telegram error: {e}"
            )

        for message in messages:
            q = extract_poll_from_message(message)
            if q:
                questions.append(q)
                print(
                    f"📥 Imported Q{len(questions)} | "
                    f"Message={message.id} | "
                    f"type={q['poll_type']} | "
                    f"closed={q['is_closed']} | "
                    f"anonymous={q['is_anonymous']} | "
                    f"correct={q['correct']}"
                )
                if len(questions) >= question_count:
                    break

        current_id = batch_end

    if not questions:
        raise RuntimeError(
            "Starting message se koi readable Poll nahi mila."
        )

    if len(questions) < question_count:
        raise RuntimeError(
            f"Sirf {len(questions)} Poll mile, "
            f"{question_count} requested the."
        )

    return questions


async def is_group_admin(chat_id, user_id):
    try:
        member = await app.get_chat_member(chat_id, user_id)
        return member.status in (
            ChatMemberStatus.OWNER,
            ChatMemberStatus.ADMINISTRATOR,
        )
    except Exception:
        return False


async def send_package(target_chat_id, source_chat, source_start, timer, questions):
    package = {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "target_chat_id": int(target_chat_id),
        "source_chat": str(source_chat),
        "source_start_message": int(source_start),
        "timer": int(timer),
        "questions": questions,
    }

    fd, path = tempfile.mkstemp(
        prefix="hybrid_import_",
        suffix=".json"
    )
    os.close(fd)

    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(package, f, ensure_ascii=False, separators=(",", ":"))

        caption = (
            "HYBRID_IMPORT\n"
            f"target={target_chat_id}\n"
            f"questions={len(questions)}"
        )

        await app.send_document(
            BOT_USERNAME,
            document=path,
            caption=caption
        )
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


@app.on_message(filters.command("import"))
async def import_command(client, message):
    # Only the configured source-reader users may request imports.
    uid = int(message.from_user.id) if message.from_user else 0

    if ALLOWED_USER_IDS and uid not in ALLOWED_USER_IDS:
        return

    # Commands should be issued from the TARGET group where the live test
    # will eventually run. Source can be any accessible group/channel.
    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    if not await is_group_admin(message.chat.id, uid):
        await message.reply_text(
            "⚠️ /import sirf target group ke admin/owner ke liye hai."
        )
        return

    args = message.command
    if len(args) < 2:
        await message.reply_text(
            "❌ Format:\n"
            "/import <source_link_or_msg_id> <questions> [timer|auto]\n\n"
            "Example:\n"
            "/import https://t.me/channel/12345 50 auto"
        )
        return

    try:
        count = int(args[2]) if len(args) >= 3 else 50
        timer_arg = args[3].lower() if len(args) >= 4 else "auto"

        if timer_arg == "auto":
            timer = None
        else:
            timer = int(timer_arg)
            if not 5 <= timer <= 600:
                raise ValueError("Timer 5-600 seconds ke beech hona chahiye.")

        source_chat, start_id = parse_source_reference(
            args[1],
            message.chat.id
        )

        await message.reply_text(
            "⏳ Source user account se Polls read ho rahe hain...\n"
            "Quiz + Regular + Anonymous + Closed supported."
        )

        questions = await import_polls(
            source_chat,
            start_id,
            count
        )

        if timer is None:
            timer = questions[0].get("source_timer") or 30

        timer = max(5, min(int(timer), 600))

        await send_package(
            message.chat.id,
            source_chat,
            start_id,
            timer,
            questions
        )

        await message.reply_text(
            "📤 Import package live-test bot ko bhej diya gaya.\n"
            f"📚 Questions: {len(questions)}\n"
            f"⏱ Timer: {timer}s"
        )

    except Exception as e:
        print("❌ SOURCE IMPORT ERROR:", repr(e))
        await message.reply_text(
            f"❌ IMPORT FAILED\n\n{e}"
        )


async def main():
    await app.start()
    me = await app.get_me()
    print("=" * 60)
    print("🔗 HYBRID SOURCE READER CONNECTED")
    print(f"👤 Source User ID: {me.id}")
    print(f"🤖 Destination Bot: {BOT_USERNAME}")
    print(f"🔐 Allowed User IDs: {sorted(ALLOWED_USER_IDS)}")
    print("📥 Reads: Quiz + Regular + Anonymous + Closed Polls")
    print("📤 Sends JSON package to the real Bot")
    print("=" * 60)

    try:
        await idle()
    finally:
        await app.stop()


app.run(main())
