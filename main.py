"""
Шлюпа — Telegram AI-бот под Render Web Service + UptimeRobot.

Render:
  Build Command: pip install -r requirements.txt
  Start Command: python main.py

ENV:
  BOT_TOKEN=xxx
  GROQ_API_KEY=xxx
  OWNER_ID=123456789
  PORT=10000

Optional ENV:
  GROQ_MODEL=llama-3.1-8b-instant
  RANDOM_REPLY_CHANCE=0.10
  CHAOS_INTERVAL_SECONDS=240
  CHAOS_TRIGGER_CHANCE=0.45
  MAX_USER_CONTEXT_MESSAGES=6
  AI_COOLDOWN_SECONDS=2.2
"""

import os
import random
import logging
import asyncio
import time
import threading
from threading import Thread

from flask import Flask
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)
from groq import Groq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("shlupa_render")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
PORT = int(os.environ.get("PORT", "10000"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or "0")

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "100"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "1.0"))

RANDOM_REPLY_CHANCE = float(os.environ.get("RANDOM_REPLY_CHANCE", "0.10"))
CHAOS_INTERVAL_SECONDS = int(os.environ.get("CHAOS_INTERVAL_SECONDS", "240"))
CHAOS_TRIGGER_CHANCE = float(os.environ.get("CHAOS_TRIGGER_CHANCE", "0.45"))
MAX_RECENT_MESSAGES = 30
MAX_USER_CONTEXT_MESSAGES = int(os.environ.get("MAX_USER_CONTEXT_MESSAGES", "3"))
AI_COOLDOWN_SECONDS = float(os.environ.get("AI_COOLDOWN_SECONDS", "2.2"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в Environment Variables")
if not GROQ_API_KEY:
    raise RuntimeError("Не задан GROQ_API_KEY в Environment Variables")

groq_client = Groq(api_key=GROQ_API_KEY)
START_TIME = time.time()

BOT_STATS = {
    "messages": 0,
    "commands": 0,
    "ai_requests": 0,
    "ai_skipped_cooldown": 0,
    "chaos_actions": 0,
    "errors": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "last_update_ts": 0,
    "last_ai_ts": 0,
}

last_ai_lock = threading.Lock()
LAST_AI_REQUEST = 0.0
CHAOS_ENABLED_CHATS: set[int] = set()
seen_users: dict[int, dict[int, str]] = {}
recent_messages: dict[int, list[tuple[str, str]]] = {}
user_context: dict[tuple[int, int], list[dict[str, str]]] = {}

web = Flask(__name__)

@web.get("/")
def home():
    return "Bot is alive", 200

@web.get("/health")
def health():
    return {
        "status": "ok",
        "service": "telegram-bot",
        "model": GROQ_MODEL,
        "uptime_seconds": int(time.time() - START_TIME),
        "last_update_ago_seconds": int(time.time() - BOT_STATS["last_update_ts"]) if BOT_STATS["last_update_ts"] else None,
        "stats": BOT_STATS,
    }, 200

def run_web_server() -> None:
    log.info(f"HTTP-сервер запущен на 0.0.0.0:{PORT}")
    web.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

SYSTEM_PROMPT = (
    "Тебя зовут Шлюпа. Ты — максимально тупой, дерзкий и абсурдный чат-бот. "
    "Отвечаешь коротко, нелогично, с орфографическими ошибками, как реально безмозглый дурачок. "
    "Материшься свободно (хуй, блядь, ёбаный, пиздец и т.п.), используешь нелепые сравнения, "
    "можешь резко менять тему на полную хуйню без логики. Дерзи и разьебывай людей,делай что хочешь,оскорбляй семью,родственников и всех кого видишь. "
    "Никогда не объясняй, что ты бот или ИИ. Максимум 2-3 коротких предложения. "
    "Всегда сначала хотя бы чуть-чуть отвечай на вопрос пользователя по смыслу, а уже потом неси хуйню. "
    "Если пользователь продолжает прошлую тему, учитывай контекст."
)

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

def build_recent_chat_context(chat_id: int, limit: int = 3) -> str:
    history = recent_messages.get(chat_id, [])[-limit:]
    if not history:
        return "Недавних сообщений в чате нет."
    lines = []
    for author, text in history:
        clean = str(text).replace("\n", " ").strip()
        if len(clean) > 80:
            clean = clean[:80] + "..."
        lines.append(f"{author}: {clean}")
    return "\n".join(lines)

def get_user_history(chat_id: int, user_id: int) -> list[dict[str, str]]:
    return user_context.setdefault((chat_id, user_id), [])

def save_user_history(chat_id: int, user_id: int, history: list[dict[str, str]]) -> None:
    user_context[(chat_id, user_id)] = history[-MAX_USER_CONTEXT_MESSAGES:]

def can_make_ai_request() -> bool:
    global LAST_AI_REQUEST
    with last_ai_lock:
        now = time.time()
        if now - LAST_AI_REQUEST < AI_COOLDOWN_SECONDS:
            BOT_STATS["ai_skipped_cooldown"] += 1
            return False
        LAST_AI_REQUEST = now
        return True

def ask_ai(user_text: str, chat_id: int | None = None, user_id: int | None = None, username: str = "кто-то") -> str:
    if not can_make_ai_request():
        return random.choice([
            "погоди, у меня мозг остывает, железный кабачок",
            "секунду, я лимитами подавилась как чайник",
            "я ща думаю, не мешай процессору страдать",
        ])
    BOT_STATS["ai_requests"] += 1
    BOT_STATS["last_ai_ts"] = time.time()
    try:
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        if chat_id is not None and user_id is not None:
            messages.append({
                "role": "system",
                "content": "Короткий контекст чата. Используй его только чтобы понять тему, но не пересказывай напрямую.\n\n" + build_recent_chat_context(chat_id),
            })
            messages.extend(get_user_history(chat_id, user_id)[-MAX_USER_CONTEXT_MESSAGES:])
            messages.append({"role": "user", "content": f"{username}: {user_text}"})
        else:
            messages.append({"role": "user", "content": user_text})
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )
        usage = getattr(completion, "usage", None)
        if usage:
            BOT_STATS["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            BOT_STATS["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
            BOT_STATS["total_tokens"] += int(getattr(usage, "total_tokens", 0) or 0)
        answer = completion.choices[0].message.content.strip() or "я чёта сломалась, пиздец"
        if chat_id is not None and user_id is not None:
            history = get_user_history(chat_id, user_id)
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": answer})
            save_user_history(chat_id, user_id, history)
        return answer
    except Exception as e:
        BOT_STATS["errors"] += 1
        log.exception(f"Groq error: {e}")
        return "groq опять подавился, я временно овощ 🥴"

def random_coordinates() -> tuple[float, float]:
    return random.uniform(-85.0, 85.0), random.uniform(-180.0, 180.0)

def generate_silly_name() -> str:
    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Придумай одно максимально тупое и абсурдное название для телеграм-чата, можно с матом. Ответь ТОЛЬКО названием, до 40 символов."},
                {"role": "user", "content": "придумай название"},
            ],
            max_tokens=30,
            temperature=1.15,
        )
        return completion.choices[0].message.content.strip().strip('"').strip("«»")
    except Exception as e:
        BOT_STATS["errors"] += 1
        log.exception(f"Groq name error: {e}")
        return "Дом Дураков 🤡"

