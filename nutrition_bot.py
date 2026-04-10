import os
import json
import logging
import asyncio
import tempfile
import base64
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, ConversationHandler,
)

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
TOKEN       = os.environ.get("BOT_TOKEN", "")
XAI_API_KEY = os.environ.get("XAI_API_KEY", "")  # Grok API key
DB_PATH     = os.environ.get("NUTRITION_DB_PATH", "nutrition.db")

# States
AWAITING_MEAL_CONFIRM = 0

# ── SQLite Database ───────────────────────────────────────────────────────────
def _get_db_connection():
    """Get a database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_database():
    """Initialize the database schema."""
    conn = _get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nutrition_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            meal_name TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            calories INTEGER,
            carbs_g REAL,
            protein_g REAL,
            fat_g REAL,
            sugar_g REAL,
            fiber_g REAL,
            meal_type TEXT,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_date ON nutrition_entries(date)")
    conn.commit()
    conn.close()

def create_nutrition_entry(
    meal_name: str,
    calories: int,
    carbs: float,
    protein: float,
    fat: float,
    sugar: Optional[float] = None,
    fiber: Optional[float] = None,
    meal_type: str = "unspecified",
    notes: str = "",
    timestamp: Optional[datetime] = None,
):
    """Create a nutrition entry in the database."""
    timestamp = timestamp or datetime.now()
    conn = _get_db_connection()
    conn.execute("""
        INSERT INTO nutrition_entries
        (meal_name, date, time, calories, carbs_g, protein_g, fat_g, sugar_g, fiber_g, meal_type, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        meal_name,
        timestamp.strftime("%Y-%m-%d"),
        timestamp.strftime("%H:%M"),
        calories,
        carbs,
        protein,
        fat,
        sugar,
        fiber,
        meal_type,
        notes,
    ))
    conn.commit()
    conn.close()

def fetch_entries(start_date: datetime, end_date: datetime) -> List[Dict]:
    """Fetch nutrition entries between two dates."""
    conn = _get_db_connection()
    cursor = conn.execute("""
        SELECT * FROM nutrition_entries
        WHERE date >= ? AND date <= ?
        ORDER BY date ASC, time ASC
    """, (start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")))

    entries = []
    for row in cursor.fetchall():
        entries.append({
            "meal_name": row["meal_name"],
            "date": row["date"],
            "time": row["time"],
            "Calories": row["calories"],
            "Carbs (g)": row["carbs_g"],
            "Protein (g)": row["protein_g"],
            "Fat (g)": row["fat_g"],
            "Sugar (g)": row["sugar_g"],
            "Fiber (g)": row["fiber_g"],
            "meal_type": row["meal_type"],
        })
    conn.close()
    return entries

# ── Grok AI Analysis ──────────────────────────────────────────────────────────
def analyze_food_text(user_message: str) -> Dict:
    """Send food description to Grok to extract nutrition info."""
    if not XAI_API_KEY:
        return {"error": "XAI_API_KEY not set"}
    if not HTTPX_AVAILABLE:
        return {"error": "httpx not installed"}

    system_prompt = """You are a nutrition expert. Analyze the food description and estimate nutritional values.
Return ONLY valid JSON (no other text, no explanations).

Format:
{
    "meal_name": "brief meal description",
    "calories": <integer>,
    "carbs": <float in grams>,
    "protein": <float in grams>,
    "fat": <float in grams>,
    "sugar": <float in grams or null if unknown>,
    "fiber": <float in grams or null if unknown>,
    "meal_type": "breakfast|lunch|dinner|snack",
    "confidence": "high|medium|low"
}

Estimate based on standard portion sizes. Be realistic."""

    try:
        response = httpx.post(
            "https://api.x.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "grok-2-latest",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "max_tokens": 500,
                "temperature": 0,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        raw = data["choices"][0]["message"]["content"].strip()

        # Strip markdown code fences
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                if part.strip().startswith("{"):
                    raw = part.strip()
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                    break

        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Grok returned invalid JSON: {e}\nRaw: {raw}")
        return {"error": "Could not parse food description"}
    except Exception as e:
        logger.error(f"Grok error: {e}")
        return {"error": str(e)}

def analyze_food_image(image_path: str, caption: str = "") -> Dict:
    """Send food image to Grok Vision to identify and estimate nutrition."""
    if not XAI_API_KEY:
        return {"error": "XAI_API_KEY not set"}
    if not HTTPX_AVAILABLE:
        return {"error": "httpx not installed"}

    with open(image_path, "rb") as f:
        image_data = f.read()

    image_base64 = base64.b64encode(image_data).decode("utf-8")

    system_prompt = """You are a nutrition expert analyzing food from an image.
Identify the food items and estimate nutritional values based on visible portions.
Return ONLY valid JSON (no other text, no explanations).

Format:
{
    "meal_name": "brief description of identified foods",
    "calories": <integer>,
    "carbs": <float in grams>,
    "protein": <float in grams>,
    "fat": <float in grams>,
    "sugar": <float in grams or null if unknown>,
    "fiber": <float in grams or null if unknown>,
    "meal_type": "breakfast|lunch|dinner|snack",
    "confidence": "high|medium|low",
    "notes": "any assumptions made about portion sizes or ingredients"
}

Be realistic with estimates. Consider common portion sizes."""

    user_content = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{image_base64}",
            },
        },
    ]

    text_part = "Analyze this meal and provide nutrition info."
    if caption:
        text_part += f" Additional context: {caption}"
    user_content.append({"type": "text", "text": text_part})

    try:
        response = httpx.post(
            "https://api.x.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "grok-2-vision-1212",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": 500,
                "temperature": 0,
            },
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
        raw = data["choices"][0]["message"]["content"].strip()

        # Strip markdown code fences
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                if part.strip().startswith("{"):
                    raw = part.strip()
                    if raw.startswith("json"):
                        raw = raw[4:]
                    raw = raw.strip()
                    break

        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Grok returned invalid JSON: {e}\nRaw: {raw}")
        return {"error": "Could not analyze image"}
    except httpx.HTTPError as e:
        logger.error(f"Grok API error: {e}")
        return {"error": f"API error: {e}"}
    except Exception as e:
        logger.error(f"Grok error: {e}")
        return {"error": str(e)}

