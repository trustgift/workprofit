#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram UserBot Manager - ДЛЯ RENDER
Адаптирован для работы на Render.com
"""

import os
import sys
import asyncio
import logging
import re
import sqlite3
import signal
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================================
# ОБРАБОТКА СИГНАЛОВ ДЛЯ КОРРЕКТНОЙ ОСТАНОВКИ
# ============================================================
def signal_handler(sig, frame):
    print(f"\n⏹ Получен сигнал {sig}. Останавливаю бота...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# ============================================================
# CONFIG (ЗАМЕНИТЕ НА СВОИ ДАННЫЕ)
# ============================================================
BOT_TOKEN = "8948221161:AAHLPfFUmK1QyRGVcaM8UVchByrxAmCkA8s"
API_ID = 26259835
API_HASH = "3fa32264398920f001dd2428b42060f6"

ADMIN_IDS = {8986358602,8566976864
}

# Для Render - используем /tmp для временных файлов
BASE_DIR = Path("/tmp") if os.path.exists("/tmp") else Path(".")
SESSIONS_DIR = BASE_DIR / "sessions"
DATABASE_FILE = BASE_DIR / "data.db"

# Создаём папки
SESSIONS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("userbot-manager")

USERNAME_RE = re.compile(r"(?<![\w])@([A-Za-z0-9_]{5,32})(?![\w])")

# Conversation states
(
    AUTH_PHONE,
    AUTH_CODE,
    AUTH_PASSWORD,
    ADD_GROUP,
    ADD_REPLY_GROUP,
    EDIT_TEXT_1,
    EDIT_TEXT_2,
    MANUAL_USERNAME,
) = range(8)

# ============================================================
# ЗАПУСК ВЕБ-СЕРВЕРА ДЛЯ RENDER
# ============================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    logger.info(f"✅ Health check server running on port {port}")
    server.serve_forever()

# Запускаем в отдельном потоке
health_thread = threading.Thread(target=run_health_server, daemon=True)
health_thread.start()

# ============================================================
# ИМПОРТЫ (после настройки, чтобы не было конфликтов)
# ============================================================
from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
    UserNotMutualContactError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    RPCError,
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

# ============================================================
# DATABASE
# ============================================================
class Database:
    def __init__(self, path: Path):
        self.path = str(path)
        self._init()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self):
        with closing(self.connect()) as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL UNIQUE,
                    session_name TEXT NOT NULL UNIQUE,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    is_monitoring INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS texts (
                    step INTEGER PRIMARY KEY,
                    text TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    user_id TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    first_sent_at TEXT,
                    replied_at TEXT,
                    second_sent_at TEXT,
                    last_error TEXT,
                    UNIQUE(account_id, username),
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    contact_id INTEGER,
                    account_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    user_id TEXT,
                    text TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE SET NULL,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS stats (
                    account_id INTEGER PRIMARY KEY,
                    sent_first INTEGER NOT NULL DEFAULT 0,
                    sent_second INTEGER NOT NULL DEFAULT 0,
                    replies INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
                );

                INSERT OR IGNORE INTO texts(step, text)
                VALUES(1, 'Привет!');

                INSERT OR IGNORE INTO texts(step, text)
                VALUES(2, 'Спасибо за ответ!');
                """
            )
            db.commit()

    # ---------- settings ----------
    def set_setting(self, key: str, value: str):
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            db.commit()

    def get_setting(self, key: str) -> Optional[str]:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row["value"] if row else None

    # ---------- accounts ----------
    def add_account(self, phone: str, session_name: str) -> int:
        with closing(self.connect()) as db:
            cur = db.execute(
                "INSERT INTO accounts(phone,session_name) VALUES(?,?)",
                (phone, session_name),
            )
            account_id = cur.lastrowid
            db.execute("INSERT OR IGNORE INTO stats(account_id) VALUES(?)", (account_id,))
            db.commit()
            return int(account_id)

    def get_accounts(self, include_inactive=True):
        with closing(self.connect()) as db:
            sql = "SELECT * FROM accounts"
            if not include_inactive:
                sql += " WHERE is_active=1"
            sql += " ORDER BY id"
            return [dict(x) for x in db.execute(sql).fetchall()]

    def get_account(self, account_id: int):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_account_by_phone(self, phone: str):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM accounts WHERE phone=?", (phone,)
            ).fetchone()
            return dict(row) if row else None

    def set_account_active(self, account_id: int, active: bool):
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE accounts SET is_active=? WHERE id=?",
                (int(active), account_id),
            )
            db.commit()

    def set_monitoring(self, account_id: int, enabled: bool):
        with closing(self.connect()) as db:
            db.execute(
                "UPDATE accounts SET is_monitoring=? WHERE id=?",
                (int(enabled), account_id),
            )
            db.commit()

    def delete_account(self, account_id: int):
        with closing(self.connect()) as db:
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            db.commit()

    # ---------- texts ----------
    def get_text(self, step: int) -> str:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT text FROM texts WHERE step=?", (step,)
            ).fetchone()
            return row["text"] if row else ""

    def set_text(self, step: int, text: str):
        with closing(self.connect()) as db:
            db.execute(
                "INSERT INTO texts(step,text) VALUES(?,?) "
                "ON CONFLICT(step) DO UPDATE SET text=excluded.text",
                (step, text),
            )
            db.commit()

    # ---------- contacts ----------
    def add_contact(
        self,
        account_id: int,
        username: str,
        source: str = "manual",
        user_id: Optional[str] = None,
    ):
        username = username.lstrip("@").strip().lower()
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM contacts WHERE account_id=? AND username=?",
                (account_id, username),
            ).fetchone()
            if row:
                return dict(row), False

            cur = db.execute(
                """
                INSERT INTO contacts(account_id,username,user_id,source,status)
                VALUES(?,?,?,?, 'pending')
                """,
                (account_id, username, user_id, source),
            )
            contact_id = cur.lastrowid
            db.commit()
            row = db.execute(
                "SELECT * FROM contacts WHERE id=?", (contact_id,)
            ).fetchone()
            return dict(row), True

    def get_contact(self, contact_id: int):
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM contacts WHERE id=?", (contact_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_contact_for_user(self, account_id: int, username: str):
        username = username.lstrip("@").lower()
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT * FROM contacts WHERE account_id=? AND username=?",
                (account_id, username),
            ).fetchone()
            return dict(row) if row else None

    def get_pending(self, limit=50):
        with closing(self.connect()) as db:
            rows = db.execute(
                """
                SELECT c.*, a.phone, a.session_name
                FROM contacts c
                JOIN accounts a ON a.id=c.account_id
                WHERE c.status='pending'
                ORDER BY c.id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(x) for x in rows]

    def set_contact_status(
        self,
        contact_id: int,
        status: str,
        *,
        error: Optional[str] = None,
    ):
        with closing(self.connect()) as db:
            db.execute(
                """
                UPDATE contacts
                SET status=?, last_error=?
                WHERE id=?
                """,
                (status, error, contact_id),
            )
            db.commit()

    def mark_first_sent(self, contact_id: int):
        with closing(self.connect()) as db:
            db.execute(
                """
                UPDATE contacts
                SET status='waiting_reply',
                    first_sent_at=CURRENT_TIMESTAMP,
                    last_error=NULL
                WHERE id=?
                """,
                (contact_id,),
            )
            db.commit()

    def mark_reply(self, contact_id: int):
        with closing(self.connect()) as db:
            db.execute(
                """
                UPDATE contacts
                SET status='reply_received',
                    replied_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (contact_id,),
            )
            db.commit()

    def mark_second_sent(self, contact_id: int):
        with closing(self.connect()) as db:
            db.execute(
                """
                UPDATE contacts
                SET status='second_sent',
                    second_sent_at=CURRENT_TIMESTAMP,
                    last_error=NULL
                WHERE id=?
                """,
                (contact_id,),
            )
            db.commit()

    def save_reply(
        self,
        contact_id: Optional[int],
        account_id: int,
        username: str,
        user_id: Optional[str],
        text: str,
    ):
        with closing(self.connect()) as db:
            db.execute(
                """
                INSERT INTO replies(contact_id,account_id,username,user_id,text)
                VALUES(?,?,?,?,?)
                """,
                (contact_id, account_id, username, user_id, text),
            )
            db.commit()

    def increment_stat(self, account_id: int, field: str):
        allowed = {"sent_first", "sent_second", "replies", "errors"}
        if field not in allowed:
            raise ValueError("Invalid stats field")
        with closing(self.connect()) as db:
            db.execute(
                f"UPDATE stats SET {field}={field}+1 WHERE account_id=?",
                (account_id,),
            )
            db.commit()

    def get_stats(self):
        with closing(self.connect()) as db:
            rows = db.execute(
                """
                SELECT a.id,a.phone,
                       COALESCE(s.sent_first,0) sent_first,
                       COALESCE(s.sent_second,0) sent_second,
                       COALESCE(s.replies,0) replies,
                       COALESCE(s.errors,0) errors
                FROM accounts a
                LEFT JOIN stats s ON s.account_id=a.id
                ORDER BY a.id
                """
            ).fetchall()
            return [dict(x) for x in rows]

    def pending_count(self):
        with closing(self.connect()) as db:
            return db.execute(
                "SELECT COUNT(*) FROM contacts WHERE status='pending'"
            ).fetchone()[0]


