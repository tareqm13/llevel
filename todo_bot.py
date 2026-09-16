import os
import json
import logging
import asyncio
import tempfile
from datetime import time as dtime
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
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
REMINDER_TIMES_RAW = os.environ.get("REMINDER_TIMES", "09:00,14:00,21:00")

BERLIN_TZ = ZoneInfo("Europe/Berlin")
TIMES_FILE = os.path.join(os.path.dirname(__file__), "reminder_times.json")

# ── Conversation states ────────────────────────────────────────────────────────
AWAITING_ADD_CONFIRM, AWAITING_DELETE_CONFIRM, AWAITING_NEW_TIMES = range(3)

# ── Keyboard ───────────────────────────────────────────────────────────────────
BTN_TODOS    = "📋 Todos anzeigen"
BTN_REMINDER = "⏰ Reminder"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BTN_TODOS, BTN_REMINDER]],
    resize_keyboard=True,
    is_persistent=True,
)

# ── Reminder time persistence ──────────────────────────────────────────────────
def load_reminder_times() -> list[str]:
    try:
        if os.path.exists(TIMES_FILE):
            with open(TIMES_FILE) as f:
                data = json.load(f)
                return data.get("times", [])
    except Exception as e:
        logger.error(f"load_reminder_times error: {e}")
    # Fall back to env var
    return [t.strip() for t in REMINDER_TIMES_RAW.split(",") if t.strip()]


def save_reminder_times(times: list[str]) -> None:
    try:
        with open(TIMES_FILE, "w") as f:
            json.dump({"times": times}, f)
    except Exception as e:
        logger.error(f"save_reminder_times error: {e}")


# ── Notion helpers ────────────────────────────────────────────────────────────
_PRIORITY_ORDER   = {"high": 0, "medium": 1, "low": 2}
_title_prop_cache = None


def _notion_client() -> NotionClient:
    return NotionClient(auth=NOTION_TOKEN)


def _get_title_property_name() -> str:
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
        page_size=100,
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
        todos.append({"title": title, "priority": priority, "page_id": page["id"]})
    todos.sort(key=lambda t: _PRIORITY_ORDER.get(t.get("priority") or "", 99))
    return todos


def create_notion_todos(items: list[dict]) -> None:
    """Create one or more todo pages. items = [{"title":..., "priority":...}]"""
    if not NOTION_AVAILABLE:
        raise RuntimeError("notion-client not installed")
    title_prop = _get_title_property_name()
    client = _notion_client()
    for item in items:
        title = item.get("title", "").strip()
        priority = item.get("priority", "medium")
        if not title:
            continue
        properties = {
            title_prop: {"title": [{"text": {"content": title}}]},
        }
        if priority:
            properties["Priority"] = {"select": {"name": priority.capitalize()}}
        client.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=properties,
        )


def delete_notion_page(page_id: str) -> None:
    """Archive (delete) a Notion page by ID."""
    _notion_client().pages.update(page_id=page_id, archived=True)


