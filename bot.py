import os
import json
import logging
from datetime import datetime, date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "")
DATA_FILE = os.environ.get("DATA_FILE", "data.json")

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
        "📋 *Commands:*\n"
        "/log – Aktivität loggen & XP verdienen\n"
        "/stats – Dein Profil & Stats anzeigen\n"
        "/rank – Rang-Übersicht\n"
        "/history – Letzte Aktivitäten\n"
        "/setname – Name ändern\n",
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

    logger.info("Bot läuft...")
    app.run_polling()

if __name__ == "__main__":
    main()