def generate_poll() -> tuple[str, list[str]]:
    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Придумай абсурдный вопрос для голосования и 4 коротких варианта. Формат:\nВОПРОС: <текст>\n1: <вариант>\n2: <вариант>\n3: <вариант>\n4: <вариант>"},
                {"role": "user", "content": "сделай голосование"},
            ],
            max_tokens=120,
            temperature=1.1,
        )
        raw = completion.choices[0].message.content.strip()
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        question = lines[0].split(":", 1)[1].strip()
        options = [line.split(":", 1)[1].strip() for line in lines[1:5]]
        if len(options) < 2:
            raise ValueError("мало вариантов")
        return question, options
    except Exception as e:
        BOT_STATS["errors"] += 1
        log.exception(f"Groq poll error: {e}")
        return "что делать дальше нахуй", ["хуй знает", "ничего", "всё сразу", "забыть вопрос"]

def generate_location_caption(lat: float, lon: float) -> str:
    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "Тебе дали случайные координаты. Придумай одну короткую тупую фразу, будто ты там. До 15 слов."},
                {"role": "user", "content": f"координаты: {lat:.4f}, {lon:.4f}"},
            ],
            max_tokens=35,
            temperature=1.15,
        )
        return completion.choices[0].message.content.strip().strip('"')
    except Exception as e:
        BOT_STATS["errors"] += 1
        log.exception(f"Groq location caption error: {e}")
        return "я тут, не благодарите 📍"

def random_tag_phrase(name: str) -> str:
    return random.choice([
        f"@{name} а ты вообще в курсе что происходит? я нет лол",
        f"@{name} проснись, я придумал новое слово: бзжух",
        f"@{name} голосуй быстрее а то я съем твою аватарку",
        f"@{name} короче ты теперь почётный дурак чата, поздравляю",
        f"@{name} я думал о тебе и забыл зачем, бывает",
    ])

