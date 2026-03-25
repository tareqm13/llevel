import os
import json
import logging
import asyncio
import tempfile
from datetime import time as dtime, timezone

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes,
)

try:
    from notion_client import Client as NotionClient
    NOTION_AVAILABLE = True
except ImportError:
    NOTION_AVAILABLE = False

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    from groq import Groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN              = os.environ.get("BOT_TOKEN", "")
NOTION_TOKEN       = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
REMINDER_CHAT_ID   = os.environ.get("REMINDER_CHAT_ID", "")
REMINDER_TIMES_RAW = os.environ.get("REMINDER_TIMES", "09:00,14:00,19:00")
# Times are UTC. For CET (UTC+1) subtract 1h. For CEST (UTC+2) subtract 2h.

# ── Notion helpers ────────────────────────────────────────────────────────────
_PRIORITY_ORDER    = {"high": 0, "medium": 1, "low": 2}
_title_prop_cache  = None   # cached title property name


def _notion_client() -> NotionClient:
    return NotionClient(auth=NOTION_TOKEN)


def _get_title_property_name() -> str:
    """Discover the title-type property name from the DB schema (cached)."""
    global _title_prop_cache
    if _title_prop_cache:
        return _title_prop_cache
    db = _notion_client().databases.retrieve(database_id=NOTION_DATABASE_ID)
    for name, prop in db.get("properties", {}).items():
        if prop.get("type") == "title":
            _title_prop_cache = name
            return name
    _title_prop_cache = "Name"
    return "Name"


def fetch_notion_todos() -> list:
    """Fetch all open todos sorted by priority. Synchronous."""
    if not NOTION_AVAILABLE:
        raise RuntimeError("notion-client not installed")
    results = _notion_client().databases.query(
        database_id=NOTION_DATABASE_ID,
        page_size=50,
    )
    todos = []
    for page in results.get("results", []):
        props = page.get("properties", {})
        title = ""
        for prop in props.values():
            if prop.get("type") == "title":
                title = "".join(p.get("plain_text", "") for p in prop.get("title", [])).strip()
                break
        if not title:
            continue
        priority = None
        for name, prop in props.items():
            if name.lower() == "priority" and prop.get("type") == "select":
                sel = prop.get("select")
                if sel:
                    priority = sel.get("name", "").lower()
                break
        todos.append({"title": title, "priority": priority})
    todos.sort(key=lambda t: _PRIORITY_ORDER.get(t["priority"] or "", 99))
    return todos


def create_notion_todo(title: str, priority: str) -> None:
    """Create a new todo page in Notion. Synchronous."""
    if not NOTION_AVAILABLE:
        raise RuntimeError("notion-client not installed")
    title_prop = _get_title_property_name()
    properties = {
        title_prop: {"title": [{"text": {"content": title}}]},
    }
    if priority:
        properties["Priority"] = {"select": {"name": priority.capitalize()}}
    _notion_client().pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties=properties,
    )


# ── AI helpers ────────────────────────────────────────────────────────────────
def parse_todo_with_groq(text: str) -> dict:
    """
    Use Groq llama to extract {title, priority} from a user message.
    Falls back to {title: text, priority: "medium"} on any error.
    """
    if not GROQ_AVAILABLE or not GROQ_API_KEY:
        return {"title": text.strip(), "priority": "medium"}
    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=100,
            temperature=0,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract a todo task from the user's message. "
                        "Return JSON only, no other text: "
                        "{\"title\": \"...\", \"priority\": \"high|medium|low\"}\n"
                        "Priority rules:\n"
                        "- high: urgent, wichtig, dringend, asap, sofort, unbedingt\n"
                        "- low: irgendwann, später, someday, wenn Zeit\n"
                        "- medium: everything else\n"
                        "Clean up the title — remove filler words, keep it concise."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        title = str(parsed.get("title", text)).strip() or text.strip()
        priority = str(parsed.get("priority", "medium")).lower()
        if priority not in ("high", "medium", "low"):
            priority = "medium"
        return {"title": title, "priority": priority}
    except Exception as e:
        logger.error(f"Groq parse error: {e}")
        return {"title": text.strip(), "priority": "medium"}


def transcribe_voice_groq(file_path: str) -> str:
    """Transcribe an audio file using Groq Whisper API. Synchronous."""
    client = Groq(api_key=GROQ_API_KEY)
    with open(file_path, "rb") as f:
        result = client.audio.transcriptions.create(
            model="whisper-large-v3-turbo",
            file=f,
        )
    return result.text.strip()


def _format_plain_list(todos: list) -> str:
    if not todos:
        return "✅ Keine offenen Todos – alles erledigt!"
    lines = ["📋 *Deine offenen Todos:*"]
    for t in todos:
        p = t.get("priority")
        emoji = {"high": " 🔴", "medium": " 🟡", "low": " 🟢"}.get(p, "")
        lines.append(f"• {t['title']}{emoji}")
    return "\n".join(lines)