# ── AI helpers ────────────────────────────────────────────────────────────────
def process_with_llm(user_text: str, todos: list) -> dict:
    """
    Use Groq Llama 3.3 70B to interpret the user's message.
    Returns {"intent": "add|delete|list|reminder|unknown", "todos": [...], "delete_ids": [...]}
    """
    if not GROQ_AVAILABLE or not GROQ_API_KEY:
        return {"intent": "add", "todos": [{"title": user_text.strip(), "priority": "medium"}], "delete_ids": []}

    todo_list_text = "\n".join(
        f"- ID:{t['page_id']} | {t['title']}" + (f" [{t['priority']}]" if t.get("priority") else "")
        for t in todos
    ) or "(keine Todos vorhanden)"

    system_prompt = (
        "Du bist ein intelligenter Todo-Assistent. Analysiere die Nachricht des Benutzers.\n\n"
        f"Aktuelle Todos:\n{todo_list_text}\n\n"
        "Antworte NUR mit JSON (kein anderer Text):\n"
        '{"intent": "add|delete|list|reminder|unknown", '
        '"todos": [{"title": "...", "priority": "high|medium|low"}], '
        '"delete_ids": ["page_id1", ...]}\n\n'
        "Regeln:\n"
        "- intent=add: Benutzer möchte Aufgabe(n) hinzufügen\n"
        "- intent=delete: Benutzer markiert Aufgabe als erledigt oder möchte sie löschen\n"
        "- intent=list: Benutzer möchte Todos sehen\n"
        "- intent=reminder: Benutzer möchte Reminder-Zeiten ändern\n"
        "- intent=unknown: unklare Anfrage\n"
        "- Bei delete: Finde die passende Aufgabe semantisch (auch wenn Schreibweise leicht abweicht) und füge die page_id in delete_ids ein\n"
        "- Bei add: Extrahiere ALLE erwähnten Aufgaben als separate Einträge\n"
        "- priority high: urgent/wichtig/dringend/asap/sofort/unbedingt\n"
        "- priority low: irgendwann/später/someday/wenn Zeit\n"
        "- priority medium: alles andere\n"
        "- Bereinige Titel: Entferne Füllwörter, halte sie prägnant\n"
        "- todos und delete_ids sind immer Arrays (auch wenn leer: [])\n"
        "- WICHTIG: Wenn der Benutzer sagt er hat ALLE Aufgaben erledigt (z.B. 'alle erledigt', 'alles erledigt', 'alle Aufgaben abgehakt', 'alles done', 'alle Aufgaben erledigt'), dann setze intent=delete und füge ALLE page_ids aus der Todo-Liste in delete_ids ein\n"
        "- WICHTIG: Sätze wie 'alle Aufgaben erledigt' bedeuten IMMER intent=delete mit allen IDs – niemals intent=add"
    )

    try:
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model="llama-4-maverick-17b-128e-instruct",
            max_tokens=400,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        parsed = json.loads(raw)
        intent = parsed.get("intent", "unknown")
        todos_out = parsed.get("todos", [])
        delete_ids = parsed.get("delete_ids", [])
        return {"intent": intent, "todos": todos_out, "delete_ids": delete_ids}
    except Exception as e:
        logger.error(f"LLM error: {e}")
        return {"intent": "add", "todos": [{"title": user_text.strip(), "priority": "medium"}], "delete_ids": []}


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
            model="claude-haiku-4-5-20251001",
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
    chat_id = REMINDER_CHAT_ID
    if not chat_id:
        logger.warning("send_reminder: REMINDER_CHAT_ID not set")
        return
    logger.info(f"send_reminder: fetching todos for chat {chat_id}")
    try:
        todos = await asyncio.to_thread(fetch_notion_todos)
    except Exception as e:
        logger.error(f"Notion fetch failed: {e}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Konnte Todos nicht aus Notion laden.",
        )
        return
    if not todos:
        logger.info("send_reminder: keine Todos, keine Nachricht gesendet")
        return
    message = await asyncio.to_thread(summarize_with_claude, todos)
    await context.bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")
    logger.info("send_reminder: message sent")


def register_reminder_jobs(app, times: list[str]) -> None:
    """Register daily reminder jobs. Each gets a unique name to avoid conflicts."""
    # Remove all existing reminder jobs
    current_jobs = app.job_queue.jobs()
    for job in current_jobs:
        if job.name and job.name.startswith("reminder_"):
            job.schedule_removal()
            logger.info(f"Removed old job: {job.name}")

    for time_str in times:
        time_str = time_str.strip()
        try:
            hour, minute = map(int, time_str.split(":"))
            job_name = f"reminder_{time_str}"
            app.job_queue.run_daily(
                send_reminder,
                time=dtime(hour, minute, tzinfo=BERLIN_TZ),
                name=job_name,
            )
            logger.info(f"Reminder geplant: {time_str} Berlin-Zeit (Job: {job_name})")
        except ValueError as e:
            logger.error(f"Ungültige Uhrzeit '{time_str}': {e}")


# ── Command handlers ──────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Todo-Bot*\n\n"
        "Schreib mir eine Aufgabe als Text oder Sprachnachricht.\n"
        "Ich erkenne die Priorität automatisch und trage sie in Notion ein.\n\n"
        "*Commands:*\n"
        "/todos – Offene Todos anzeigen\n"
        "/reminders – Erinnerungszeiten verwalten\n"
        "/testreminder – Reminder jetzt sofort testen\n",
        parse_mode="Markdown",
        reply_markup=MAIN_KEYBOARD,
    )