db = Database(DATABASE_FILE)

# ============================================================
# USERBOT MANAGER
# ============================================================
@dataclass
class RunningAccount:
    client: TelegramClient
    phone: str


class UserBotManager:
    def __init__(self, bot: "BotSystem"):
        self.bot = bot
        self.clients: dict[int, RunningAccount] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self.running = True

    def lock_for(self, account_id: int):
        if account_id not in self._locks:
            self._locks[account_id] = asyncio.Lock()
        return self._locks[account_id]

    async def start_account(self, account: dict) -> bool:
        account_id = account["id"]

        if account_id in self.clients:
            return True

        session_path = str(SESSIONS_DIR / account["session_name"])
        client = TelegramClient(session_path, API_ID, API_HASH)

        try:
            await client.connect()

            if not await client.is_user_authorized():
                logger.warning(
                    "Session %s is not authorized; account stays offline.",
                    account["phone"],
                )
                await client.disconnect()
                return False

            running = RunningAccount(client=client, phone=account["phone"])
            self.clients[account_id] = running

            @client.on(events.NewMessage(incoming=True))
            async def incoming_handler(event):
                try:
                    await self.handle_private_message(account_id, event)
                except Exception:
                    logger.exception("Private message handler failed")

            @client.on(events.NewMessage())
            async def group_handler(event):
                try:
                    await self.handle_group_message(account_id, event)
                except Exception:
                    logger.exception("Group message handler failed")

            logger.info("Account started: %s", account["phone"])
            return True

        except Exception:
            logger.exception("Cannot start account %s", account["phone"])
            try:
                await client.disconnect()
            except Exception:
                pass
            return False

    async def stop_account(self, account_id: int):
        running = self.clients.pop(account_id, None)
        if not running:
            return

        try:
            await running.client.disconnect()
        except Exception:
            logger.exception("Error stopping account %s", account_id)

    async def start_all(self):
        for account in db.get_accounts(include_inactive=False):
            await self.start_account(account)

    async def stop_all(self):
        self.running = False
        for account_id in list(self.clients):
            await self.stop_account(account_id)

    async def handle_group_message(self, account_id: int, event):
        account = db.get_account(account_id)
        if not account or not account["is_active"] or not account["is_monitoring"]:
            return

        group_id = db.get_setting("monitor_group_id")
        if not group_id:
            return

        if str(event.chat_id) != str(group_id):
            return

        text = event.raw_text or ""
        usernames = {m.group(1).lower() for m in USERNAME_RE.finditer(text)}
        if not usernames:
            return

        for username in usernames:
            contact, created = db.add_contact(
                account_id,
                username,
                source="monitor",
            )
            if created:
                await self.bot.notify_new_contact(contact)

    async def handle_private_message(self, account_id: int, event):
        if not event.is_private:
            return

        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False):
            return

        username = getattr(sender, "username", None)
        if not username:
            return

        contact = db.get_contact_for_user(account_id, username)

        if not contact or contact["status"] != "waiting_reply":
            return

        text = event.raw_text or ""
        user_id = str(getattr(sender, "id", "")) or None

        db.save_reply(
            contact["id"],
            account_id,
            username,
            user_id,
            text,
        )
        db.mark_reply(contact["id"])
        db.increment_stat(account_id, "replies")

        await self.bot.notify_reply(
            account_id,
            contact,
            text,
        )

        await self.send_second_message(contact["id"], account_id, username)

    async def send_second_message(
        self,
        contact_id: int,
        account_id: int,
        username: str,
    ) -> bool:
        text = db.get_text(2).strip()
        if not text:
            db.set_contact_status(
                contact_id,
                "error",
                error="Второй текст не установлен",
            )
            await self.bot.notify_error(
                account_id,
                username,
                "Второй текст не установлен",
            )
            return False

        return await self._send(
            contact_id,
            account_id,
            username,
            text,
            step=2,
        )

    async def send_first_message(self, contact_id: int) -> bool:
        contact = db.get_contact(contact_id)
        if not contact or contact["status"] != "pending":
            return False

        text = db.get_text(1).strip()
        if not text:
            await self.bot.notify_error(
                contact["account_id"],
                contact["username"],
                "Первый текст не установлен",
            )
            return False

        return await self._send(
            contact_id,
            contact["account_id"],
            contact["username"],
            text,
            step=1,
        )

    async def _send(
        self,
        contact_id: int,
        account_id: int,
        username: str,
        text: str,
        step: int,
    ) -> bool:
        running = self.clients.get(account_id)
        if not running:
            error = "Аккаунт не запущен"
            db.set_contact_status(contact_id, "error", error=error)
            db.increment_stat(account_id, "errors")
            await self.bot.notify_error(account_id, username, error)
            return False

        async with self.lock_for(account_id):
            try:
                entity = await running.client.get_entity(username)
                await running.client.send_message(entity, text)

                if step == 1:
                    db.mark_first_sent(contact_id)
                    db.increment_stat(account_id, "sent_first")
                else:
                    db.mark_second_sent(contact_id)
                    db.increment_stat(account_id, "sent_second")

                logger.info(
                    "Sent step %s from account %s to @%s",
                    step,
                    account_id,
                    username,
                )
                return True

            except FloodWaitError as e:
                error = f"FloodWait: Telegram просит подождать {e.seconds} сек."
                db.set_contact_status(contact_id, "error", error=error)
                db.increment_stat(account_id, "errors")
                await self.bot.notify_error(account_id, username, error)
                return False

            except PeerFloodError:
                error = "SPAM/FLOOD ограничение Telegram"
                db.set_contact_status(contact_id, "blocked", error=error)
                db.increment_stat(account_id, "errors")
                await self.bot.notify_error(account_id, username, error)
                return False

            except (UsernameInvalidError, UsernameNotOccupiedError) as e:
                error = f"Username недоступен: {type(e).__name__}"
                db.set_contact_status(contact_id, "error", error=error)
                db.increment_stat(account_id, "errors")
                await self.bot.notify_error(account_id, username, error)
                return False

            except UserPrivacyRestrictedError:
                error = "Пользователь ограничил получение сообщений"
                db.set_contact_status(contact_id, "error", error=error)
                db.increment_stat(account_id, "errors")
                await self.bot.notify_error(account_id, username, error)
                return False

            except UserNotMutualContactError:
                error = "Telegram не разрешил отправку этому пользователю"
                db.set_contact_status(contact_id, "error", error=error)
                db.increment_stat(account_id, "errors")
                await self.bot.notify_error(account_id, username, error)
                return False

            except RPCError as e:
                error = f"Telegram RPC error: {e}"
                db.set_contact_status(contact_id, "error", error=error)
                db.increment_stat(account_id, "errors")
                await self.bot.notify_error(account_id, username, error)
                return False

            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                db.set_contact_status(contact_id, "error", error=error)
                db.increment_stat(account_id, "errors")
                await self.bot.notify_error(account_id, username, error)
                logger.exception("Message sending failed")
                return False