def summarize_with_claude(todos: list) -> str:
    """Use Claude Haiku to write a motivating reminder. Synchronous."""
    if not todos:
        return "✅ Keine offenen Todos – alles erledigt! Gute Arbeit!"
    if not ANTHROPIC_AVAILABLE or not ANTHROPIC_API_KEY:
        return _format_plain_list(todos)
    todo_text = "\n".join(
        f"- {t['title']}" + (f" [Priorität: {t['priority']}]" if t.get("priority") else "")
        for t in todos
    )
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            system=(
                "Du bist ein fokussierter Produktivitäts-Coach. "
                "Schreib eine kurze, motivierende Erinnerung. "
                "Nur Klartext, keine Markdown-Header. "
                "Antworte in der Sprache der Aufgaben-Titel."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Offene Todos:\n\n{todo_text}\n\n"
                    "Schreib eine knappe Erinnerung (3-5 Punkte max), "
                    "hebe die wichtigsten Aufgaben hervor, "
                    "ende mit einem kurzen motivierenden Satz. Max 200 Wörter."
                ),
            }],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return _format_plain_list(todos)


# ── Scheduled reminder ────────────────────────────────────────────────────────
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    if not REMINDER_CHAT_ID:
        return
    try:
        todos = await asyncio.to_thread(fetch_notion_todos)
    except Exception as e:
        logger.error(f"Notion fetch failed: {e}")
        await context.bot.send_message(
            chat_id=REMINDER_CHAT_ID,
            text="⚠️ Konnte Todos nicht aus Notion laden.",
        )
        return
    message = await asyncio.to_thread(summarize_with_claude, todos)
    await context.bot.send_message(
        chat_id=REMINDER_CHAT_ID, text=message, parse_mode="Markdown"
    )


# ── Command handlers ──────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Todo-Reminder Bot*\n\n"
        "Schreib mir einfach eine Aufgabe — als Text oder Sprachnachricht.\n"
        "Ich erkenne die Priorität automatisch und trage sie in Notion ein.\n\n"
        "*Commands:*\n"
        "/todos – Offene Todos anzeigen\n"
        "/reminders – Erinnerungszeiten anzeigen\n",
        parse_mode="Markdown",
    )


async def todos_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Lade Todos...")
    try:
        todos = await asyncio.to_thread(fetch_notion_todos)
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {e}")
        return
    await update.message.reply_text(_format_plain_list(todos), parse_mode="Markdown")


async def reminders_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entries = [t.strip() for t in REMINDER_TIMES_RAW.split(",") if t.strip()]
    lines = ["⏰ *Geplante Erinnerungen (UTC):*"] + [f"  • {t} Uhr" for t in entries]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ── Message handlers ──────────────────────────────────────────────────────────
_PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


async def _add_todo_from_text(text: str, update: Update) -> None:
    """Parse text, create Notion todo, reply with confirmation."""
    parsed = await asyncio.to_thread(parse_todo_with_groq, text)
    title    = parsed["title"]
    priority = parsed["priority"]
    emoji    = _PRIORITY_EMOJI.get(priority, "")
    await asyncio.to_thread(create_notion_todo, title, priority)
    await update.message.reply_text(
        f"✅ *{title}* {emoji} wurde zu Notion hinzugefügt!",
        parse_mode="Markdown",
    )


async def handle_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return
    try:
        await _add_todo_from_text(text, update)
    except Exception as e:
        logger.error(f"handle_text_message error: {e}")
        await update.message.reply_text(f"❌ Fehler beim Hinzufügen: {e}")


async def handle_voice_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not GROQ_AVAILABLE or not GROQ_API_KEY:
        await update.message.reply_text(
            "❌ GROQ_API_KEY nicht gesetzt – Sprachnachrichten nicht verfügbar."
        )
        return

    msg = await update.message.reply_text("🎙 Verarbeite Sprachnachricht...")

    # Download voice file to a temp file
    voice = update.message.voice
    tg_file = await ctx.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    await tg_file.download_to_drive(tmp_path)

    try:
        transcribed = await asyncio.to_thread(transcribe_voice_groq, tmp_path)
    except Exception as e:
        logger.error(f"Transcription error: {e}")
        await msg.edit_text(f"❌ Transkription fehlgeschlagen: {e}")
        return
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not transcribed:
        await msg.edit_text("❌ Konnte die Sprachnachricht nicht verstehen.")
        return

    try:
        parsed = await asyncio.to_thread(parse_todo_with_groq, transcribed)
        title    = parsed["title"]
        priority = parsed["priority"]
        emoji    = _PRIORITY_EMOJI.get(priority, "")
        await asyncio.to_thread(create_notion_todo, title, priority)
        await msg.edit_text(
            f"✅ *{title}* {emoji} wurde zu Notion hinzugefügt!\n"
            f"_(Erkannt: \"{transcribed}\")_",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"handle_voice_message error: {e}")
        await msg.edit_text(f"❌ Fehler beim Hinzufügen: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("todos",     todos_command))
    app.add_handler(CommandHandler("reminders", reminders_command))

    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_message))

    # Scheduled reminders
    for time_str in [t.strip() for t in REMINDER_TIMES_RAW.split(",") if t.strip()]:
        try:
            hour, minute = map(int, time_str.split(":"))
            app.job_queue.run_daily(
                send_reminder,
                time=dtime(hour, minute, tzinfo=timezone.utc),
            )
            logger.info(f"Reminder scheduled at {time_str} UTC")
        except ValueError as e:
            logger.error(f"Invalid time '{time_str}': {e}")

    logger.info("Bot läuft...")
    app.run_polling()


if __name__ == "__main__":
    main()
