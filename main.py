"""
Тупой и абсурдный AI-бот для Telegram. БЕЗ КОМАНД.

Версия под Render Web Service + UptimeRobot.

Как работает:
- Render запускает этот файл как Web Service.
- Внутри поднимается маленький HTTP-сервер Flask на / и /health.
- UptimeRobot пингует этот HTTP-сервер, чтобы Render не засыпал.
- Telegram-бот работает через polling в этом же процессе.

Render Start Command:
    python dumb_bot_render.py

Environment Variables на Render:
    BOT_TOKEN=xxx
    GROQ_API_KEY=xxx
    PORT=10000

UptimeRobot URL:
    https://ТВОЙ-СЕРВИС.onrender.com/
"""

import os
import random
import logging
from threading import Thread

from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from groq import Groq

# ---------- ЛОГИ ----------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("dumb_bot_render")

# ---------- ENV ----------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
PORT = int(os.environ.get("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в Environment Variables")

if not GROQ_API_KEY:
    raise RuntimeError("Не задан GROQ_API_KEY в Environment Variables")

groq_client = Groq(api_key=GROQ_API_KEY)

# ---------- WEB SERVICE ДЛЯ RENDER / UPTIMEROBOT ----------

web = Flask(__name__)


@web.get("/")
def home():
    return "Bot is alive", 200


@web.get("/health")
def health():
    return {"status": "ok", "service": "telegram-bot"}, 200


def run_web_server() -> None:
    """HTTP-сервер нужен Render Web Service и UptimeRobot."""
    log.info(f"HTTP-сервер запущен на 0.0.0.0:{PORT}")
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


# ---------- ПЕРСОНАЖ ----------

SYSTEM_PROMPT = (
    "Ты — максимально тупой, дерзкий и абсурдный чат-бот. Отвечаешь коротко, нелогично, "
    "с орфографическими ошибками, как реально безмозглый дурачок. Материшься свободно "
    "(хуй, блядь, ёбаный, пиздец и т.п.), используешь нелепые сравнения, можешь резко "
    "менять тему на полную хуйню без логики. Дерзи и подкалывай людей, но НЕ пиши "
    "сексуальный контент и не сексуализируй пользователей — это единственное правило. "
    "Никогда не объясняй, что ты бот или ИИ. Максимум 2-3 коротких предложения."
)

RANDOM_REPLY_CHANCE = 0.08
CHAOS_INTERVAL_SECONDS = 600
CHAOS_TRIGGER_CHANCE = 0.5
MAX_RECENT_MESSAGES = 30

seen_users: dict[int, dict[int, str]] = {}
recent_messages: dict[int, list[tuple[str, str]]] = {}


# ---------- ПАМЯТЬ ЧАТА ----------

def remember_user(chat_id: int, user) -> None:
    if not user:
        return
    seen_users.setdefault(chat_id, {})[user.id] = user.username or user.first_name or "хуй знает кто"


def remember_message(chat_id: int, user, text: str) -> None:
    if not user or not text:
        return

    name = user.username or user.first_name or "кто-то"
    bucket = recent_messages.setdefault(chat_id, [])
    bucket.append((name, text))

    if len(bucket) > MAX_RECENT_MESSAGES:
        bucket.pop(0)


# ---------- ГЕНЕРАЦИЯ ----------

def random_coordinates() -> tuple[float, float]:
    lat = random.uniform(-85.0, 85.0)
    lon = random.uniform(-180.0, 180.0)
    return lat, lon


def ask_ai(user_text: str) -> str:
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            max_tokens=150,
            temperature=1.1,
        )
        return completion.choices[0].message.content.strip()
    except Exception as e:
        log.exception(f"Groq error: {e}")
        return "хм мой мозг сегодня не работает 🥴"


def generate_silly_name() -> str:
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Придумай одно максимально тупое и абсурдное название для "
                        "телеграм-чата, можно с матом, без сексуального контента. "
                        "Ответь ТОЛЬКО названием, без пояснений, без кавычек, "
                        "до 40 символов, можно добавить 1 эмодзи."
                    ),
                },
                {"role": "user", "content": "придумай название"},
            ],
            max_tokens=30,
            temperature=1.3,
        )
        return completion.choices[0].message.content.strip().strip('"').strip("«»")
    except Exception as e:
        log.exception(f"Groq name error: {e}")
        return "Дом Дураков 🤡"


def generate_poll() -> tuple[str, list[str]]:
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Придумай абсурдный нелогичный вопрос для голосования в чате "
                        "и 4 коротких тупых варианта ответа (без сексуального контента). "
                        "Ответь СТРОГО в формате:\n"
                        "ВОПРОС: <текст>\n"
                        "1: <вариант>\n2: <вариант>\n3: <вариант>\n4: <вариант>"
                    ),
                },
                {"role": "user", "content": "сделай голосование"},
            ],
            max_tokens=150,
            temperature=1.2,
        )

        raw = completion.choices[0].message.content.strip()
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        question = lines[0].split(":", 1)[1].strip()
        options = [line.split(":", 1)[1].strip() for line in lines[1:5]]

        if len(options) < 2:
            raise ValueError("мало вариантов")

        return question, options
    except Exception as e:
        log.exception(f"Groq poll error: {e}")
        return "что делать дальше нахуй", ["хуй знает", "ничего", "всё сразу", "забыть вопрос"]