# ── Formatting ────────────────────────────────────────────────────────────────
def format_nutrition_entry(data: Dict) -> str:
    """Format nutrition data as readable text."""
    lines = [f"🍽 *{data.get('meal_name', 'Meal')}*\n"]
    lines.append(f"🔥 Calories: {data.get('calories', '?')} kcal")
    lines.append(f"🍞 Carbs: {data.get('carbs', '?')}g")
    lines.append(f"🥩 Protein: {data.get('protein', '?')}g")
    lines.append(f"🥑 Fat: {data.get('fat', '?')}g")

    if data.get('sugar') is not None:
        lines.append(f"🍬 Sugar: {data['sugar']}g")
    if data.get('fiber') is not None:
        lines.append(f"🌾 Fiber: {data['fiber']}g")

    lines.append(f"\n🕐 Meal: {data.get('meal_type', 'unspecified').capitalize()}")

    confidence = data.get('confidence', 'medium')
    if confidence == 'low':
        lines.append("\n⚠️ Low confidence - values are estimates")

    return "\n".join(lines)

def format_daily_summary(entries: List[Dict]) -> str:
    """Format daily nutrition summary."""
    if not entries:
        return "📭 No entries for this day."

    total_calories = sum(e.get("Calories", 0) or 0 for e in entries)
    total_carbs = sum(e.get("Carbs (g)", 0) or 0 for e in entries)
    total_protein = sum(e.get("Protein (g)", 0) or 0 for e in entries)
    total_fat = sum(e.get("Fat (g)", 0) or 0 for e in entries)

    lines = ["📊 *Daily Summary*\n"]
    lines.append(f"🔥 Total Calories: {total_calories} kcal")
    lines.append(f"🍞 Total Carbs: {total_carbs:.0f}g")
    lines.append(f"🥩 Total Protein: {total_protein:.0f}g")
    lines.append(f"🥑 Total Fat: {total_fat:.0f}g")
    lines.append(f"\n📝 Meals logged: {len(entries)}")
    lines.append("\n*Meals:*")

    for e in entries:
        time_str = e.get("time", "")
        cals = e.get("Calories", "?")
        lines.append(f"  • {time_str} - {e['meal_name']} ({cals} kcal)")

    return "\n".join(lines)

def format_weekly_summary(entries: List[Dict]) -> str:
    """Format weekly nutrition summary."""
    if not entries:
        return "📭 No entries for this week."

    by_date = {}
    for e in entries:
        date = e.get("date", "unknown")
        if date not in by_date:
            by_date[date] = []
        by_date[date].append(e)

    lines = ["📊 *Weekly Summary*\n"]

    for date in sorted(by_date.keys()):
        day_entries = by_date[date]
        day_cals = sum(e.get("Calories", 0) or 0 for e in day_entries)
        day_protein = sum(e.get("Protein (g)", 0) or 0 for e in day_entries)
        lines.append(f"*{date}*: {day_cals} kcal, {day_protein:.0f}g protein ({len(day_entries)} meals)")

    total_calories = sum(e.get("Calories", 0) or 0 for e in entries)
    avg_calories = total_calories // len(by_date) if by_date else 0

    lines.append(f"\n📈 Total: {total_calories} kcal")
    lines.append(f"📉 Daily avg: {avg_calories} kcal")

    return "\n".join(lines)

# ── Confirmation keyboard ─────────────────────────────────────────────────────
def _yes_no_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data="confirm_yes"),
        InlineKeyboardButton("❌ No", callback_data="confirm_no"),
    ]])

