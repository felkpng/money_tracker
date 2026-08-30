import os

# Токен Telegram-бота от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "8766794697:AAF0OuJTcznbsJIj1FWGyvjA5Hs-I6K6VPM")

# Ссылка на задеплоенный GitHub Pages фронтенд (обязательно HTTPS)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://felkpng.github.io/money_tracker/")

# Путь к локальной базе данных SQLite
DB_PATH = os.getenv("DB_PATH", "tracker.db")

# Конфигурация запуска сервера (Render передает порт в $PORT)
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PORT", "8000"))