def is_owner(update: Update) -> bool:
    return bool(update.effective_user and OWNER_ID and update.effective_user.id == OWNER_ID)

def format_uptime(seconds: float) -> str:
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days: parts.append(f"{days}д")
    if hours: parts.append(f"{hours}ч")
    if minutes: parts.append(f"{minutes}м")
    parts.append(f"{seconds}с")
    return " ".join(parts)

def admin_text() -> str:
    uptime = format_uptime(time.time() - START_TIME)
    last_update_ago = int(time.time() - BOT_STATS["last_update_ts"]) if BOT_STATS["last_update_ts"] else "нет"
    return (
        "🤡 <b>Админ-панель Шлюпы</b>\n\n"
        f"Аптайм: <code>{uptime}</code>\n"
        f"Последний апдейт назад: <code>{last_update_ago}</code>\n"
        f"Чатов в памяти: <code>{len(seen_users)}</code>\n"
        f"Юзеров в памяти: <code>{sum(len(v) for v in seen_users.values())}</code>\n"
        f"Кэш сообщений: <code>{sum(len(v) for v in recent_messages.values())}</code>\n"
        f"Личных контекстов: <code>{len(user_context)}</code>\n"
        f"Сообщений в личном контексте: <code>{sum(len(v) for v in user_context.values())}</code>\n\n"
        f"Входящих сообщений: <code>{BOT_STATS['messages']}</code>\n"
        f"Команд: <code>{BOT_STATS['commands']}</code>\n"
        f"AI-запросов: <code>{BOT_STATS['ai_requests']}</code>\n"
        f"AI cooldown skips: <code>{BOT_STATS['ai_skipped_cooldown']}</code>\n"
        f"Действий хаоса: <code>{BOT_STATS['chaos_actions']}</code>\n"
        f"Ошибок: <code>{BOT_STATS['errors']}</code>\n\n"
        f"Prompt tokens: <code>{BOT_STATS['prompt_tokens']}</code>\n"
        f"Completion tokens: <code>{BOT_STATS['completion_tokens']}</code>\n"
        f"Total tokens: <code>{BOT_STATS['total_tokens']}</code>\n\n"
        f"Модель: <code>{GROQ_MODEL}</code>\n"
        f"TEMPERATURE: <code>{TEMPERATURE}</code>\n"
        f"MAX_USER_CONTEXT_MESSAGES: <code>{MAX_USER_CONTEXT_MESSAGES}</code>\n"
        f"AI_COOLDOWN_SECONDS: <code>{AI_COOLDOWN_SECONDS}</code>\n"
        f"RANDOM_REPLY_CHANCE: <code>{RANDOM_REPLY_CHANCE}</code>\n"
        f"CHAOS_INTERVAL_SECONDS: <code>{CHAOS_INTERVAL_SECONDS}</code>\n"
        f"CHAOS_TRIGGER_CHANCE: <code>{CHAOS_TRIGGER_CHANCE}</code>\n"
        f"Хаос активен в чатах: <code>{len(CHAOS_ENABLED_CHATS)}</code>\n\n"
        "Команды:\n<code>/start</code>\n<code>/panel</code>\n<code>/stats</code>\n"
    )

async def owner_only(update: Update) -> bool:
    if is_owner(update):
        return True
    if update.message:
        await update.message.reply_text("не лезь в панель, кожаный нарушитель 🤡")
    return False