# ── Core message processing ───────────────────────────────────────────────────
async def _handle_text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """Process food description text."""
    await update.message.reply_text("🔍 Analyzing your meal...")

    result = await asyncio.to_thread(analyze_food_text, text)

    if "error" in result:
        await update.message.reply_text(f"❌ {result['error']}")
        return ConversationHandler.END

    ctx.user_data["pending_nutrition"] = result

    message = format_nutrition_entry(result)
    message += "\n\n📝 Log this meal?"

    await update.message.reply_text(message, reply_markup=_yes_no_keyboard(), parse_mode="Markdown")
    return AWAITING_MEAL_CONFIRM

async def _handle_photo_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Process food photo."""
    await update.message.reply_text("📸 Analyzing your meal photo...")

    photo = update.message.photo[-1]
    tg_file = await ctx.bot.get_file(photo.file_id)

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name

    await tg_file.download_to_drive(tmp_path)
    caption = update.message.caption or ""

    try:
        result = await asyncio.to_thread(analyze_food_image, tmp_path, caption)
    except Exception as e:
        logger.error(f"Image analysis error: {e}")
        await update.message.reply_text(f"❌ Failed to analyze image: {e}")
        return ConversationHandler.END
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if "error" in result:
        await update.message.reply_text(f"❌ {result['error']}")
        return ConversationHandler.END

    ctx.user_data["pending_nutrition"] = result

    message = format_nutrition_entry(result)
    message += "\n\n📝 Log this meal?"

    await update.message.reply_text(message, reply_markup=_yes_no_keyboard(), parse_mode="Markdown")
    return AWAITING_MEAL_CONFIRM

# ── Callbacks ─────────────────────────────────────────────────────────────────
async def handle_meal_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm_no":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    nutrition = ctx.user_data.pop("pending_nutrition", {})

    if not nutrition:
        await query.edit_message_text("❌ No pending meal data.")
        return ConversationHandler.END

    try:
        await asyncio.to_thread(
            create_nutrition_entry,
            meal_name=nutrition.get("meal_name", "Unknown"),
            calories=nutrition.get("calories", 0),
            carbs=nutrition.get("carbs", 0),
            protein=nutrition.get("protein", 0),
            fat=nutrition.get("fat", 0),
            sugar=nutrition.get("sugar"),
            fiber=nutrition.get("fiber"),
            meal_type=nutrition.get("meal_type", "unspecified"),
            notes=nutrition.get("notes", ""),
        )
        await query.edit_message_text("✅ Meal logged successfully!")
    except Exception as e:
        await query.edit_message_text(f"❌ Error logging meal: {e}")

    return ConversationHandler.END

# ── Commands ──────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍽 *Nutrition Tracker Bot*\n\n"
        "Track your meals with text or photos!\n\n"
        "*How to use:*\n"
        "📝 _Send text_: 'I ate 2 eggs, toast, and coffee'\n"
        "📸 _Send a photo_: I'll analyze the food\n"
        "📊 _View stats_: /day or /week\n\n"
        "I'll estimate calories, carbs, protein, fat, and more!",
        parse_mode="Markdown",
    )

async def day_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show today's nutrition summary."""
    await update.message.reply_text("⏳ Loading today's entries...")

    now = datetime.now()
    start_of_day = datetime(now.year, now.month, now.day)

    try:
        entries = await asyncio.to_thread(fetch_entries, start_of_day, now)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return

    summary = format_daily_summary(entries)
    await update.message.reply_text(summary, parse_mode="Markdown")

async def week_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show this week's nutrition summary."""
    await update.message.reply_text("⏳ Loading this week's entries...")

    now = datetime.now()
    start_of_week = now - timedelta(days=now.weekday())
    start_of_week = datetime(start_of_week.year, start_of_week.month, start_of_week.day)

    try:
        entries = await asyncio.to_thread(fetch_entries, start_of_week, now)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return

    summary = format_weekly_summary(entries)
    await update.message.reply_text(summary, parse_mode="Markdown")

async def log_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Quick log command with inline args."""
    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text(
            "Usage: /log <meal description>\n"
            "Example: /log 2 eggs and toast for breakfast"
        )
        return

    return await _handle_text_message(update, ctx, text)

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # Initialize database
    init_database()
    logger.info(f"Database initialized: {DB_PATH}")

    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text_message),
            MessageHandler(filters.PHOTO, _handle_photo_message),
        ],
        states={
            AWAITING_MEAL_CONFIRM: [CallbackQueryHandler(handle_meal_confirm, pattern="^confirm_")],
        },
        fallbacks=[],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("day", day_command))
    app.add_handler(CommandHandler("week", week_command))
    app.add_handler(CommandHandler("log", log_command))
    app.add_handler(conv)

    logger.info("Nutrition Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
