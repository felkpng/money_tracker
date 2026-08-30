import os

# Токен Telegram-бота от @BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "8766794697:AAF0OuJTcznbsJIj1FWGyvjA5Hs-I6K6VPM")

# Ссылка на задеплоенный GitHub Pages фронтенд (обязательно HTTPS)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://felkpng.github.io/money_tracker/")

# Строка подключения к облачной PostgreSQL (Neon.tech)
DATABASE_URL = os.getenv(
    "DATABASE_URL", 
    "postgresql://neondb_owner:npg_uBrF4fR9lagM@ep-fragrant-firefly-b2o3yuh2-pooler.c-6.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)

# Конфигурация запуска сервера (Render передает порт в $PORT)
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("PORT", "8000"))
