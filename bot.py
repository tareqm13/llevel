import os
import json
import logging
import asyncio
from datetime import datetime, date, time as dtime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
try:
    from notion_client import Client as NotionClient
    from notion_client.errors import APIResponseError as NotionAPIError
    NOTION_AVAILABLE = True
except ImportError:
    NOTION_AVAILABLE = False
try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN     = os.environ.get("BOT_TOKEN", "")
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

# ── Notion / Reminder Config ──────────────────────────────────────────────────
NOTION_TOKEN       = os.environ.get("NOTION_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
REMINDER_CHAT_ID   = os.environ.get("REMINDER_CHAT_ID", "")
REMINDER_TIMES_RAW = os.environ.get("REMINDER_TIMES", "09:00,14:00,19:00")
# All times are in UTC. Example: "09:00,14:00,19:00" → 9am, 2pm, 7pm UTC.

# ── RPG Constants ─────────────────────────────────────────────────────────────
STATS = {
    "STR": {"name": "Strength",     "emoji": "💪", "desc": "Gym / Fitness"},
    "INT": {"name": "Intelligence", "emoji": "📚", "desc": "Lernen / Uni"},
    "DEX": {"name": "Dexterity",    "emoji": "💻", "desc": "Coding / Skills"},
    "WIL": {"name": "Willpower",    "emoji": "🔥", "desc": "Habits / Disziplin"},
}

RANKS = [
    (0,    "E", "⬛"),
    (500,  "D", "🟫"),
    (1500, "C", "⬜"),
    (3000, "B", "🟦"),
    (6000, "A", "🟨"),
    (10000,"S", "🟥"),
]

XP_TABLE = {
    # Gym / STR
    "gym_session":       ("STR", 50,  "Gym Session"),
    "gym_pr":            ("STR", 100, "Neuer PR"),
    "cardio":            ("STR", 30,  "Cardio"),
    # Lernen / INT
    "study_30":          ("INT", 20,  "30 Min lernen"),
    "study_60":          ("INT", 45,  "1h lernen"),
    "study_120":         ("INT", 100, "2h+ lernen"),
    "aufgaben_solved":   ("INT", 35,  "Aufgaben gelöst"),
    "anki_session":      ("INT", 25,  "Anki Session"),
    # Coding / DEX
    "coding_30":         ("DEX", 20,  "30 Min coden"),
    "coding_project":    ("DEX", 80,  "An Projekt gearbeitet"),
    "coding_commit":     ("DEX", 40,  "Commit gepusht"),
    # Habits / WIL
    "early_rise":        ("WIL", 30,  "Früh aufgestanden (<8 Uhr)"),
    "no_procrastinate":  ("WIL", 40,  "Kein Prokrastinieren"),
    "cold_shower":       ("WIL", 25,  "Kaltdusche"),
    "no_social_media":   ("WIL", 35,  "Social Media < 30 Min"),
    "sleep_on_time":     ("WIL", 20,  "Pünktlich schlafen"),
}

ACTIVITY_CATEGORIES = {
    "STR": {k: v for k, v in XP_TABLE.items() if v[0] == "STR"},
    "INT": {k: v for k, v in XP_TABLE.items() if v[0] == "INT"},
    "DEX": {k: v for k, v in XP_TABLE.items() if v[0] == "DEX"},
    "WIL": {k: v for k, v in XP_TABLE.items() if v[0] == "WIL"},
}

# Conversation states
CHOOSING_CATEGORY, CHOOSING_ACTIVITY = range(2)

# ── Data Layer ────────────────────────────────────────────────────────────────
def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_user(data: dict, uid: str) -> dict:
    if uid not in data:
        data[uid] = {
            "name": "Hunter",
            "xp": {"STR": 0, "INT": 0, "DEX": 0, "WIL": 0},
            "total_xp": 0,
            "level": 1,
            "log": [],
            "streaks": {"last_log_date": None, "current_streak": 0, "longest_streak": 0},
            "today_logged": [],
        }
    return data[uid]

# ── RPG Logic ─────────────────────────────────────────────────────────────────
def get_rank(xp: int) -> tuple:
    rank_name, rank_emoji = "E", "⬛"
    for threshold, name, emoji in RANKS:
        if xp >= threshold:
            rank_name, rank_emoji = name, emoji
    return rank_name, rank_emoji

def xp_to_level(total_xp: int) -> int:
    # Level = floor(1 + sqrt(total_xp / 100))
    import math
    return max(1, int(1 + math.sqrt(total_xp / 100)))

def xp_for_next_level(level: int) -> int:
    return (level) ** 2 * 100

def get_overall_rank(total_xp: int) -> tuple:
    return get_rank(total_xp)

def update_streak(user: dict) -> int:
    today = str(date.today())
    streaks = user["streaks"]
    last = streaks.get("last_log_date")

    if last == today:
        return streaks["current_streak"]

    if last == str(date.fromordinal(date.today().toordinal() - 1)):
        streaks["current_streak"] += 1
    else:
        streaks["current_streak"] = 1

    if streaks["current_streak"] > streaks["longest_streak"]:
        streaks["longest_streak"] = streaks["current_streak"]

    streaks["last_log_date"] = today
    user["today_logged"] = []  # reset daily activities
    return streaks["current_streak"]

# ── UI Helpers ────────────────────────────────────────────────────────────────
def build_stat_bar(xp: int, max_xp: int = 500, length: int = 10) -> str:
    filled = min(length, int((xp % max_xp) / max_xp * length))
    return "█" * filled + "░" * (length - filled)

def format_profile(user: dict) -> str:
    total_xp = user["total_xp"]
    level = xp_to_level(total_xp)
    next_lvl_xp = xp_for_next_level(level)
    prev_lvl_xp = xp_for_next_level(level - 1) if level > 1 else 0
    progress = total_xp - prev_lvl_xp
    needed = next_lvl_xp - prev_lvl_xp
    rank, rank_emoji = get_overall_rank(total_xp)
    streak = user["streaks"]["current_streak"]
    longest = user["streaks"]["longest_streak"]

    lines = [
        f"╔══════════════════════╗",
        f"║  ⚔️  {user['name']:<16}  ║",
        f"║  {rank_emoji} Rank {rank}  │  Level {level:<4}  ║",
        f"╚══════════════════════╝",
        f"",
        f"📊 *STATS*",
    ]

    for stat_key, info in STATS.items():
        xp = user["xp"][stat_key]
        stat_rank, stat_emoji = get_rank(xp)
        bar = build_stat_bar(xp)
        lines.append(f"{info['emoji']} *{stat_key}* [{stat_rank}] {bar} `{xp} XP`")

    lines += [
        f"",
        f"⚡ *Total XP:* `{total_xp}`",
        f"📈 *Level Progress:* `{progress}/{needed} XP`",
        f"{'█' * int(progress/needed*10) if needed > 0 else '██████████'}{'░' * (10 - int(progress/needed*10)) if needed > 0 else ''}",
        f"",
        f"🔥 *Streak:* `{streak} Tage` (Rekord: `{longest}`)",
    ]
    return "\n".join(lines)

# ── Notion & Reminder Logic ───────────────────────────────────────────────────
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}