# ============================================================
# BOT SYSTEM
# ============================================================
class BotSystem:
    def __init__(self):
        self.bot_app: Optional[Application] = None
        self.manager = UserBotManager(self)

    # ---------- helpers ----------
    def is_admin(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id in ADMIN_IDS)

    async def deny(self, update: Update):
        if update.callback_query:
            await update.callback_query.answer("Нет доступа", show_alert=True)
        elif update.message:
            await update.message.reply_text("⛔ Доступ запрещён")

    async def edit_or_reply(self, update: Update, text: str, markup=None):
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                await update.callback_query.message.reply_text(
                    text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                )
        else:
            await update.message.reply_text(
                text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN,
            )

    # ---------- notifications ----------
    async def send_to_admins(self, text: str, markup=None):
        if not self.bot_app:
            return
        for admin_id in ADMIN_IDS:
            try:
                await self.bot_app.bot.send_message(
                    chat_id=admin_id,
                    text=text,
                    reply_markup=markup,
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                logger.exception("Cannot notify admin %s", admin_id)

    async def notify_new_contact(self, contact: dict):
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✍️ Написать",
                        callback_data=f"write:{contact['id']}",
                    ),
                    InlineKeyboardButton(
                        "⏭ Пропустить",
                        callback_data=f"skip:{contact['id']}",
                    ),
                ]
            ]
        )
        
        username = contact['username']
        
        await self.send_to_admins(
            "👤 *Новый username*\n\n"
            f"Username: [@{username}](https://t.me/{username})\n"
            f"Аккаунт ID: `{contact['account_id']}`\n"
            f"Источник: `{contact['source']}`\n\n"
            "Выберите действие:",
            keyboard,
        )

    async def notify_reply(self, account_id: int, contact: dict, text: str):
        account = db.get_account(account_id)
        phone = account["phone"] if account else "неизвестен"
        username = contact['username']

        await self.send_to_reply_group(
            "📩 *Новый ответ*\n\n"
            f"👤 Пользователь: [@{username}](https://t.me/{username})\n"
            f"🤖 Аккаунт: `{phone}`\n"
            f"🆔 Account ID: `{account_id}`\n\n"
            f"💬 Ответ:\n{text[:3500]}"
        )

    async def send_to_reply_group(self, text: str):
        group_id = db.get_setting("reply_group_id")
        if not group_id or not self.bot_app:
            return
        try:
            await self.bot_app.bot.send_message(
                chat_id=int(group_id),
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            logger.exception("Cannot send to reply group")

    async def notify_error(self, account_id: int, username: str, error: str):
        account = db.get_account(account_id)
        phone = account["phone"] if account else "неизвестен"

        await self.send_to_admins(
            "⚠️ *Ошибка отправки*\n\n"
            f"🤖 Аккаунт: `{phone}`\n"
            f"👤 Пользователь: [@{username}](https://t.me/{username})\n"
            f"🆔 Account ID: `{account_id}`\n\n"
            f"Ошибка: `{error[:1500]}`"
        )

    # ---------- menu ----------
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update):
            return await self.deny(update)
        await self.show_main_menu(update)

    async def show_main_menu(self, update: Update):
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("👤 Аккаунты", callback_data="menu:accounts"),
                    InlineKeyboardButton("👥 Пользователи", callback_data="menu:users"),
                ],
                [
                    InlineKeyboardButton("📡 Мониторинг", callback_data="menu:monitor"),
                    InlineKeyboardButton("💬 Ответы", callback_data="menu:reply_group"),
                ],
                [
                    InlineKeyboardButton("📝 Тексты", callback_data="menu:texts"),
                    InlineKeyboardButton("📊 Статистика", callback_data="menu:stats"),
                ],
            ]
        )
        await self.edit_or_reply(
            update,
            "*USERBOT MANAGER*\n\nВыберите раздел:",
            keyboard,
        )

    # ---------- accounts ----------
    async def menu_accounts(self, update: Update, context=None):
        accounts = db.get_accounts()
        lines = ["👤 *АККАУНТЫ*", ""]

        keyboard = [[
            InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account")
        ]]

        if not accounts:
            lines.append("Нет аккаунтов.")
        else:
            for a in accounts:
                online = "🟢" if a["id"] in self.manager.clients else "🔴"
                active = "✅" if a["is_active"] else "⛔"
                monitor = "🔍" if a["is_monitoring"] else "🚫"

                lines.extend(
                    [
                        f"{online} {active} `{a['phone']}`",
                        f"   Мониторинг: {monitor}",
                        "",
                    ]
                )
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{'⏸' if a['is_active'] else '▶️'} Аккаунт",
                            callback_data=f"account_active:{a['id']}",
                        ),
                        InlineKeyboardButton(
                            f"{'🔍' if a['is_monitoring'] else '🚫'} Мониторинг",
                            callback_data=f"account_monitor:{a['id']}",
                        ),
                    ]
                )
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            "🗑 Удалить",
                            callback_data=f"account_delete:{a['id']}",
                        )
                    ]
                )

        keyboard.append(
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")]
        )

        await self.edit_or_reply(update, "\n".join(lines), InlineKeyboardMarkup(keyboard))

    # ---------- add account ----------
    async def add_account_start(self, update: Update, context):
        if not self.is_admin(update):
            return await self.deny(update)
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "📱 *Добавление Telegram-аккаунта*\n\n"
            "Введи номер телефона в международном формате.\n"
            "Например: `+370...`\n\n"
            "Для отмены: /cancel",
            parse_mode=ParseMode.MARKDOWN,
        )
        return AUTH_PHONE

    async def add_account_phone(self, update: Update, context):
        phone = update.message.text.strip()
        if not re.fullmatch(r"\+\d{5,15}", phone):
            await update.message.reply_text("❌ Неверный формат номера.")
            return AUTH_PHONE

        if db.get_account_by_phone(phone):
            await update.message.reply_text("❌ Такой аккаунт уже есть.")
            return AUTH_PHONE

        session_name = "user_" + re.sub(r"\D", "", phone)
        client = TelegramClient(
            str(SESSIONS_DIR / session_name),
            API_ID,
            API_HASH,
        )

        try:
            await client.connect()
            await client.send_code_request(phone)

            context.user_data["auth_client"] = client
            context.user_data["phone"] = phone
            context.user_data["session_name"] = session_name

            await update.message.reply_text(
                "📨 Код отправлен в Telegram.\n\n"
                "Введи код из Telegram.\n"
                "Для отмены: /cancel"
            )
            return AUTH_CODE

        except Exception as e:
            await client.disconnect()
            await update.message.reply_text(f"❌ Не удалось отправить код:\n{e}")
            return ConversationHandler.END

    async def add_account_code(self, update: Update, context):
        code = update.message.text.strip().replace(" ", "")
        client = context.user_data.get("auth_client")
        phone = context.user_data.get("phone")
        session_name = context.user_data.get("session_name")

        if not client:
            await update.message.reply_text("❌ Сессия авторизации потеряна.")
            return ConversationHandler.END

        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            await update.message.reply_text(
                "🔐 На аккаунте включён 2FA.\nВведи пароль 2FA:"
            )
            return AUTH_PASSWORD
        except PhoneCodeInvalidError:
            await update.message.reply_text("❌ Неверный код. Попробуй ещё раз.")
            return AUTH_CODE
        except Exception as e:
            await client.disconnect()
            await update.message.reply_text(f"❌ Ошибка авторизации:\n{e}")
            return ConversationHandler.END

        return await self.finish_account_auth(update, context)

    async def add_account_password(self, update: Update, context):
        password = update.message.text
        client = context.user_data.get("auth_client")

        if not client:
            await update.message.reply_text("❌ Сессия авторизации потеряна.")
            return ConversationHandler.END

        try:
            await client.sign_in(password=password)
        except Exception as e:
            await update.message.reply_text(f"❌ Неверный 2FA пароль:\n{e}")
            return AUTH_PASSWORD

        return await self.finish_account_auth(update, context)

    async def finish_account_auth(self, update: Update, context):
        client = context.user_data["auth_client"]
        phone = context.user_data["phone"]
        session_name = context.user_data["session_name"]

        try:
            me = await client.get_me()
            if not me:
                raise RuntimeError("Не удалось получить профиль аккаунта.")

            await client.disconnect()

            account_id = db.add_account(phone, session_name)
            account = db.get_account(account_id)

            ok = await self.manager.start_account(account)
            if not ok:
                db.set_account_active(account_id, False)
                await update.message.reply_text(
                    "⚠️ Авторизация успешна, но аккаунт не удалось запустить.\n"
                    "Проверь API_ID/API_HASH и session."
                )
            else:
                await update.message.reply_text(
                    f"✅ Аккаунт добавлен.\n\n"
                    f"Телефон: `{phone}`\n"
                    f"Username: `@{getattr(me, 'username', '') or 'нет'}`",
                    parse_mode=ParseMode.MARKDOWN,
                )

        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            await update.message.reply_text(f"❌ Ошибка сохранения аккаунта:\n{e}")
        finally:
            context.user_data.clear()

        return ConversationHandler.END

    # ---------- groups ----------
    async def menu_monitor(self, update: Update, context=None):
        group_id = db.get_setting("monitor_group_id")
        text = (
            "📡 *МОНИТОРИНГ*\n\n"
            f"Группа: `{group_id}`" if group_id else "📡 *МОНИТОРИНГ*\n\nГруппа не установлена."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Установить группу", callback_data="set_monitor_group")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")],
            ]
        )
        await self.edit_or_reply(update, text, keyboard)

    async def set_monitor_group_start(self, update: Update, context):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "📡 Введи ID группы.\n\n"
            "Обычно это что-то вроде `-1001234567890`.\n"
            "Укажи ID группы, где аккаунты находятся и где появляются @username.\n\n"
            "/cancel — отмена",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_GROUP

    async def set_monitor_group_confirm(self, update: Update, context):
        group_id = update.message.text.strip()
        if not re.fullmatch(r"-?\d+", group_id):
            await update.message.reply_text("❌ ID должен быть числом.")
            return ADD_GROUP

        db.set_setting("monitor_group_id", group_id)
        await update.message.reply_text("✅ Группа мониторинга сохранена.")
        return ConversationHandler.END

    async def menu_reply_group(self, update: Update, context=None):
        group_id = db.get_setting("reply_group_id")
        text = (
            "💬 *ГРУППА ОТВЕТОВ*\n\n"
            f"Группа: `{group_id}`" if group_id else "💬 *ГРУППА ОТВЕТОВ*\n\nГруппа не установлена."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Установить группу", callback_data="set_reply_group")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")],
            ]
        )
        await self.edit_or_reply(update, text, keyboard)

    async def set_reply_group_start(self, update: Update, context):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "💬 Введи ID группы, куда бот будет присылать ответы.\n\n"
            "Бот должен находиться в этой группе.\n\n"
            "/cancel — отмена",
        )
        return ADD_REPLY_GROUP

    async def set_reply_group_confirm(self, update: Update, context):
        group_id = update.message.text.strip()
        if not re.fullmatch(r"-?\d+", group_id):
            await update.message.reply_text("❌ ID должен быть числом.")
            return ADD_REPLY_GROUP

        try:
            await self.bot_app.bot.send_message(
                chat_id=int(group_id),
                text="✅ Тест: группа ответов подключена.",
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Бот не смог отправить тестовое сообщение:\n{e}"
            )
            return ADD_REPLY_GROUP

        db.set_setting("reply_group_id", group_id)
        await update.message.reply_text("✅ Группа ответов сохранена.")
        return ConversationHandler.END

    # ---------- texts ----------
    async def menu_texts(self, update: Update, context=None):
        t1 = db.get_text(1)
        t2 = db.get_text(2)

        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📝 Изменить 1-й текст", callback_data="edit_text:1")],
                [InlineKeyboardButton("📝 Изменить 2-й текст", callback_data="edit_text:2")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")],
            ]
        )

        await self.edit_or_reply(
            update,
            "📝 *ТЕКСТЫ*\n\n"
            f"*1-й текст:*\n{t1}\n\n"
            f"*2-й текст:*\n{t2}",
            keyboard,
        )

    async def edit_text_start(self, update: Update, context):
        step = int(update.callback_query.data.split(":")[1])
        context.user_data["edit_step"] = step

        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"📝 Введи новый текст для шага {step}.\n\n/cancel — отмена"
        )
        return EDIT_TEXT_1 if step == 1 else EDIT_TEXT_2

    async def edit_text_confirm(self, update: Update, context):
        step = context.user_data.get("edit_step")
        if step not in (1, 2):
            return ConversationHandler.END

        text = update.message.text.strip()
        if not text:
            await update.message.reply_text("❌ Текст не может быть пустым.")
            return EDIT_TEXT_1 if step == 1 else EDIT_TEXT_2

        db.set_text(step, text)
        context.user_data.clear()
        await update.message.reply_text(f"✅ Текст {step} сохранён.")
        return ConversationHandler.END

    # ---------- manual users ----------
    async def menu_users(self, update: Update, context=None):
        pending = db.get_pending(20)
        keyboard = [
            [InlineKeyboardButton("➕ Добавить username", callback_data="manual_add")]
        ]

        if pending:
            for c in pending:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"✍️ @{c['username']} / {c['phone']}",
                            callback_data=f"write:{c['id']}",
                        )
                    ]
                )

        keyboard.append(
            [InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")]
        )

        text = f"👥 *ПОЛЬЗОВАТЕЛИ*\n\nОжидают решения: `{len(pending)}`"
        await self.edit_or_reply(update, text, InlineKeyboardMarkup(keyboard))

    async def manual_add_start(self, update: Update, context):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "👤 Введи username.\n\n"
            "Например: `@example`\n\n"
            "/cancel — отмена"
        )
        return MANUAL_USERNAME

    async def manual_add_confirm(self, update: Update, context):
        username = update.message.text.strip().lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
            await update.message.reply_text("❌ Некорректный username.")
            return MANUAL_USERNAME

        accounts = db.get_accounts(include_inactive=False)
        if not accounts:
            await update.message.reply_text("❌ Нет активных аккаунтов.")
            return ConversationHandler.END

        context.user_data["manual_username"] = username.lower()

        keyboard = [
            [
                InlineKeyboardButton(
                    f"{a['phone']}",
                    callback_data=f"manual_account:{a['id']}",
                )
            ]
            for a in accounts
        ]
        keyboard.append(
            [InlineKeyboardButton("❌ Отмена", callback_data="menu:users")]
        )

        await update.message.reply_text(
            f"👤 `@{username}`\n\nВыбери аккаунт:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    # ---------- callbacks ----------
    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update):
            return await self.deny(update)

        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "menu:main":
            return await self.show_main_menu(update)

        if data == "menu:accounts":
            return await self.menu_accounts(update, context)

        if data == "menu:users":
            return await self.menu_users(update, context)

        if data == "menu:monitor":
            return await self.menu_monitor(update, context)

        if data == "menu:reply_group":
            return await self.menu_reply_group(update, context)

        if data == "menu:texts":
            return await self.menu_texts(update, context)

        if data == "menu:stats":
            return await self.menu_stats(update, context)

        if data == "add_account":
            return await self.add_account_start(update, context)

        if data == "set_monitor_group":
            return await self.set_monitor_group_start(update, context)

        if data == "set_reply_group":
            return await self.set_reply_group_start(update, context)

        if data == "manual_add":
            return await self.manual_add_start(update, context)

        if data.startswith("edit_text:"):
            return await self.edit_text_start(update, context)

        if data.startswith("write:"):
            contact_id = int(data.split(":")[1])
            contact = db.get_contact(contact_id)

            if not contact:
                return await query.edit_message_text("❌ Пользователь не найден.")

            if contact["status"] != "pending":
                return await query.edit_message_text(
                    f"ℹ️ Пользователь уже обработан.\nСтатус: {contact['status']}"
                )

            ok = await self.manager.send_first_message(contact_id)

            if ok:
                return await query.edit_message_text(
                    f"✅ Первое сообщение отправлено.\n\n"
                    f"@{contact['username']}\n"
                    f"Аккаунт: {contact['account_id']}\n\n"
                    f"Теперь система ждёт ответ."
                )

            return await query.edit_message_text(
                f"❌ Отправить сообщение не получилось.\n\n"
                f"@{contact['username']}\n"
                f"Подробная ошибка отправлена администратору."
            )

        if data.startswith("skip:"):
            contact_id = int(data.split(":")[1])
            contact = db.get_contact(contact_id)
            if contact:
                db.set_contact_status(contact_id, "skipped")
            return await query.edit_message_text("⏭ Пользователь пропущен.")

        if data.startswith("manual_account:"):
            account_id = int(data.split(":")[1])
            username = context.user_data.get("manual_username")
            if not username:
                return await query.edit_message_text("❌ Данные потеряны. Добавь username ещё раз.")

            contact, created = db.add_contact(
                account_id,
                username,
                source="manual",
            )
            context.user_data.pop("manual_username", None)

            if not created:
                return await query.edit_message_text(
                    f"ℹ️ `@{username}` уже существует для этого аккаунта.\n"
                    f"Статус: `{contact['status']}`",
                    parse_mode=ParseMode.MARKDOWN,
                )

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✍️ Написать",
                            callback_data=f"write:{contact['id']}",
                        ),
                        InlineKeyboardButton(
                            "⏭ Пропустить",
                            callback_data=f"skip:{contact['id']}",
                        ),
                    ]
                ]
            )
            return await query.edit_message_text(
                f"👤 `@{username}` добавлен.\n\nВыбери действие:",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
            )

        if data.startswith("account_monitor:"):
            account_id = int(data.split(":")[1])
            account = db.get_account(account_id)
            if account:
                db.set_monitoring(
                    account_id,
                    not bool(account["is_monitoring"]),
                )
            return await self.menu_accounts(update, context)

        if data.startswith("account_active:"):
            account_id = int(data.split(":")[1])
            account = db.get_account(account_id)
            if not account:
                return await query.edit_message_text("❌ Аккаунт не найден.")

            new_active = not bool(account["is_active"])
            db.set_account_active(account_id, new_active)

            if new_active:
                await self.manager.start_account(
                    db.get_account(account_id)
                )
            else:
                await self.manager.stop_account(account_id)

            return await self.menu_accounts(update, context)

        if data.startswith("account_delete:"):
            account_id = int(data.split(":")[1])
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 Да, удалить",
                            callback_data=f"account_delete_confirm:{account_id}",
                        ),
                        InlineKeyboardButton(
                            "❌ Отмена",
                            callback_data="menu:accounts",
                        ),
                    ]
                ]
            )
            return await query.edit_message_text(
                "⚠️ *Удалить аккаунт?*\n\n"
                "Связанные контакты будут удалены из базы.",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
            )

        if data.startswith("account_delete_confirm:"):
            account_id = int(data.split(":")[1])
            account = db.get_account(account_id)
            if account:
                await self.manager.stop_account(account_id)
                session_file = SESSIONS_DIR / account["session_name"]
                db.delete_account(account_id)

                for p in (
                    session_file,
                    Path(str(session_file) + "-journal"),
                ):
                    try:
                        if p.exists():
                            p.unlink()
                    except Exception:
                        logger.exception("Cannot delete session file %s", p)

            return await self.menu_accounts(update, context)

    async def menu_stats(self, update: Update, context=None):
        stats = db.get_stats()
        pending = db.pending_count()

        lines = [
            "📊 *СТАТИСТИКА*",
            "",
            f"Ожидают: `{pending}`",
            f"Онлайн: `{len(self.manager.clients)}`",
            "",
        ]

        for s in stats:
            lines.append(
                f"`{s['phone']}` — "
                f"1-й: {s['sent_first']} | "
                f"2-й: {s['sent_second']} | "
                f"ответы: {s['replies']} | "
                f"ошибки: {s['errors']}"
            )

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Назад", callback_data="menu:main")]]
        )
        await self.edit_or_reply(update, "\n".join(lines), keyboard)

    # ---------- cancel ----------
    async def cancel(self, update: Update, context):
        client = context.user_data.pop("auth_client", None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        context.user_data.clear()
        await update.message.reply_text("✅ Операция отменена.")
        return ConversationHandler.END

    # ---------- startup ----------
    def build_application(self):
        app = Application.builder().token(BOT_TOKEN).build()
        self.bot_app = app

        conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(
                    self.add_account_start,
                    pattern=r"^add_account$",
                ),
                CallbackQueryHandler(
                    self.set_monitor_group_start,
                    pattern=r"^set_monitor_group$",
                ),
                CallbackQueryHandler(
                    self.set_reply_group_start,
                    pattern=r"^set_reply_group$",
                ),
                CallbackQueryHandler(
                    self.edit_text_start,
                    pattern=r"^edit_text:\d+$",
                ),
                CallbackQueryHandler(
                    self.manual_add_start,
                    pattern=r"^manual_add$",
                ),
            ],
            states={
                AUTH_PHONE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_phone)
                ],
                AUTH_CODE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_code)
                ],
                AUTH_PASSWORD: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_password)
                ],
                ADD_GROUP: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_monitor_group_confirm)
                ],
                ADD_REPLY_GROUP: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_reply_group_confirm)
                ],
                EDIT_TEXT_1: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_text_confirm)
                ],
                EDIT_TEXT_2: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_text_confirm)
                ],
                MANUAL_USERNAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_add_confirm)
                ],
            },
            fallbacks=[
                CommandHandler("cancel", self.cancel),
            ],
            allow_reentry=True,
        )

        app.add_handler(conv)
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("menu", self.start))
        app.add_handler(CommandHandler("cancel", self.cancel))
        app.add_handler(CallbackQueryHandler(self.callback))

        return app

    async def run(self):
        if BOT_TOKEN == "8948221161:AAHLPfFUmK1QyRGVcaM8UVchByrxAmCkA8s":
            # Токен уже вставлен, пропускаем
            pass

        app = self.build_application()

        # Удаляем вебхук перед запуском
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ Webhook удалён")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось удалить webhook: {e}")

        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info("✅ Бот запущен на Render!")

        try:
            await self.manager.start_all()
            logger.info("✅ Юзерботы запущены")
            
            # Держим бота живым
            while True:
                await asyncio.sleep(3600)
                
        except KeyboardInterrupt:
            logger.info("⏹ Получен сигнал остановки")
        finally:
            await self.manager.stop_all()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("✅ Остановка завершена")


# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    system = BotSystem()
    await system.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹ Бот остановлен")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