async def todos_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Lade Todos...", reply_markup=MAIN_KEYBOARD)
    try:
        todos = await asyncio.to_thread(fetch_notion_todos)
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {e}", reply_markup=MAIN_KEYBOARD)
        return
    await update.message.reply_text(_format_plain_list(todos), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)


async def test_reminder_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Manually trigger the reminder right now — for testing."""
    await update.message.reply_text("🔔 Sende Test-Reminder...", reply_markup=MAIN_KEYBOARD)
    try:
        await send_reminder(ctx)
        await update.message.reply_text("✅ Test-Reminder gesendet!", reply_markup=MAIN_KEYBOARD)
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {e}", reply_markup=MAIN_KEYBOARD)


async def reminders_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    times = load_reminder_times()
    lines = ["⏰ *Geplante Erinnerungen (Berlin-Zeit):*"] + [f"  • {t} Uhr" for t in times]
    lines.append("\nZeiten ändern? Schick mir einfach z.B.: *Ändere Reminder auf 08:00, 13:00, 20:00*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)


# ── Conversation: add / delete todos via text or voice ────────────────────────
_PRIORITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


async def _process_message(text: str, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Shared logic for text and voice message handling."""
    await update.message.reply_text("⏳ Verarbeite...", reply_markup=MAIN_KEYBOARD)

    try:
        todos = await asyncio.to_thread(fetch_notion_todos)
    except Exception as e:
        await update.message.reply_text(f"❌ Notion-Fehler: {e}", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    result = await asyncio.to_thread(process_with_llm, text, todos)
    intent = result.get("intent", "unknown")

    if intent == "list":
        await update.message.reply_text(_format_plain_list(todos), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
        return ConversationHandler.END

    if intent == "reminder":
        await update.message.reply_text(
            "Schreib mir die neuen Zeiten im Format *HH:MM, HH:MM* (Berlin-Zeit):",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        return AWAITING_NEW_TIMES

    if intent == "delete":
        delete_ids = result.get("delete_ids", [])
        if not delete_ids:
            await update.message.reply_text(
                "❌ Ich konnte keine passende Aufgabe finden.\nTippe /todos um alle Aufgaben zu sehen.",
                reply_markup=MAIN_KEYBOARD,
            )
            return ConversationHandler.END

        # Find titles for the IDs
        id_to_title = {t["page_id"]: t["title"] for t in todos}
        titles = [id_to_title.get(pid, pid) for pid in delete_ids]

        ctx.user_data["pending_delete_ids"] = delete_ids
        ctx.user_data["pending_delete_titles"] = titles

        titles_text = "\n".join(f"• *{t}*" for t in titles)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Ja, löschen", callback_data="del_yes"),
             InlineKeyboardButton("❌ Abbrechen", callback_data="del_no")],
        ])
        await update.message.reply_text(
            f"🗑 Möchtest du folgende Aufgabe(n) löschen?\n\n{titles_text}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return AWAITING_DELETE_CONFIRM

    if intent == "add":
        new_todos = result.get("todos", [])
        if not new_todos:
            new_todos = [{"title": text.strip(), "priority": "medium"}]

        ctx.user_data["pending_add_todos"] = new_todos

        lines = ["➕ Soll ich folgende Aufgabe(n) hinzufügen?\n"]
        for t in new_todos:
            p = t.get("priority", "medium")
            emoji = _PRIORITY_EMOJI.get(p, "")
            lines.append(f"• *{t['title']}* {emoji}")

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Ja", callback_data="add_yes"),
             InlineKeyboardButton("❌ Nein", callback_data="add_no")],
        ])
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return AWAITING_ADD_CONFIRM

    # Unknown intent
    await update.message.reply_text(
        "🤔 Ich habe die Anfrage nicht verstanden. Schreib mir eine Aufgabe oder nutze /todos.",
        reply_markup=MAIN_KEYBOARD,
    )
    return ConversationHandler.END


async def handle_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text:
        return ConversationHandler.END
    # Handle bottom keyboard buttons
    if text == BTN_TODOS:
        await todos_command(update, ctx)
        return ConversationHandler.END
    if text == BTN_REMINDER:
        await reminders_command(update, ctx)
        return ConversationHandler.END
    return await _process_message(text, update, ctx)


