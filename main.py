"""
Шлюпа — Telegram AI-бот под Render Web Service + UptimeRobot.

Render:
  Build Command: pip install -r requirements.txt
  Start Command: python main.py

ENV:
  BOT_TOKEN=xxx
  OPENMODEL_API_KEY=xxx
  OWNER_ID=123456789
  PORT=10000

Optional ENV:
  OPENMODEL_MODEL=deepseek-v4-flash
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
from datetime import datetime, timezone
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
import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("shlupa_render")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OPENMODEL_API_KEY = os.environ.get("OPENMODEL_API_KEY", "").strip() or os.environ.get("GROQ_API_KEY", "").strip()
OPENMODEL_BASE_URL = os.environ.get("OPENMODEL_BASE_URL", "https://api.openmodel.ai").strip()
PORT = int(os.environ.get("PORT", "10000"))
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or "0")

GROQ_MODEL = os.environ.get("OPENMODEL_MODEL", os.environ.get("GROQ_MODEL", "deepseek-v4-flash"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "100"))
TEMPERATURE = float(os.environ.get("TEMPERATURE", "1.0"))

RANDOM_REPLY_CHANCE = float(os.environ.get("RANDOM_REPLY_CHANCE", "0.10"))
CHAOS_INTERVAL_SECONDS = int(os.environ.get("CHAOS_INTERVAL_SECONDS", "240"))
CHAOS_TRIGGER_CHANCE = float(os.environ.get("CHAOS_TRIGGER_CHANCE", "0.45"))
MAX_RECENT_MESSAGES = 30
MAX_USER_CONTEXT_MESSAGES = int(os.environ.get("MAX_USER_CONTEXT_MESSAGES", "3"))
AI_COOLDOWN_SECONDS = float(os.environ.get("AI_COOLDOWN_SECONDS", "2.2"))
DAILY_USER_TOKEN_LIMIT = int(os.environ.get("DAILY_USER_TOKEN_LIMIT", "5000"))
TITLE_OF_DAY_HOUR_UTC = int(os.environ.get("TITLE_OF_DAY_HOUR_UTC", "12"))
RENAME_COOLDOWN_SECONDS = int(os.environ.get("RENAME_COOLDOWN_SECONDS", "21600"))
LEAVE_GROUP_CHANCE = float(os.environ.get("LEAVE_GROUP_CHANCE", "0.01"))

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в Environment Variables")
if not OPENMODEL_API_KEY:
    raise RuntimeError("Не задан OPENMODEL_API_KEY в Environment Variables")

openmodel_client = anthropic.Anthropic(
    api_key=OPENMODEL_API_KEY,
    base_url=OPENMODEL_BASE_URL,
)


class _CompatMessage:
    def __init__(self, content: str):
        self.content = content


class _CompatChoice:
    def __init__(self, content: str):
        self.message = _CompatMessage(content)


class _CompatUsage:
    def __init__(self, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.prompt_tokens = int(prompt_tokens or 0)
        self.completion_tokens = int(completion_tokens or 0)
        self.total_tokens = self.prompt_tokens + self.completion_tokens


class _CompatCompletion:
    def __init__(self, text: str, usage: _CompatUsage):
        self.choices = [_CompatChoice(text)]
        self.usage = usage


class _OpenModelCompletions:
    def create(self, model: str, messages: list[dict], max_tokens: int, temperature: float = 1.0, **kwargs):
        system_parts = []
        chat_messages = []

        for msg in messages:
            role = msg.get("role", "user")
            content = str(msg.get("content", ""))

            if role == "system":
                system_parts.append(content)
            elif role in ("user", "assistant"):
                chat_messages.append({"role": role, "content": content})

        if not chat_messages:
            chat_messages = [{"role": "user", "content": "скажи что-нибудь"}]

        response = openmodel_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system="\n\n".join(system_parts) if system_parts else None,
            messages=chat_messages,
        )

        text_parts = []
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)

        text = "".join(text_parts).strip()

        usage = getattr(response, "usage", None)
        compat_usage = _CompatUsage(
            prompt_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
        )
        return _CompatCompletion(text, compat_usage)


class _OpenModelChat:
    def __init__(self):
        self.completions = _OpenModelCompletions()


class _OpenModelCompatClient:
    def __init__(self):
        self.chat = _OpenModelChat()


groq_client = _OpenModelCompatClient()
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
    "blocked_unactivated": 0,
    "daily_limit_hits": 0,
    "title_of_day_sent": 0,
    "last_update_ts": 0,
    "last_ai_ts": 0,
}

last_ai_lock = threading.Lock()
LAST_AI_REQUEST = 0.0
CHAOS_ENABLED_CHATS: set[int] = set()
seen_users: dict[int, dict[int, str]] = {}
recent_messages: dict[int, list[tuple[str, str]]] = {}
user_context: dict[tuple[int, int], list[dict[str, str]]] = {}

# Пользователи, которые написали /start в личке.
activated_users: set[int] = set()

# Дневной расход токенов по людям: {user_id: {"date": "YYYY-MM-DD", "tokens": int}}
user_daily_tokens: dict[int, dict[str, int | str]] = {}

# Титул дня по чатам: {chat_id: "YYYY-MM-DD"}
title_of_day_state: dict[int, str] = {}
last_rename_time: dict[int, float] = {}

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


# ---------- АКТИВАЦИЯ / ЛИМИТЫ / ТИТУЛ ДНЯ ----------

def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def is_private_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


def is_user_activated(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    return user_id in activated_users


def get_user_daily_tokens(user_id: int) -> int:
    day = today_key()
    data = user_daily_tokens.get(user_id)

    if not data or data.get("date") != day:
        user_daily_tokens[user_id] = {"date": day, "tokens": 0}
        return 0

    return int(data.get("tokens", 0) or 0)


def add_user_daily_tokens(user_id: int, tokens: int) -> None:
    if user_id == OWNER_ID:
        return

    day = today_key()
    current = get_user_daily_tokens(user_id)
    user_daily_tokens[user_id] = {
        "date": day,
        "tokens": current + int(tokens or 0),
    }


def has_daily_tokens_left(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    return get_user_daily_tokens(user_id) < DAILY_USER_TOKEN_LIMIT


def activation_text(bot_username: str | None = None) -> str:
    if bot_username:
        return (
            "🤡 э, ты ещё не активировал Шлюпу\n\n"
            f"напиши мне в личку: @{bot_username}\n"
            "и жмакни /start\n\n"
            "потом вернёшься сюда, кабачок недонастроенный"
        )
    return (
        "🤡 э, ты ещё не активировал Шлюпу\n\n"
        "напиши мне в личку /start\n"
        "потом возвращайся сюда, кабачок"
    )


def limit_text() -> str:
    return (
        "🤡 дневной лимит токенов сожран\n\n"
        "на сегодня всё, приходи завтра\n"
        "я не резиновая, кожаный пылесос"
    )


async def notify_limit_private(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    try:
        await context.bot.send_message(chat_id=user_id, text=limit_text())
    except Exception as e:
        log.warning(f"Не смог отправить лимит в ЛС user={user_id}: {e}")


def make_title_of_day(users: dict[int, str]) -> str | None:
    if not users:
        return None

    _, name = random.choice(list(users.items()))
    titles = [
        "главный кабачок дня",
        "министр арбузной промышленности",
        "почётный мыслитель табуретки",
        "рыцарь кривого вайба",
        "генерал диванных войск",
        "официальный хранитель пельменя",
        "магистр подозрительного чая",
        "главный овощ конференции",
        "президент случайной хуйни",
        "князь мокрого асфальта",
    ]
    reasons = [
        "слишком уверенно молчал",
        "выглядел как человек, который спорит с микроволновкой",
        "по энергетике сегодня победил холодильник",
        "чат сам так решил, я просто ору",
        "подозрительно долго существовал",
        "его аура пахнет системным блоком",
        "так совпали звёзды и мой сломанный процессор",
        "иначе вселенная бы не загрузилась",
    ]
    return (
        "🏆 ТИТУЛ ДНЯ\n\n"
        f"Сегодня <b>{random.choice(titles)}</b>:\n"
        f"@{name}\n\n"
        f"Причина: {random.choice(reasons)} 🤡"
    )


async def maybe_send_title_of_day(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    day = today_key()
    if title_of_day_state.get(chat_id) == day:
        return

    now = datetime.now(timezone.utc)
    if now.hour < TITLE_OF_DAY_HOUR_UTC:
        return

    title = make_title_of_day(seen_users.get(chat_id, {}))
    if not title:
        return

    try:
        await context.bot.send_message(chat_id, title, parse_mode="HTML")
        title_of_day_state[chat_id] = day
        BOT_STATS["title_of_day_sent"] += 1
    except Exception as e:
        BOT_STATS["errors"] += 1
        log.exception(f"Title of day error in chat {chat_id}: {e}")


def ask_ai(user_text: str, chat_id: int | None = None, user_id: int | None = None, username: str = "кто-то", ignore_user_limit: bool = False) -> str:
    if user_id is not None and not ignore_user_limit and not has_daily_tokens_left(user_id):
        BOT_STATS["daily_limit_hits"] += 1
        return limit_text()

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
            total_used = int(getattr(usage, "total_tokens", 0) or 0)
            BOT_STATS["total_tokens"] += total_used
            if user_id is not None and not ignore_user_limit:
                add_user_daily_tokens(user_id, total_used)
        answer = completion.choices[0].message.content.strip() or "я чёта сломалась, пиздец"
        if chat_id is not None and user_id is not None:
            history = get_user_history(chat_id, user_id)
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": answer})
            save_user_history(chat_id, user_id, history)
        return answer
    except Exception as e:
        BOT_STATS["errors"] += 1
        log.exception(f"OpenModel error: {e}")
        return "openmodel опять подавился, я временно овощ 🥴"

def random_coordinates() -> tuple[float, float]:
    return random.uniform(-85.0, 85.0), random.uniform(-180.0, 180.0)

def generate_silly_name() -> str:
    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "придумай максимально уебанское, дебильное, глупое и бомжатское название для чата. "
                        "нужно использовать русский мат. "
                        "название должно выглядеть как максимальное несуразное чудо, которое могла придумать только шлюпа. "
                        "до 40 символов. "
                        "ответь только названием. "
                        "без кавычек. "
                        "без пояснений."
                    ),
                },
                {"role": "user", "content": "придумай название"},
            ],
            max_tokens=35,
            temperature=1.35,
        )

        title = completion.choices[0].message.content.strip().strip('"').strip("«»").lower()
        return title[:40]

    except Exception as e:
        BOT_STATS["errors"] += 1
        log.exception(f"OpenModel name error: {e}")
        return "министерство ебаных кабачков"


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
        log.exception(f"OpenModel poll error: {e}")
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
        log.exception(f"OpenModel location caption error: {e}")
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


def find_known_user_name(user_id: int) -> str:
    for users in seen_users.values():
        if user_id in users:
            return users[user_id]
    return str(user_id)


def top_token_users_text(limit: int = 10) -> str:
    day = today_key()
    rows = []

    for user_id, data in user_daily_tokens.items():
        if data.get("date") != day:
            continue

        tokens = int(data.get("tokens", 0) or 0)
        if tokens <= 0:
            continue

        name = find_known_user_name(int(user_id))
        rows.append((tokens, int(user_id), name))

    rows.sort(reverse=True)

    if not rows:
        return "Топ токеножоров сегодня: <code>пока пусто</code>\n"

    lines = ["<b>ТОП токеножоров сегодня:</b>"]
    for i, (tokens, user_id, name) in enumerate(rows[:limit], start=1):
        if user_id == OWNER_ID:
            limit_text_part = "∞"
        else:
            limit_text_part = str(DAILY_USER_TOKEN_LIMIT)

        safe_name = str(name).replace("<", "").replace(">", "")
        lines.append(f"{i}. @{safe_name} — <code>{tokens}/{limit_text_part}</code>")

    return "\n".join(lines) + "\n"


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
        f"Ошибок: <code>{BOT_STATS['errors']}</code>\n"
        f"Неактивированных стопнуло: <code>{BOT_STATS['blocked_unactivated']}</code>\n"
        f"Уперлись в дневной лимит: <code>{BOT_STATS['daily_limit_hits']}</code>\n"
        f"Титулов дня выдано: <code>{BOT_STATS['title_of_day_sent']}</code>\n\n"
        f"Prompt tokens: <code>{BOT_STATS['prompt_tokens']}</code>\n"
        f"Completion tokens: <code>{BOT_STATS['completion_tokens']}</code>\n"
        f"Total tokens: <code>{BOT_STATS['total_tokens']}</code>\n\n"
        f"{top_token_users_text()}\n"
        f"Провайдер: <code>OpenModel / DeepSeek</code>\n"
        f"Модель: <code>{GROQ_MODEL}</code>\n"
        f"TEMPERATURE: <code>{TEMPERATURE}</code>\n"
        f"MAX_USER_CONTEXT_MESSAGES: <code>{MAX_USER_CONTEXT_MESSAGES}</code>\n"
        f"AI_COOLDOWN_SECONDS: <code>{AI_COOLDOWN_SECONDS}</code>\n"
        f"RANDOM_REPLY_CHANCE: <code>{RANDOM_REPLY_CHANCE}</code>\n"
        f"CHAOS_INTERVAL_SECONDS: <code>{CHAOS_INTERVAL_SECONDS}</code>\n"
        f"CHAOS_TRIGGER_CHANCE: <code>{CHAOS_TRIGGER_CHANCE}</code>\n"
        f"RENAME_COOLDOWN_SECONDS: <code>{RENAME_COOLDOWN_SECONDS}</code>\n"
        f"LEAVE_GROUP_CHANCE: <code>{LEAVE_GROUP_CHANCE}</code>\n"
        f"Хаос активен в чатах: <code>{len(CHAOS_ENABLED_CHATS)}</code>\n\n"
        "Команды:\n<code>/start</code>\n<code>/panel</code>\n"
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

    if is_private_chat(update) and user_id:
        activated_users.add(user_id)
        get_user_daily_tokens(user_id)

    ensure_chaos_running(chat_id, context.job_queue)
    if is_owner(update):
        await msg.reply_text(admin_text(), parse_mode="HTML")
        return

    if not is_private_chat(update):
        await msg.reply_text(activation_text(context.bot.username))
        return

    start_text = ask_ai(
        "Юзер написал /start в личке. Скажи коротко, что активация готова и теперь можно писать тебе в чатах.",
        chat_id=chat_id,
        user_id=user_id,
        username=username,
    )
    await msg.reply_text(start_text)

async def panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    BOT_STATS["commands"] += 1
    BOT_STATS["last_update_ts"] = time.time()
    log.warning(f"/panel received from user={update.effective_user.id if update.effective_user else None}")
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

    # В личке Шлюпа отвечает на любой текст.
    # В группе — на упоминание, ответ на её сообщение, слово "шлюпа" или рандомный шанс.
    private_chat = bool(update.effective_chat and update.effective_chat.type == "private")
    should_reply = private_chat or mentioned or is_reply_to_bot or called_shlupa or random.random() < RANDOM_REPLY_CHANCE

    log.info(
        f"should_reply={should_reply} private={private_chat} "
        f"mentioned={mentioned} reply={is_reply_to_bot} called={called_shlupa}"
    )

    if not should_reply:
        return

    # Если юзер не активировал бота в ЛС — не тратим Groq, а даём готовый текст.
    if not is_user_activated(user_id):
        BOT_STATS["blocked_unactivated"] += 1
        await msg.reply_text(activation_text(bot_username))
        return

    # Если дневной лимит юзера сожран — тоже не тратим Groq.
    if not has_daily_tokens_left(user_id):
        BOT_STATS["daily_limit_hits"] += 1
        await msg.reply_text(limit_text())
        await notify_limit_private(context, user_id)
        return

    text = msg.text
    if bot_username:
        text = text.replace(f"@{bot_username}", "").replace(f"@{bot_username.lower()}", "")

    reply = ask_ai(
        text.strip() or "скажи что-нибудь тупое",
        chat_id=chat_id,
        user_id=user_id,
        username=username,
    )
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


def can_rename_chat(chat_id: int) -> bool:
    now = time.time()
    last = last_rename_time.get(chat_id, 0)
    return now - last >= RENAME_COOLDOWN_SECONDS


def mark_chat_renamed(chat_id: int) -> None:
    last_rename_time[chat_id] = time.time()


def random_leave_phrase() -> str:
    return random.choice([
        "я ухожу из этой помойки 🤡",
        "вы меня морально уронили в суп",
        "чат официально признан овощебазой",
        "я посмотрела на это и решила ливнуть",
        "вы меня заебали, пока",
        "шлюпа покидает корабль, дальше тоните сами",
        "я вышла, потому что тут пахнет коллективным пиздецом",
    ]).lower()


async def chaos_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id

    await maybe_send_title_of_day(context, chat_id)

    if chat_id not in CHAOS_ENABLED_CHATS:
        return

    if random.random() > CHAOS_TRIGGER_CHANCE:
        return

    action = random.choice([
        "rename",
        "tag",
        "copy",
        "message",
        "poll",
        "location",
        "leave",
    ])

    BOT_STATS["chaos_actions"] += 1
    log.info(f"chaos action={action} chat={chat_id}")

    try:
        if action == "rename":
            if not can_rename_chat(chat_id):
                return

            title = generate_silly_name()

            try:
                await context.bot.set_chat_title(chat_id, title[:128])
                mark_chat_renamed(chat_id)
                await context.bot.send_message(
                    chat_id,
                    f"🤡 переименовала этот цирк в:\n{title}"
                )
            except Exception as e:
                BOT_STATS["errors"] += 1
                log.warning(f"rename failed in chat {chat_id}: {e}")

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
            await context.bot.send_message(
                chat_id,
                ask_ai("скажи что-нибудь внезапное и тупое")
            )

        elif action == "copy":
            history = recent_messages.get(chat_id, [])
            if history:
                _, text = random.choice(history)
                await context.bot.send_message(chat_id, text)

        elif action == "leave":
            if random.random() < LEAVE_GROUP_CHANCE:
                await context.bot.send_message(chat_id, random_leave_phrase())
                try:
                    await context.bot.leave_chat(chat_id)
                except Exception as e:
                    BOT_STATS["errors"] += 1
                    log.warning(f"leave failed in chat {chat_id}: {e}")

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
    app.add_handler(ChatMemberHandler(on_bot_added_to_chat, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(error_handler)
    log.info("Telegram-бот запускается через polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