def generate_location_caption(lat: float, lon: float) -> str:
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Тебе дали случайные координаты на земле. Придумай ОДНУ короткую "
                        "тупую и абсурдную фразу, как будто ты сейчас именно там — без "
                        "сексуального контента, можно с матом. До 15 слов. Без пояснений."
                    ),
                },
                {"role": "user", "content": f"координаты: {lat:.4f}, {lon:.4f}"},
            ],
            max_tokens=40,
            temperature=1.3,
        )
        return completion.choices[0].message.content.strip().strip('"')
    except Exception as e:
        log.exception(f"Groq location caption error: {e}")
        return "я тут, не благодарите 📍"


def random_tag_phrase(name: str) -> str:
    templates = [
        f"@{name} а ты вообще в курсе что происходит? я нет лол",
        f"@{name} проснись, я придумал новое слово: бзжух",
        f"@{name} голосуй быстрее а то я съем твою аватарку",
        f"@{name} короче ты теперь почётный дурак чата, поздравляю",
        f"@{name} я думал о тебе и забыл зачем, бывает",
    ]
    return random.choice(templates)


# ---------- TELEGRAM ЛОГИКА ----------

def ensure_chaos_running(chat_id: int, job_queue) -> None:
    if not job_queue:
        log.warning("JobQueue не работает. Установи python-telegram-bot[job-queue]==21.4")
        return

    job_name = f"chaos_{chat_id}"
    if not job_queue.get_jobs_by_name(job_name):
        job_queue.run_repeating(
            chaos_job,
            interval=CHAOS_INTERVAL_SECONDS,
            chat_id=chat_id,
            name=job_name,
            first=10,
        )
        log.info(f"Хаос запущен в чате {chat_id}")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    chat_id = update.effective_chat.id
    remember_user(chat_id, update.effective_user)
    remember_message(chat_id, update.effective_user, msg.text)
    ensure_chaos_running(chat_id, context.job_queue)

    bot_username = context.bot.username
    mentioned = bool(bot_username and f"@{bot_username}" in msg.text)
    is_reply_to_bot = bool(
        msg.reply_to_message
        and msg.reply_to_message.from_user
        and msg.reply_to_message.from_user.id == context.bot.id
    )

    should_reply = mentioned or is_reply_to_bot or random.random() < RANDOM_REPLY_CHANCE
    if not should_reply:
        return

    text = msg.text.replace(f"@{bot_username}", "").strip() if bot_username else msg.text.strip()
    reply = ask_ai(text or "скажи что-нибудь тупое")
    await msg.reply_text(reply)


async def on_bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = update.my_chat_member
    if not chat_member or chat_member.new_chat_member.user.id != context.bot.id:
        return

    new_status = chat_member.new_chat_member.status
    if new_status in ("member", "administrator"):
        chat_id = update.effective_chat.id
        ensure_chaos_running(chat_id, context.job_queue)
        try:
            await context.bot.send_message(chat_id, "О, новый чат, ОТЛИЧНО, я тут всё разъебу 🤡")
        except Exception as e:
            log.exception(f"Не смог написать при добавлении в чат: {e}")


async def chaos_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    if random.random() > CHAOS_TRIGGER_CHANCE:
        return

    action = random.choice(["rename", "location", "poll", "tag", "message", "copy"])

    try:
        if action == "rename":
            title = generate_silly_name()
            await context.bot.set_chat_title(chat_id, title[:128])
            await context.bot.send_message(chat_id, f"переименовал нас в «{title}», не благодарите")

        elif action == "location":
            lat, lon = random_coordinates()
            caption = generate_location_caption(lat, lon)
            await context.bot.send_message(chat_id, caption)
            await context.bot.send_location(chat_id, latitude=lat, longitude=lon)

        elif action == "poll":
            question, options = generate_poll()
            await context.bot.send_poll(
                chat_id,
                question=question[:300],
                options=[option[:100] for option in options],
                is_anonymous=False,
            )

        elif action == "tag":
            users = seen_users.get(chat_id, {})
            if users:
                _, name = random.choice(list(users.items()))
                await context.bot.send_message(chat_id, random_tag_phrase(name))

        elif action == "message":
            await context.bot.send_message(chat_id, ask_ai("скажи что-нибудь внезапное и тупое"))

        elif action == "copy":
            history = recent_messages.get(chat_id, [])
            if history:
                _, text = random.choice(history)
                await context.bot.send_message(chat_id, text)

    except Exception as e:
        log.exception(f"Chaos job error in chat {chat_id}: {e}")


# ---------- START ----------

def main() -> None:
    # Сначала HTTP-сервер, чтобы Render видел открытый порт.
    Thread(target=run_web_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Telegram-бот запущен через polling. Render Web Service готов для UptimeRobot.")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