async def handle_voice_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if not GROQ_AVAILABLE or not GROQ_API_KEY:
        await update.message.reply_text(
            "❌ GROQ_API_KEY nicht gesetzt – Sprachnachrichten nicht verfügbar.",
            reply_markup=MAIN_KEYBOARD,
        )
        return ConversationHandler.END

    msg = await update.message.reply_text("🎙 Transkribiere Sprachnachricht...")

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
        return ConversationHandler.END
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if not transcribed:
        await msg.edit_text("❌ Konnte die Sprachnachricht nicht verstehen.")
        return ConversationHandler.END

    await msg.edit_text(f"🎙 Erkannt: \"{transcribed}\"")
    return await _process_message(transcribed, update, ctx)


# ── Conversation callbacks ─────────────────────────────────────────────────────
async def handle_add_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "add_no":
        await query.edit_message_text("❌ Abgebrochen.")
        return ConversationHandler.END

    new_todos = ctx.user_data.pop("pending_add_todos", [])
    if not new_todos:
        await query.edit_message_text("❌ Keine Aufgaben gefunden.")
        return ConversationHandler.END

    try:
        await asyncio.to_thread(create_notion_todos, new_todos)
        lines = ["✅ Hinzugefügt:"]
        for t in new_todos:
            p = t.get("priority", "medium")
            emoji = _PRIORITY_EMOJI.get(p, "")
            lines.append(f"• *{t['title']}* {emoji}")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await query.edit_message_text(f"❌ Fehler beim Hinzufügen: {e}")

    return ConversationHandler.END


async def handle_delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "del_no":
        await query.edit_message_text("❌ Abgebrochen.")
        return ConversationHandler.END

    delete_ids = ctx.user_data.pop("pending_delete_ids", [])
    titles = ctx.user_data.pop("pending_delete_titles", [])

    errors = []
    for pid in delete_ids:
        try:
            await asyncio.to_thread(delete_notion_page, pid)
        except Exception as e:
            errors.append(str(e))

    if errors:
        await query.edit_message_text(f"⚠️ Teilweise Fehler: {', '.join(errors)}")
    else:
        titles_text = "\n".join(f"• *{t}*" for t in titles)
        await query.edit_message_text(
            f"✅ Gelöscht:\n{titles_text}",
            parse_mode="Markdown",
        )

    return ConversationHandler.END


async def handle_new_times(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends new reminder times like '08:00, 13:00, 20:00'."""
    text = update.message.text.strip()
    raw_times = [t.strip() for t in text.replace(";", ",").split(",") if t.strip()]

    valid_times = []
    invalid = []
    for t in raw_times:
        try:
            h, m = map(int, t.split(":"))
            if 0 <= h <= 23 and 0 <= m <= 59:
                valid_times.append(f"{h:02d}:{m:02d}")
            else:
                invalid.append(t)
        except Exception:
            invalid.append(t)

    if not valid_times:
        await update.message.reply_text(
            f"❌ Keine gültigen Zeiten erkannt in: {text}\nBitte im Format HH:MM angeben, z.B. *08:00, 13:00, 20:00*",
            parse_mode="Markdown",
            reply_markup=MAIN_KEYBOARD,
        )
        return AWAITING_NEW_TIMES

    save_reminder_times(valid_times)
    register_reminder_jobs(ctx.application, valid_times)

    lines = ["⏰ *Neue Erinnerungszeiten (Berlin-Zeit):*"] + [f"  • {t} Uhr" for t in valid_times]
    if invalid:
        lines.append(f"\n⚠️ Ignoriert (ungültig): {', '.join(invalid)}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    # Conversation handler
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message),
            MessageHandler(filters.VOICE, handle_voice_message),
        ],
        states={
            AWAITING_ADD_CONFIRM: [
                CallbackQueryHandler(handle_add_confirm, pattern="^add_"),
            ],
            AWAITING_DELETE_CONFIRM: [
                CallbackQueryHandler(handle_delete_confirm, pattern="^del_"),
            ],
            AWAITING_NEW_TIMES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_new_times),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("todos", todos_command))
    app.add_handler(CommandHandler("reminders", reminders_command))
    app.add_handler(CommandHandler("testreminder", test_reminder_command))
    app.add_handler(conv)

    # Scheduled reminders
    times = load_reminder_times()
    register_reminder_jobs(app, times)

    logger.info("Bot läuft...")
    app.run_polling()


if __name__ == "__main__":
    main()
