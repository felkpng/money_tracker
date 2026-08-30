import asyncio
import hashlib
import hmac
import json
import logging
import urllib.parse
import zoneinfo
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import aiosqlite
import uvicorn
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MSK_TZ = zoneinfo.ZoneInfo("Europe/Moscow")

DEFAULT_CATEGORIES = [
    ("🍔", "Еда"),
    ("🚕", "Такси"),
    ("🛒", "Покупки"),
    ("🏠", "Жилье"),
    ("🎮", "Игры"),
    ("💊", "Здоровье"),
    ("☕", "Кофе"),
    ("📦", "Прочее"),
]


# ==========================================
# 1. СХЕМА БАЗЫ ДАННЫХ
# ==========================================

async def init_db():
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                daily_limit REAL DEFAULT 2000.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id TEXT PRIMARY KEY,
                user_id INTEGER,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id) ON DELETE CASCADE
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id TEXT PRIMARY KEY,
                user_id INTEGER,
                category_id TEXT,
                amount REAL NOT NULL,
                note TEXT,
                date TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (telegram_id) ON DELETE CASCADE,
                FOREIGN KEY (category_id) REFERENCES categories (id) ON DELETE SET NULL
            )
        """)
        await db.commit()


# ==========================================
# 2. АУТЕНТИФИКАЦИЯ INITDATA
# ==========================================

def verify_telegram_init_data(init_data: str) -> dict:
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            raise ValueError("Hash missing")

        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))

        secret_key = hmac.new(b"WebAppData", settings.BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if calculated_hash != received_hash:
            raise ValueError("Invalid hash signature")

        return json.loads(parsed_data.get("user", "{}"))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Telegram authentication failed: {e}"
        )


async def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token"
        )

    init_data = authorization.split("Bearer ")[1].strip()
    user_info = verify_telegram_init_data(init_data)
    user_id = user_info.get("id")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID not found in initData"
        )

    async with aiosqlite.connect(settings.DB_PATH) as db:
        cur = await db.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (user_id,))
        if not await cur.fetchone():
            await db.execute(
                "INSERT INTO users (telegram_id, username, first_name) VALUES (?, ?, ?)",
                (user_id, user_info.get("username", ""), user_info.get("first_name", ""))
            )
            for idx, (emoji, name) in enumerate(DEFAULT_CATEGORIES, start=1):
                await db.execute(
                    "INSERT INTO categories (id, user_id, name, emoji) VALUES (?, ?, ?, ?)",
                    (f"cat_{user_id}_{idx}", user_id, name, emoji)
                )
            await db.commit()

    return user_info


# ==========================================
# 3. МОДЕЛИ PYDANTIC
# ==========================================

class ExpenseCreate(BaseModel):
    id: str
    amount: float = Field(gt=0)
    categoryId: str
    note: Optional[str] = ""
    date: str


class CategoryCreate(BaseModel):
    id: str
    name: str = Field(min_length=1, max_length=30)
    emoji: str


class LimitUpdate(BaseModel):
    dailyLimit: float = Field(gt=0)


# ==========================================
# 4. РАССЫЛКА В 23:00 ПО МСК
# ==========================================

bot = Bot(token=settings.BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler(timezone=MSK_TZ)


async def send_daily_reports():
    today_str = datetime.now(MSK_TZ).strftime("%Y-%m-%d")

    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        users_cursor = await db.execute("SELECT telegram_id, first_name, daily_limit FROM users")
        users = await users_cursor.fetchall()

        for user in users:
            uid = user["telegram_id"]
            limit = user["daily_limit"]

            exp_cursor = await db.execute(
                "SELECT SUM(amount) as total FROM expenses WHERE user_id = ? AND date = ?",
                (uid, today_str)
            )
            res = await exp_cursor.fetchone()
            total_today = res["total"] if (res and res["total"]) else 0.0

            if total_today == 0:
                text = (
                    f"🌙 **Вечерний отчет**\n\n"
                    f"Сегодня без расходов 🎉\n"
                    f"Дневная норма: **{int(limit):,} ₽**"
                )
            elif total_today <= limit:
                remaining = limit - total_today
                percent = round((total_today / limit) * 100)
                text = (
                    f"🌙 **Вечерний отчет за сегодня:**\n\n"
                    f"💸 Потрачено: **{int(total_today):,} ₽** из **{int(limit):,} ₽** ({percent}%)\n"
                    f"✅ Остаток нормы: **{int(remaining):,} ₽**"
                )
            else:
                over = total_today - limit
                percent = round((total_today / limit) * 100)
                text = (
                    f"⚠️ **Внимание: лимит превышен!**\n\n"
                    f"💸 Потрачено: **{int(total_today):,} ₽** из **{int(limit):,} ₽** ({percent}%)\n"
                    f"📈 Превышение: **+{int(over):,} ₽**"
                )

            try:
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="📊 Открыть Аналитику",
                            web_app=WebAppInfo(url=settings.WEBAPP_URL)
                        )
                    ]]
                )
                await bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Push failed for {uid}: {e}")


# ==========================================
# 5. FASTAPI LIFESPAN И РОУТЫ
# ==========================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler.add_job(send_daily_reports, "cron", hour=23, minute=0)
    scheduler.start()

    polling_task = asyncio.create_task(dp.start_polling(bot))
    yield
    polling_task.cancel()
    scheduler.shutdown()
    await bot.session.close()


app = FastAPI(title="TMA Expense Tracker API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/init")
async def get_initial_data(user: dict = Depends(get_current_user)):
    user_id = user["id"]
    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        u_cur = await db.execute("SELECT daily_limit FROM users WHERE telegram_id = ?", (user_id,))
        user_row = await u_cur.fetchone()
        daily_limit = user_row["daily_limit"] if user_row else 2000.0

        c_cur = await db.execute("SELECT id, name, emoji FROM categories WHERE user_id = ?", (user_id,))
        categories = [dict(row) for row in await c_cur.fetchall()]

        e_cur = await db.execute(
            "SELECT id, amount, category_id as categoryId, note, date FROM expenses WHERE user_id = ? ORDER BY date DESC, created_at DESC",
            (user_id,)
        )
        expenses = [dict(row) for row in await e_cur.fetchall()]

    return {
        "dailyLimit": daily_limit,
        "categories": categories,
        "expenses": expenses
    }


@app.post("/api/expenses")
async def create_expense(payload: ExpenseCreate, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute(
            "INSERT INTO expenses (id, user_id, category_id, amount, note, date) VALUES (?, ?, ?, ?, ?, ?)",
            (payload.id, user_id, payload.categoryId, payload.amount, payload.note, payload.date)
        )
        await db.commit()
    return {"status": "ok", "expense": payload.model_dump()}


@app.delete("/api/expenses/{expense_id}")
async def delete_expense(expense_id: str, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (expense_id, user_id))
        await db.commit()
    return {"status": "deleted"}


@app.post("/api/categories")
async def create_category(payload: CategoryCreate, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute(
            "INSERT INTO categories (id, user_id, name, emoji) VALUES (?, ?, ?, ?)",
            (payload.id, user_id, payload.name, payload.emoji)
        )
        await db.commit()
    return {"status": "ok", "category": payload.model_dump()}


@app.delete("/api/categories/{category_id}")
async def delete_category(category_id: str, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("DELETE FROM categories WHERE id = ? AND user_id = ?", (category_id, user_id))
        await db.commit()
    return {"status": "deleted"}


@app.patch("/api/settings/limit")
async def update_daily_limit(payload: LimitUpdate, user: dict = Depends(get_current_user)):
    user_id = user["id"]
    async with aiosqlite.connect(settings.DB_PATH) as db:
        await db.execute("UPDATE users SET daily_limit = ? WHERE telegram_id = ?", (payload.dailyLimit, user_id))
        await db.commit()
    return {"status": "ok", "dailyLimit": payload.dailyLimit}


# ==========================================
# 6. ХЭНДЛЕРЫ БОТА
# ==========================================

@dp.message(CommandStart())
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cur = await db.execute("SELECT telegram_id FROM users WHERE telegram_id = ?", (user_id,))
        if not await cur.fetchone():
            await db.execute(
                "INSERT INTO users (telegram_id, username, first_name) VALUES (?, ?, ?)",
                (user_id, message.from_user.username or "", message.from_user.first_name or "")
            )
            for idx, (emoji, name) in enumerate(DEFAULT_CATEGORIES, start=1):
                await db.execute(
                    "INSERT INTO categories (id, user_id, name, emoji) VALUES (?, ?, ?, ?)",
                    (f"cat_{user_id}_{idx}", user_id, name, emoji)
                )
            await db.commit()

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="⚡ Открыть Трекер Расходов",
                web_app=WebAppInfo(url=settings.WEBAPP_URL)
            )
        ]]
    )

    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Открывайте трекер кнопкой ниже или записывайте траты прямо в чат сообщением:\n"
        "👉 `Еда 450`\n"
        "👉 `Такси 320 поездка в офис`\n\n"
        "Каждый вечер в 23:00 я пришлю сводку дня.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


@dp.message(F.text)
async def handle_quick_expense(message: types.Message):
    text = message.text.strip()
    parts = text.split(maxsplit=2)

    if len(parts) < 2:
        return

    cat_query = parts[0].strip().lower()
    amount_str = parts[1].replace(",", ".").strip()
    note = parts[2].strip() if len(parts) > 2 else ""

    try:
        amount = float(amount_str)
        if amount <= 0:
            return
    except ValueError:
        return

    user_id = message.from_user.id
    today_str = datetime.now(MSK_TZ).strftime("%Y-%m-%d")

    async with aiosqlite.connect(settings.DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        c_cur = await db.execute("SELECT id, name, emoji FROM categories WHERE user_id = ?", (user_id,))
        categories = await c_cur.fetchall()

        matched_cat = next(
            (c for c in categories if c["name"].lower() == cat_query or c["emoji"] == cat_query),
            None
        )
        if not matched_cat:
            return

        expense_id = f"exp_tg_{int(datetime.now().timestamp() * 1000)}"
        await db.execute(
            "INSERT INTO expenses (id, user_id, category_id, amount, note, date) VALUES (?, ?, ?, ?, ?, ?)",
            (expense_id, user_id, matched_cat["id"], amount, note, today_str)
        )
        await db.commit()

        t_cur = await db.execute(
            "SELECT SUM(amount) as total FROM expenses WHERE user_id = ? AND date = ?",
            (user_id, today_str)
        )
        total_res = await t_cur.fetchone()
        today_total = total_res["total"] if total_res and total_res["total"] else amount

        u_cur = await db.execute("SELECT daily_limit FROM users WHERE telegram_id = ?", (user_id,))
        user_row = await u_cur.fetchone()
        limit = user_row["daily_limit"] if user_row else 2000.0

    note_label = f" ({note})" if note else ""
    status_icon = "⚠️ Лимит превышен!" if today_total > limit else "✅ В пределах нормы"

    await message.reply(
        f"Записано: **{matched_cat['emoji']} {matched_cat['name']}** — **{int(amount):,} ₽**{note_label}\n\n"
        f"📊 Траты за сегодня: **{int(today_total):,} ₽** / {int(limit):,} ₽\n"
        f"{status_icon}",
        parse_mode="Markdown"
    )


if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.SERVER_HOST, port=settings.SERVER_PORT, reload=True)