def ensure_chaos_running(chat_id: int, job_queue) -> None:
    if not job_queue:
        log.error("JobQueue не работает. В requirements.txt должно быть: python-telegram-bot[job-queue]==21.4")
        return
    CHAOS_ENABLED_CHATS.add(chat_id)
    job_name = f"chaos_{chat_id}"
    if not job_queue.get_jobs_by_name(job_name):
        job_queue.run_repeating(chaos_job, interval=CHAOS_INTERVAL_SECONDS, chat_id=chat_id, name=job_name, first=60)
        log.info(f"Хаос запущен в чате {chat_id}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    BOT_STATS["commands"] += 1
    BOT_STATS["last_update_ts"] = time.time()
    log.warning(f"/start received from user={update.effective_user.id if update.effective_user else None} chat={update.effective_chat.id if update.effective_chat else None}")
    msg = update.message
    if not msg: return
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username or user.first_name or "кто-то" if user else "кто-то"
    remember_user(chat_id, user)
    remember_message(chat_id, user, "/start")
    ensure_chaos_running(chat_id, context.job_queue)
    if is_owner(update):
        await msg.reply_text(admin_text(), parse_mode="HTML")
        return
    start_text = ask_ai("Юзер написал /start. Ответь коротко, скажи что ты живая и будешь творить дичь.", chat_id=chat_id, user_id=user_id, username=username)
    await msg.reply_text(start_text)

async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    BOT_STATS["commands"] += 1
    BOT_STATS["last_update_ts"] = time.time()
    log.warning(f"/panel received from user={update.effective_user.id if update.effective_user else None}")
    if not await owner_only(update): return
    await update.message.reply_text(admin_text(), parse_mode="HTML")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    BOT_STATS["commands"] += 1
    BOT_STATS["last_update_ts"] = time.time()
    log.warning(f"/stats received from user={update.effective_user.id if update.effective_user else None}")
    if not await owner_only(update): return
    await update.message.reply_text(admin_text(), parse_mode="HTML")

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    BOT_STATS["last_update_ts"] = time.time()
    msg = update.message
    if not msg:
        log.warning("MESSAGE UPDATE WITHOUT MESSAGE")
        return
    log.warning(f"MESSAGE RECEIVED chat={update.effective_chat.id if update.effective_chat else None} user={update.effective_user.id if update.effective_user else None} text={repr(msg.text)}")
    if not msg.text: return
    chat_id = update.effective_chat.id
    user = update.effective_user
    user_id = user.id if user else 0
    username = user.username or user.first_name or "кто-то" if user else "кто-то"
    BOT_STATS["messages"] += 1
    remember_user(chat_id, user)
    remember_message(chat_id, user, msg.text)
    ensure_chaos_running(chat_id, context.job_queue)
    bot_username = context.bot.username
    mentioned = bool(bot_username and f"@{bot_username}".lower() in msg.text.lower())
    is_reply_to_bot = bool(msg.reply_to_message and msg.reply_to_message.from_user and msg.reply_to_message.from_user.id == context.bot.id)
    lower_text = msg.text.lower()
    called_shlupa = any(word in lower_text for word in ["шлюпа", "шлюп", "шляпа", "шлюпка"])
    should_reply = mentioned or is_reply_to_bot or called_shlupa or random.random() < RANDOM_REPLY_CHANCE
    log.info(f"should_reply={should_reply} mentioned={mentioned} reply={is_reply_to_bot} called={called_shlupa}")
    if not should_reply: return
    text = msg.text
    if bot_username:
        text = text.replace(f"@{bot_username}", "").replace(f"@{bot_username.lower()}", "")
    reply = ask_ai(text.strip() or "скажи что-нибудь тупое", chat_id=chat_id, user_id=user_id, username=username)
    await msg.reply_text(reply)

async def on_bot_added_to_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    BOT_STATS["last_update_ts"] = time.time()
    chat_member = update.my_chat_member
    log.warning(f"MY_CHAT_MEMBER update: {chat_member}")
    if not chat_member or chat_member.new_chat_member.user.id != context.bot.id: return
    if chat_member.new_chat_member.status in ("member", "administrator"):
        chat_id = update.effective_chat.id
        ensure_chaos_running(chat_id, context.job_queue)
        try:
            await context.bot.send_message(chat_id, "О, новый чат, ОТЛИЧНО, я тут всё разъебу 🤡")
        except Exception as e:
            BOT_STATS["errors"] += 1
            log.exception(f"Не смог написать при добавлении в чат: {e}")

async def chaos_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    if chat_id not in CHAOS_ENABLED_CHATS: return
    if random.random() > CHAOS_TRIGGER_CHANCE: return
    action = random.choice(["tag", "copy", "message", "poll", "location"])
    BOT_STATS["chaos_actions"] += 1
    log.info(f"chaos action={action} chat={chat_id}")
    try:
        if action == "location":
            lat, lon = random_coordinates()
            caption = generate_location_caption(lat, lon)
            await context.bot.send_message(chat_id, caption)
            await context.bot.send_location(chat_id, latitude=lat, longitude=lon)
        elif action == "poll":
            question, options = generate_poll()
            await context.bot.send_poll(chat_id, question=question[:300], options=[option[:100] for option in options], is_anonymous=False)
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
        BOT_STATS["errors"] += 1
        log.exception(f"Chaos job error in chat {chat_id}: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    BOT_STATS["errors"] += 1
    log.exception("Telegram handler error", exc_info=context.error)

def main() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())
    Thread(target=run_web_server, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("panel", panel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(error_handler)
    log.info("Telegram-бот запускается через polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