def fetch_notion_todos() -> list:
    """Fetch all open todos from Notion. Synchronous — call via asyncio.to_thread."""
    if not NOTION_AVAILABLE:
        raise RuntimeError("notion-client not installed")
    client = NotionClient(auth=NOTION_TOKEN)
    results = client.databases.query(
        database_id=NOTION_DATABASE_ID,
        page_size=50,
    )
    todos = []
    for page in results.get("results", []):
        props = page.get("properties", {})
        # Find title property (type == "title")
        title = ""
        for prop in props.values():
            if prop.get("type") == "title":
                parts = prop.get("title", [])
                title = "".join(p.get("plain_text", "") for p in parts).strip()
                break
        if not title:
            continue
        # Find priority (select property named "Priority")
        priority = None
        for name, prop in props.items():
            if name.lower() == "priority" and prop.get("type") == "select":
                sel = prop.get("select")
                if sel:
                    priority = sel.get("name", "").lower()
                break
        todos.append({"title": title, "priority": priority})

    # Sort: high → medium → low → no priority
    todos.sort(key=lambda t: _PRIORITY_ORDER.get(t["priority"] or "", 99))
    return todos


def _format_plain_todo_list(todos: list) -> str:
    if not todos:
        return "✅ Keine offenen Todos – alles erledigt!"
    lines = ["📋 *Deine offenen Todos:*"]
    for t in todos:
        p = t.get("priority")
        label = {"high": " 🔴", "medium": " 🟡", "low": " 🟢"}.get(p, "")
        lines.append(f"• {t['title']}{label}")
    return "\n".join(lines)


def summarize_todos_with_claude(todos: list) -> str:
    """Use Claude Haiku to create a motivating reminder. Synchronous."""
    if not todos:
        return "✅ Keine offenen Todos – alles erledigt! Gute Arbeit!"
    if not ANTHROPIC_AVAILABLE or not ANTHROPIC_API_KEY:
        return _format_plain_todo_list(todos)

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
                "Der Nutzer trackt seine Todos in Notion. "
                "Schreib eine kurze, motivierende Erinnerung. "
                "Nur Klartext, keine Markdown-Header. "
                "Antworte in der Sprache der Aufgaben-Titel."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Hier sind die offenen Todos des Nutzers aus Notion:\n\n{todo_text}\n\n"
                    "Schreib eine knappe Erinnerung (3-5 Stichpunkte max) die:\n"
                    "1. Die 2-3 wichtigsten/dringendsten Aufgaben zuerst hervorhebt\n"
                    "2. Ähnliche Aufgaben gruppiert wenn es viele gibt\n"
                    "3. Mit einem kurzen motivierenden Satz endet\n"
                    "Maximal 200 Wörter."
                ),
            }],
        )
        return response.content[0].text
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return _format_plain_todo_list(todos)


async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: fetch todos and send reminder to REMINDER_CHAT_ID."""
    if not REMINDER_CHAT_ID:
        logger.warning("REMINDER_CHAT_ID not set, skipping reminder")
        return
    try:
        todos = await asyncio.to_thread(fetch_notion_todos)
    except Exception as e:
        logger.error(f"Notion fetch failed: {e}")
        await context.bot.send_message(
            chat_id=REMINDER_CHAT_ID,
            text="⚠️ Konnte Todos nicht aus Notion laden. Bitte Token/Datenbank prüfen.",
        )
        return
    message = await asyncio.to_thread(summarize_todos_with_claude, todos)
    await context.bot.send_message(chat_id=REMINDER_CHAT_ID, text=message, parse_mode="Markdown")


def _register_reminder_jobs(app):
    """Parse REMINDER_TIMES and register daily reminder jobs."""
    if not REMINDER_CHAT_ID:
        logger.warning("REMINDER_CHAT_ID not set — tägliche Erinnerungen deaktiviert")
        return
    entries = [t.strip() for t in REMINDER_TIMES_RAW.split(",") if t.strip()]
    registered = 0
    for time_str in entries:
        try:
            hour, minute = map(int, time_str.split(":"))
            job_time = dtime(hour, minute, tzinfo=timezone.utc)
            app.job_queue.run_daily(send_reminder, time=job_time, name=f"reminder_{time_str}")
            logger.info(f"Erinnerung geplant um {time_str} UTC")
            registered += 1
        except (ValueError, AttributeError) as e:
            logger.error(f"Ungültige Zeit in REMINDER_TIMES '{time_str}': {e}")
    if registered == 0:
        logger.warning("Keine gültigen Reminder-Zeiten in REMINDER_TIMES gefunden")


# ── Handlers ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load_data()
    user = get_user(data, uid)
    user["name"] = update.effective_user.first_name or "Hunter"
    save_data(data)

    await update.message.reply_text(
        f"⚔️ *Willkommen, {user['name']}!*\n\n"
        "Du bist nun ein *Hunter*. Deine Reise beginnt jetzt.\n\n"
        "📋 *RPG Commands:*\n"
        "/log – Aktivität loggen & XP verdienen\n"
        "/stats – Dein Profil & Stats anzeigen\n"
        "/rank – Rang-Übersicht\n"
        "/history – Letzte Aktivitäten\n"
        "/setname – Name ändern\n\n"
        "✅ *Todo-Erinnerungen:*\n"
        "/todos – Offene Todos aus Notion anzeigen\n"
        "/reminders – Geplante Erinnerungszeiten anzeigen\n",
        parse_mode="Markdown"
    )

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load_data()
    user = get_user(data, uid)
    await update.message.reply_text(format_profile(user), parse_mode="Markdown")

async def rank_info(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["🏆 *RANG-SYSTEM*\n"]
    for threshold, name, emoji in RANKS:
        lines.append(f"{emoji} *Rang {name}* – ab `{threshold} XP`")
    lines += [
        "",
        "📊 *Gilt pro Stat UND für Gesamt-XP*",
        "Erreiche Rang S in allen Stats → Legendärer Status 🔴"
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def log_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load_data()
    user = get_user(data, uid)
    # Reset today_logged if new day
    today = str(date.today())
    if user["streaks"].get("last_log_date") != today and user["streaks"].get("last_log_date") is not None:
        last = user["streaks"].get("last_log_date")
        if last != str(date.fromordinal(date.today().toordinal() - 1)):
            user["today_logged"] = []
    save_data(data)

    keyboard = [
        [InlineKeyboardButton(f"{STATS[s]['emoji']} {s} – {STATS[s]['desc']}", callback_data=f"cat_{s}")]
        for s in STATS
    ]
    keyboard.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
    await update.message.reply_text(
        "⚔️ *Was hast du heute gemacht?*\nWähle eine Kategorie:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return CHOOSING_CATEGORY

async def choose_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("Abgebrochen.")
        return ConversationHandler.END

    stat = query.data.replace("cat_", "")
    ctx.user_data["stat"] = stat
    activities = ACTIVITY_CATEGORIES[stat]

    keyboard = []
    for key, (_, xp, label) in activities.items():
        keyboard.append([InlineKeyboardButton(f"{label} (+{xp} XP)", callback_data=f"act_{key}")])
    keyboard.append([InlineKeyboardButton("⬅️ Zurück", callback_data="back")])
    keyboard.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])

    await query.edit_message_text(
        f"{STATS[stat]['emoji']} *{STATS[stat]['name']}* – Was genau?\n",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return CHOOSING_ACTIVITY

async def choose_activity(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        await query.edit_message_text("Abgebrochen.")
        return ConversationHandler.END

    if query.data == "back":
        keyboard = [
            [InlineKeyboardButton(f"{STATS[s]['emoji']} {s} – {STATS[s]['desc']}", callback_data=f"cat_{s}")]
            for s in STATS
        ]
        keyboard.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
        await query.edit_message_text(
            "⚔️ *Was hast du heute gemacht?*\nWähle eine Kategorie:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return CHOOSING_CATEGORY

    act_key = query.data.replace("act_", "")
    stat, xp_gain, label = XP_TABLE[act_key]

    uid = str(update.effective_user.id)
    data = load_data()
    user = get_user(data, uid)

    # Check if already logged today
    if act_key in user.get("today_logged", []):
        await query.edit_message_text(
            f"⚠️ *{label}* hast du heute schon geloggt!\nKomm morgen wieder für mehr XP.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    # Apply XP
    old_level = xp_to_level(user["total_xp"])
    old_rank, _ = get_rank(user["xp"][stat])

    user["xp"][stat] += xp_gain
    user["total_xp"] += xp_gain
    user["today_logged"].append(act_key)

    new_level = xp_to_level(user["total_xp"])
    new_rank, new_rank_emoji = get_rank(user["xp"][stat])

    # Log entry
    user["log"].append({
        "date": str(date.today()),
        "activity": label,
        "stat": stat,
        "xp": xp_gain
    })
    user["log"] = user["log"][-50:]  # keep last 50

    # Update streak
    streak = update_streak(user)
    save_data(data)

    # Build response
    msg = [f"✅ *{label}*", f"", f"{STATS[stat]['emoji']} +*{xp_gain} {stat}-XP*"]

    if new_rank != old_rank:
        msg.append(f"\n🎉 *RANG AUFSTIEG!* {stat}: → {new_rank_emoji} Rang {new_rank}!")

    if new_level > old_level:
        msg.append(f"\n⬆️ *LEVEL UP!* → Level {new_level}!")

    msg.append(f"\n🔥 Streak: {streak} Tag{'e' if streak != 1 else ''}")
    msg.append(f"📊 Gesamt-XP: `{user['total_xp']}`")

    keyboard = [[InlineKeyboardButton("➕ Weitere Aktivität", callback_data="more"),
                 InlineKeyboardButton("📊 Stats", callback_data="show_stats")]]
    await query.edit_message_text(
        "\n".join(msg),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def post_log_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "more":
        keyboard = [
            [InlineKeyboardButton(f"{STATS[s]['emoji']} {s} – {STATS[s]['desc']}", callback_data=f"cat_{s}")]
            for s in STATS
        ]
        keyboard.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
        await query.edit_message_text(
            "⚔️ *Weitere Aktivität:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return CHOOSING_CATEGORY

    elif query.data == "show_stats":
        uid = str(update.effective_user.id)
        data = load_data()
        user = get_user(data, uid)
        await query.edit_message_text(format_profile(user), parse_mode="Markdown")
        return ConversationHandler.END

async def history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    data = load_data()
    user = get_user(data, uid)
    log = user.get("log", [])[-10:][::-1]

    if not log:
        await update.message.reply_text("Noch keine Aktivitäten geloggt. Starte mit /log!")
        return

    lines = ["📜 *LETZTE AKTIVITÄTEN*\n"]
    for entry in log:
        stat = entry["stat"]
        lines.append(f"{STATS[stat]['emoji']} `{entry['date']}` – {entry['activity']} (+{entry['xp']} XP)")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def setname(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Nutze: /setname DeinName")
        return
    uid = str(update.effective_user.id)
    data = load_data()
    user = get_user(data, uid)
    new_name = " ".join(ctx.args)[:20]
    user["name"] = new_name
    save_data(data)
    await update.message.reply_text(f"✅ Name geändert zu: *{new_name}*", parse_mode="Markdown")

async def todos_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Todos werden geladen...")
    try:
        todos = await asyncio.to_thread(fetch_notion_todos)
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler beim Laden: {e}")
        return
    await update.message.reply_text(_format_plain_todo_list(todos), parse_mode="Markdown")

async def reminders_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    entries = [t.strip() for t in REMINDER_TIMES_RAW.split(",") if t.strip()]
    if not entries or not REMINDER_CHAT_ID:
        await update.message.reply_text("Keine Erinnerungen konfiguriert.")
        return
    lines = ["⏰ *Geplante Erinnerungen (UTC):*"]
    for t in entries:
        lines.append(f"  • {t} Uhr")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("log", log_start)],
        states={
            CHOOSING_CATEGORY: [CallbackQueryHandler(choose_category)],
            CHOOSING_ACTIVITY: [
                CallbackQueryHandler(choose_activity, pattern="^(act_|back|cancel)"),
            ],
        },
        fallbacks=[],
        per_message=False,
    )

    # Also handle the post-log buttons outside conversation
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(post_log_callback, pattern="^(more|show_stats)$"))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("rank", rank_info))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("setname", setname))
    app.add_handler(CommandHandler("todos", todos_command))
    app.add_handler(CommandHandler("reminders", reminders_command))

    _register_reminder_jobs(app)

    logger.info("Bot läuft...")
    app.run_polling()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        raise
