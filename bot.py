#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ZOV UserBot System v3.0 - ПРАВИЛЬНАЯ ВЕРСИЯ
Юзерботы сами кидают кнопки в ЛС
"""

import os
import re
import sqlite3
import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional

# ==================== НАСТРОЙКА ====================

BOT_TOKEN = "8849260350:AAH3YDz5Qz6KfkfTCSPO2mzRu6nGUkrcGtY"
API_ID = 20734425
API_HASH = "f72fa8d1d63a8f984e47a115c76df123"
ADMIN_IDS = [8986358602]  # Ваш ID

SESSIONS_DIR = "sessions"
DATABASE_FILE = "data.db"

os.makedirs(SESSIONS_DIR, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== ИМПОРТЫ ====================

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    PhoneNumberInvalidError,
    PhoneCodeExpiredError,
    PeerFloodError
)

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler
)

# Состояния
AUTH_PHONE, AUTH_CODE, AUTH_PASSWORD = range(3)
ADD_GROUP = 10
EDIT_TEXTS = 20


# ==================== БАЗА ДАННЫХ ====================

class Database:
    def __init__(self):
        self.db_file = DATABASE_FILE
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()

            # Аккаунты
            c.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE,
                    session_name TEXT UNIQUE,
                    is_active INTEGER DEFAULT 1,
                    is_monitoring INTEGER DEFAULT 1,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Группа для мониторинга (одна)
            c.execute('''
                CREATE TABLE IF NOT EXISTS monitor_group (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT UNIQUE,
                    group_name TEXT
                )
            ''')

            # Группа для ответов
            c.execute('''
                CREATE TABLE IF NOT EXISTS reply_group (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT UNIQUE,
                    group_name TEXT
                )
            ''')

            # Тексты (1-й и 2-й)
            c.execute('''
                CREATE TABLE IF NOT EXISTS texts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    step INTEGER UNIQUE,
                    text TEXT
                )
            ''')
            # Добавляем дефолтные тексты
            c.execute("INSERT OR IGNORE INTO texts (step, text) VALUES (1, 'Привет! Как дела?')")
            c.execute("INSERT OR IGNORE INTO texts (step, text) VALUES (2, 'Отлично! Чем могу помочь?')")

            # Найденные пользователи
            c.execute('''
                CREATE TABLE IF NOT EXISTS found_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    user_id TEXT,
                    account_id INTEGER,
                    found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    processed_by INTEGER,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                )
            ''')

            # Очередь сообщений
            c.execute('''
                CREATE TABLE IF NOT EXISTS message_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    username TEXT,
                    step INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'pending',
                    error_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                )
            ''')

            # Ответы пользователей
            c.execute('''
                CREATE TABLE IF NOT EXISTS replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    username TEXT,
                    user_id TEXT,
                    reply_text TEXT,
                    replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Статистика
            c.execute('''
                CREATE TABLE IF NOT EXISTS stats (
                    account_id INTEGER PRIMARY KEY,
                    sent INTEGER DEFAULT 0,
                    replies INTEGER DEFAULT 0,
                    errors INTEGER DEFAULT 0,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                )
            ''')

            conn.commit()

    # ============ АККАУНТЫ ============

    def add_account(self, phone: str, session_name: str) -> int:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO accounts (phone, session_name) VALUES (?, ?)",
                (phone, session_name)
            )
            account_id = c.lastrowid
            c.execute("INSERT OR IGNORE INTO stats (account_id) VALUES (?)", (account_id,))
            conn.commit()
            return account_id

    def get_accounts(self, active_only: bool = True) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            query = """
                SELECT a.*, s.sent, s.replies, s.errors 
                FROM accounts a
                LEFT JOIN stats s ON a.id = s.account_id
            """
            if active_only:
                query += " WHERE a.is_active = 1"
            c.execute(query)
            return [dict(row) for row in c.fetchall()]

    def get_account(self, account_id: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            row = c.fetchone()
            return dict(row) if row else None

    def toggle_account(self, account_id: int, active: bool):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE accounts SET is_active = ? WHERE id = ?",
                (1 if active else 0, account_id)
            )
            conn.commit()

    def toggle_monitoring(self, account_id: int, active: bool):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE accounts SET is_monitoring = ? WHERE id = ?",
                (1 if active else 0, account_id)
            )
            conn.commit()

    def delete_account(self, account_id: int):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
            c.execute("DELETE FROM stats WHERE account_id = ?", (account_id,))
            conn.commit()

    # ============ ГРУППЫ ============

    def set_monitor_group(self, group_id: str, group_name: str):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM monitor_group")
            c.execute(
                "INSERT INTO monitor_group (group_id, group_name) VALUES (?, ?)",
                (group_id, group_name)
            )
            conn.commit()

    def get_monitor_group(self) -> Optional[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM monitor_group LIMIT 1")
            row = c.fetchone()
            return dict(row) if row else None

    def set_reply_group(self, group_id: str, group_name: str):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM reply_group")
            c.execute(
                "INSERT INTO reply_group (group_id, group_name) VALUES (?, ?)",
                (group_id, group_name)
            )
            conn.commit()

    def get_reply_group(self) -> Optional[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM reply_group LIMIT 1")
            row = c.fetchone()
            return dict(row) if row else None

    # ============ ТЕКСТЫ ============

    def get_text(self, step: int) -> Optional[str]:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute("SELECT text FROM texts WHERE step = ?", (step,))
            row = c.fetchone()
            return row[0] if row else None

    def set_text(self, step: int, text: str):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO texts (step, text) VALUES (?, ?)",
                (step, text)
            )
            conn.commit()

    # ============ НАЙДЕННЫЕ ПОЛЬЗОВАТЕЛИ ============

    def save_found_user(self, username: str, account_id: int, user_id: str = None):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            # Проверяем, не обработан ли уже
            c.execute(
                "SELECT * FROM found_users WHERE username = ? AND status = 'pending'",
                (username,)
            )
            if c.fetchone():
                return
            c.execute(
                "INSERT INTO found_users (username, user_id, account_id) VALUES (?, ?, ?)",
                (username, user_id, account_id)
            )
            conn.commit()

    def get_pending_users(self) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                "SELECT * FROM found_users WHERE status = 'pending' ORDER BY found_at"
            )
            return [dict(row) for row in c.fetchall()]

    def get_pending_users_by_account(self, account_id: int) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                "SELECT * FROM found_users WHERE status = 'pending' AND account_id = ? ORDER BY found_at",
                (account_id,)
            )
            return [dict(row) for row in c.fetchall()]

    def update_found_status(self, user_id: int, status: str, processed_by: int = None):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE found_users SET status = ?, processed_by = ? WHERE id = ?",
                (status, processed_by, user_id)
            )
            conn.commit()

    def skip_found_user(self, user_id: int):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE found_users SET status = 'skipped' WHERE id = ?",
                (user_id,)
            )
            conn.commit()

    # ============ ОЧЕРЕДЬ ============

    def add_to_queue(self, account_id: int, username: str, step: int = 1):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO message_queue (account_id, username, step) VALUES (?, ?, ?)",
                (account_id, username, step)
            )
            conn.commit()
            return c.lastrowid

    def get_queue(self) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                "SELECT * FROM message_queue WHERE status = 'pending' ORDER BY created_at"
            )
            return [dict(row) for row in c.fetchall()]

    def get_queue_by_username(self, username: str, account_id: int) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                "SELECT * FROM message_queue WHERE username = ? AND account_id = ? AND status = 'pending'",
                (username, account_id)
            )
            return [dict(row) for row in c.fetchall()]

    def update_queue_status(self, queue_id: int, status: str, error: str = None):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE message_queue SET status = ?, error_text = ? WHERE id = ?",
                (status, error, queue_id)
            )
            conn.commit()

    def update_queue_step(self, queue_id: int, step: int):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "UPDATE message_queue SET step = ? WHERE id = ?",
                (step, queue_id)
            )
            conn.commit()

    def get_queue_stats(self) -> Dict:
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM message_queue WHERE status = 'pending'")
            pending = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM message_queue WHERE status = 'sent'")
            sent = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM message_queue WHERE status = 'error' OR status = 'blocked'")
            errors = c.fetchone()[0]
            return {'pending': pending, 'sent': sent, 'errors': errors}

    # ============ ОТВЕТЫ ============

    def save_reply(self, account_id: int, username: str, user_id: str, text: str):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO replies (account_id, username, user_id, reply_text) VALUES (?, ?, ?, ?)",
                (account_id, username, user_id, text)
            )
            conn.commit()

    def get_replies(self) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                "SELECT * FROM replies ORDER BY replied_at DESC LIMIT 20"
            )
            return [dict(row) for row in c.fetchall()]

    # ============ СТАТИСТИКА ============

    def inc_stats(self, account_id: int, field: str):
        with sqlite3.connect(self.db_file) as conn:
            c = conn.cursor()
            c.execute(
                f"UPDATE stats SET {field} = {field} + 1 WHERE account_id = ?",
                (account_id,)
            )
            conn.commit()


# ==================== ЮЗЕРБОТ МЕНЕДЖЕР ====================

class UserBotManager:
    def __init__(self, db: Database, bot_app=None):
        self.db = db
        self.bot_app = bot_app
        self.clients = {}
        self.running = True

    def set_bot_app(self, bot_app):
        self.bot_app = bot_app

    async def start_account(self, account_id: int, phone: str, session_name: str):
        if account_id in self.clients:
            return

        session_path = f"{SESSIONS_DIR}/{session_name}"
        client = TelegramClient(session_path, API_ID, API_HASH)

        try:
            await client.start(phone=phone)

            self.clients[account_id] = {
                'client': client,
                'phone': phone
            }

            # Обработчик входящих сообщений
            @client.on(events.NewMessage(incoming=True))
            async def handle_incoming(event):
                await self._handle_incoming(account_id, event)

            # Обработчик новых сообщений в группах
            @client.on(events.NewMessage)
            async def handle_group_message(event):
                await self._handle_group_message(account_id, event)

            logger.info(f"✅ Юзербот {phone} запущен")
            return client

        except Exception as e:
            logger.error(f"Ошибка запуска {phone}: {e}")
            return None

    async def stop_account(self, account_id: int):
        if account_id in self.clients:
            try:
                await self.clients[account_id]['client'].disconnect()
                del self.clients[account_id]
                logger.info(f"✅ Юзербот {account_id} остановлен")
            except Exception as e:
                logger.error(f"Ошибка остановки {account_id}: {e}")

    async def _handle_group_message(self, account_id: int, event):
        """Обработка сообщений в группах - поиск юзернеймов"""
        try:
            # Проверяем, что это сообщение в группе
            if not event.is_group:
                return

            # Проверяем, включён ли мониторинг
            account = self.db.get_account(account_id)
            if not account or not account.get('is_monitoring', 1):
                return

            # Проверяем, что это нужная группа
            monitor_group = self.db.get_monitor_group()
            if not monitor_group:
                return

            # Получаем ID группы из события
            chat_id = str(event.chat_id)
            if chat_id != monitor_group['group_id']:
                return

            msg = event.message
            if not msg.text:
                return

            # Ищем юзернеймы
            usernames = re.findall(r'@(\w+)', msg.text)

            for username in usernames:
                # Проверяем, не обработан ли уже
                pending = self.db.get_pending_users_by_account(account_id)
                if any(u['username'] == username for u in pending):
                    continue

                # Сохраняем найденного
                self.db.save_found_user(username, account_id)

                # Отправляем кнопку в ЛС пользователю бота (админу)
                await self._send_buttons_to_admin(username, account_id, account['phone'])

        except Exception as e:
            logger.error(f"Ошибка обработки сообщения группы: {e}")

    async def _send_buttons_to_admin(self, username: str, account_id: int, phone: str):
        """Отправка кнопок админу от имени юзербота"""
        if not self.bot_app:
            return

        # Находим запись в БД
        pending = self.db.get_pending_users_by_account(account_id)
        found_user = None
        for f in pending:
            if f['username'] == username:
                found_user = f
                break

        if not found_user:
            return

        found_id = found_user['id']

        keyboard = [
            [
                InlineKeyboardButton("✍️ Написать", callback_data=f"write_{found_id}_{account_id}"),
                InlineKeyboardButton("⏭️ Пропустить", callback_data=f"skip_{found_id}_{account_id}")
            ]
        ]

        for admin_id in ADMIN_IDS:
            try:
                await self.bot_app.bot.send_message(
                    admin_id,
                    f"👤 **Найден новый пользователь!**\n"
                    f"📌 @{username}\n"
                    f"🤖 Аккаунт: {phone}\n\n"
                    f"Выберите действие:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
                logger.info(f"✅ Кнопки отправлены админу для @{username}")
            except Exception as e:
                logger.error(f"Ошибка отправки кнопок админу: {e}")

    async def _handle_incoming(self, account_id: int, event):
        """Обработка личных сообщений - ответы пользователей"""
        try:
            msg = event.message
            sender = await event.get_sender()

            if not sender or sender.bot or not event.is_private:
                return

            username = sender.username or sender.first_name or str(sender.id)
            user_id = str(sender.id)
            text = msg.text or ""

            # Сохраняем ответ
            self.db.save_reply(account_id, username, user_id, text)
            self.db.inc_stats(account_id, 'replies')

            # Отправляем ответ в группу для ответов
            await self._send_reply_to_group(account_id, username, text)

            # Проверяем очередь для этого пользователя
            await self._process_user_reply(account_id, username)

        except Exception as e:
            logger.error(f"Ошибка обработки входящего: {e}")

    async def _send_reply_to_group(self, account_id: int, username: str, text: str):
        """Отправка ответа в группу для ответов"""
        if not self.bot_app:
            return

        reply_group = self.db.get_reply_group()
        if not reply_group:
            return

        account = self.db.get_account(account_id)

        try:
            await self.bot_app.bot.send_message(
                reply_group['group_id'],
                f"📩 **Ответ от пользователя**\n"
                f"👤 @{username}\n"
                f"🤖 Аккаунт: {account['phone'] if account else 'Неизвестно'}\n"
                f"📝 Текст: {text[:500]}"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки в группу: {e}")

    async def _process_user_reply(self, account_id: int, username: str):
        """Обработка ответа - отправка 2-го текста"""
        queue = self.db.get_queue_by_username(username, account_id)

        for q in queue:
            if q['step'] == 1:
                # Получаем 2-й текст
                text2 = self.db.get_text(2)
                if text2:
                    # Отправляем 2-й текст
                    await self._send_message(account_id, username, q['id'], text2, 2)
                else:
                    self.db.update_queue_status(q['id'], 'delivered')

    async def _send_message(self, account_id: int, username: str, queue_id: int, text: str, step: int):
        """Отправка сообщения пользователю"""
        try:
            client = self.clients.get(account_id, {}).get('client')
            if not client:
                return False

            entity = await client.get_entity(username)
            await client.send_message(entity, text)

            self.db.update_queue_status(queue_id, 'sent')
            self.db.update_queue_step(queue_id, step)
            self.db.inc_stats(account_id, 'sent')

            logger.info(f"✅ Отправлено @{username} с аккаунта {account_id}")
            return True

        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
            return await self._send_message(account_id, username, queue_id, text, step)

        except PeerFloodError:
            self.db.update_queue_status(queue_id, 'blocked', 'SPAM блок')
            self.db.inc_stats(account_id, 'errors')
            for admin_id in ADMIN_IDS:
                try:
                    await self.bot_app.bot.send_message(
                        admin_id,
                        f"⚠️ **SPAM блок!**\nАккаунт: {self.clients.get(account_id, {}).get('phone', 'Неизвестно')}\nПользователь: @{username}"
                    )
                except:
                    pass
            return False

        except Exception as e:
            self.db.update_queue_status(queue_id, 'error', str(e))
            self.db.inc_stats(account_id, 'errors')
            return False

    async def send_first_message(self, found_id: int, account_id: int):
        """Отправка 1-го текста найденному пользователю"""
        found = self.db.get_pending_users()
        user = None
        for f in found:
            if f['id'] == found_id:
                user = f
                break

        if not user:
            return False

        text1 = self.db.get_text(1)
        if not text1:
            return False

        # Добавляем в очередь
        queue_id = self.db.add_to_queue(account_id, user['username'], 1)

        # Отправляем
        result = await self._send_message(account_id, user['username'], queue_id, text1, 1)

        if result:
            # Отмечаем как обработанного
            self.db.update_found_status(found_id, 'processed')

        return result

    async def run_all(self):
        """Запуск всех аккаунтов"""
        accounts = self.db.get_accounts(active_only=True)
        for account in accounts:
            await self.start_account(
                account['id'],
                account['phone'],
                account['session_name']
            )

    async def stop_all(self):
        self.running = False
        for account_id in list(self.clients.keys()):
            await self.stop_account(account_id)


# ==================== ОСНОВНОЙ БОТ ====================

class BotSystem:
    def __init__(self):
        self.db = Database()
        self.bot_manager = UserBotManager(self.db)
        self.bot_app = None

    def is_admin(self, user_id: int) -> bool:
        return user_id in ADMIN_IDS

    # ============ МЕНЮ ============

    async def main_menu(self, update):
        keyboard = [
            [InlineKeyboardButton("👤 Аккаунты", callback_data="menu_accounts")],
            [InlineKeyboardButton("📁 Группа мониторинга", callback_data="menu_group")],
            [InlineKeyboardButton("📩 Группа для ответов", callback_data="menu_reply_group")],
            [InlineKeyboardButton("📝 Тексты", callback_data="menu_texts")],
            [InlineKeyboardButton("🔍 Найденные", callback_data="menu_found")],
            [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
            [InlineKeyboardButton("📨 Очередь", callback_data="menu_queue")],
        ]

        text = "🇷🇺 **ZOV UserBot System** 🇷🇺\n\n📋 Главное меню"

        if hasattr(update, 'callback_query'):
            await update.callback_query.edit_message_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
            )
        else:
            await update.message.reply_text(
                text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
            )

    # ============ АККАУНТЫ ============

    async def menu_accounts(self, update):
        accounts = self.db.get_accounts(active_only=False)
        query = update.callback_query

        keyboard = [
            [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account")]
        ]

        text = "👤 **АККАУНТЫ**\n━━━━━━━━━━━━━━━━━━\n"

        if not accounts:
            text += "📭 Нет аккаунтов"
        else:
            for acc in accounts:
                status = "✅" if acc['is_active'] else "⛔"
                monitor = "🔍" if acc.get('is_monitoring', 1) else "⛔"
                online = "🟢" if acc['id'] in self.bot_manager.clients else "🔴"

                text += f"\n{status} {acc['phone']} {online}\n"
                text += f"  ID: `{acc['id']}` | Мониторинг: {monitor}\n"
                text += f"  Отправлено: {acc.get('sent', 0)} | Ответов: {acc.get('replies', 0)}\n"

                keyboard.append([
                    InlineKeyboardButton(
                        f"{'⏸️' if acc['is_active'] else '▶️'}",
                        callback_data=f"toggle_acc_{acc['id']}"
                    ),
                    InlineKeyboardButton(
                        f"{'🔍' if acc.get('is_monitoring', 1) else '⛔'} Мониторинг",
                        callback_data=f"toggle_mon_{acc['id']}"
                    ),
                    InlineKeyboardButton(
                        f"🗑️",
                        callback_data=f"del_acc_{acc['id']}"
                    )
                ])
                keyboard.append([])

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])

        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )

    # ============ ДОБАВЛЕНИЕ АККАУНТА ============

    async def add_account_start(self, update, context):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "📱 **Добавление аккаунта**\n\n"
            "Введите номер телефона:\n`+71234567890`\n\n"
            "Для отмены: /cancel",
            parse_mode='Markdown'
        )
        return AUTH_PHONE

    async def add_account_phone(self, update, context):
        phone = update.message.text.strip()
        if not phone.startswith('+') or not phone[1:].isdigit():
            await update.message.reply_text("❌ Неверный формат. Используйте +71234567890")
            return AUTH_PHONE

        if self.db.get_account_by_phone(phone):
            await update.message.reply_text(f"❌ Аккаунт {phone} уже существует")
            return AUTH_PHONE

        context.user_data['phone'] = phone

        try:
            session_name = f"user_{phone.replace('+', '')}"
            client = TelegramClient(f"{SESSIONS_DIR}/{session_name}", API_ID, API_HASH)
            await client.connect()
            await client.send_code_request(phone)
            context.user_data['client'] = client
            context.user_data['session_name'] = session_name

            await update.message.reply_text(
                f"📱 Код отправлен на {phone}\nВведите код:"
            )
            return AUTH_CODE

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            return ConversationHandler.END

    async def add_account_code(self, update, context):
        code = update.message.text.strip()
        client = context.user_data.get('client')
        phone = context.user_data.get('phone')
        session_name = context.user_data.get('session_name')

        if not client:
            await update.message.reply_text("❌ Ошибка. Начните заново")
            return ConversationHandler.END

        try:
            await client.sign_in(phone, code)

            if await client.is_user_authorized():
                account_id = self.db.add_account(phone, session_name)
                await self.bot_manager.start_account(account_id, phone, session_name)

                await update.message.reply_text(f"✅ Аккаунт {phone} добавлен и запущен!")
                await client.disconnect()
                context.user_data.clear()
                return ConversationHandler.END
            else:
                await update.message.reply_text("❌ Ошибка авторизации")
                return ConversationHandler.END

        except SessionPasswordNeededError:
            await update.message.reply_text("🔐 Требуется 2FA пароль\nВведите пароль:")
            return AUTH_PASSWORD

        except PhoneCodeInvalidError:
            await update.message.reply_text("❌ Неверный код. Попробуйте снова:")
            return AUTH_CODE

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            return ConversationHandler.END

    async def add_account_password(self, update, context):
        password = update.message.text.strip()
        client = context.user_data.get('client')
        phone = context.user_data.get('phone')
        session_name = context.user_data.get('session_name')

        if not client:
            await update.message.reply_text("❌ Ошибка. Начните заново")
            return ConversationHandler.END

        try:
            await client.sign_in(password=password)

            if await client.is_user_authorized():
                account_id = self.db.add_account(phone, session_name)
                await self.bot_manager.start_account(account_id, phone, session_name)

                await update.message.reply_text(f"✅ Аккаунт {phone} добавлен и запущен!")
                await client.disconnect()
                context.user_data.clear()
                return ConversationHandler.END
            else:
                await update.message.reply_text("❌ Ошибка авторизации")
                return ConversationHandler.END

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            return AUTH_PASSWORD

    # ============ ГРУППА МОНИТОРИНГА ============

    async def menu_group(self, update):
        query = update.callback_query
        group = self.db.get_monitor_group()

        keyboard = [
            [InlineKeyboardButton("📝 Установить группу", callback_data="set_group")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu_group")]
        ]

        text = "📁 **ГРУППА ДЛЯ МОНИТОРИНГА**\n━━━━━━━━━━━━━━━━━━\n"

        if group:
            text += f"📌 {group['group_name']}\n"
            text += f"🆔 ID: `{group['group_id']}`"
        else:
            text += "❌ Группа не установлена"

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])

        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )

    async def set_group_start(self, update, context):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "📁 **Установка группы для мониторинга**\n\n"
            "Введите ID группы:\n`-100123456789`\n\n"
            "⚠️ Аккаунты должны быть в этой группе!\n\n"
            "Для отмены: /cancel",
            parse_mode='Markdown'
        )
        return ADD_GROUP

    async def set_group_confirm(self, update, context):
        group_id = update.message.text.strip()

        # Пробуем получить название группы
        try:
            # Отправляем тестовое сообщение
            await self.bot_app.bot.send_message(
                group_id,
                "✅ Группа установлена для мониторинга!"
            )

            self.db.set_monitor_group(group_id, f"Группа {group_id}")

            await update.message.reply_text(
                f"✅ Группа установлена для мониторинга!\nID: `{group_id}`",
                parse_mode='Markdown'
            )

        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка: бот не может отправить сообщение в группу.\n"
                f"Убедитесь, что бот добавлен в группу.\n\n"
                f"Ошибка: {str(e)[:100]}"
            )
            return ADD_GROUP

        context.user_data.clear()
        return ConversationHandler.END

    # ============ ГРУППА ДЛЯ ОТВЕТОВ ============

    async def menu_reply_group(self, update):
        query = update.callback_query
        group = self.db.get_reply_group()

        keyboard = [
            [InlineKeyboardButton("📝 Установить группу", callback_data="set_reply_group")],
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu_reply_group")]
        ]

        text = "📩 **ГРУППА ДЛЯ ОТВЕТОВ**\n━━━━━━━━━━━━━━━━━━\n"

        if group:
            text += f"📌 {group['group_name']}\n"
            text += f"🆔 ID: `{group['group_id']}`"
        else:
            text += "❌ Группа не установлена"

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])

        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )

    async def set_reply_group_start(self, update, context):
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "📩 **Установка группы для ответов**\n\n"
            "Введите ID группы:\n`-100123456789`\n\n"
            "⚠️ Бот должен быть админом в этой группе!\n\n"
            "Для отмены: /cancel",
            parse_mode='Markdown'
        )
        return ADD_GROUP

    async def set_reply_group_confirm(self, update, context):
        group_id = update.message.text.strip()

        try:
            await self.bot_app.bot.send_message(
                group_id,
                "✅ Группа установлена для получения ответов!"
            )

            self.db.set_reply_group(group_id, f"Группа {group_id}")

            await update.message.reply_text(
                f"✅ Группа установлена для ответов!\nID: `{group_id}`",
                parse_mode='Markdown'
            )

        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка: бот не может отправить сообщение.\n"
                f"Убедитесь, что бот добавлен в группу и является админом.\n\n"
                f"Ошибка: {str(e)[:100]}"
            )
            return ADD_GROUP

        context.user_data.clear()
        return ConversationHandler.END

    # ============ ТЕКСТЫ ============

    async def menu_texts(self, update):
        query = update.callback_query
        text1 = self.db.get_text(1) or "Не установлен"
        text2 = self.db.get_text(2) or "Не установлен"

        keyboard = [
            [InlineKeyboardButton("📝 1-й текст", callback_data="edit_text_1")],
            [InlineKeyboardButton("📝 2-й текст", callback_data="edit_text_2")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
        ]

        text = (
            f"📝 **ТЕКСТЫ**\n━━━━━━━━━━━━━━━━━━\n\n"
            f"📌 **1-й текст (первое сообщение):**\n{text1}\n\n"
            f"📌 **2-й текст (после ответа):**\n{text2}"
        )

        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )

    async def edit_text_start(self, update, context):
        query = update.callback_query
        await query.answer()

        step = 1 if "edit_text_1" in query.data else 2
        context.user_data['edit_step'] = step

        current = self.db.get_text(step) or "Не установлен"

        await query.edit_message_text(
            f"📝 **Редактирование {step}-го текста**\n\n"
            f"Текущий текст:\n{current}\n\n"
            f"Введите новый текст:\n\n"
            f"Для отмены: /cancel",
            parse_mode='Markdown'
        )
        return EDIT_TEXTS

    async def edit_text_confirm(self, update, context):
        text = update.message.text.strip()
        step = context.user_data.get('edit_step', 1)

        self.db.set_text(step, text)

        await update.message.reply_text(f"✅ {step}-й текст сохранён!")
        context.user_data.clear()
        return ConversationHandler.END

    # ============ НАЙДЕННЫЕ ============

    async def menu_found(self, update):
        query = update.callback_query
        found = self.db.get_pending_users()

        keyboard = [
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu_found")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
        ]

        text = "🔍 **НАЙДЕННЫЕ ПОЛЬЗОВАТЕЛИ**\n━━━━━━━━━━━━━━━━━━\n"

        if not found:
            text += "📭 Нет новых пользователей"
        else:
            for f in found[:20]:
                text += f"\n👤 @{f['username']}\n"
                text += f"  Аккаунт: {f['account_id']}\n"
                text += f"  Найден: {f['found_at'][:16]}\n"

        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )

    # ============ СТАТИСТИКА ============

    async def menu_stats(self, update):
        query = update.callback_query
        accounts = self.db.get_accounts(active_only=False)
        queue = self.db.get_queue_stats()
        found = len(self.db.get_pending_users())

        text = "📊 **СТАТИСТИКА**\n━━━━━━━━━━━━━━━━━━\n\n"
        text += f"👤 Аккаунтов: {len(accounts)}\n"
        text += f"🟢 Онлайн: {len(self.bot_manager.clients)}\n"
        text += f"🔍 В ожидании: {found}\n"
        text += f"📨 В очереди: {queue.get('pending', 0)}\n"
        text += f"✅ Отправлено: {queue.get('sent', 0)}\n"
        text += f"⚠️ Ошибок: {queue.get('errors', 0)}\n\n"

        text += "📊 **По аккаунтам:**\n"
        for acc in accounts:
            text += f"  {acc['phone']}: отправлено {acc.get('sent', 0)}, ответов {acc.get('replies', 0)}\n"

        keyboard = [
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu_stats")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
        ]

        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )

    # ============ ОЧЕРЕДЬ ============

    async def menu_queue(self, update):
        query = update.callback_query
        queue = self.db.get_queue()

        keyboard = [
            [InlineKeyboardButton("🔄 Обновить", callback_data="menu_queue")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_main")]
        ]

        text = "📨 **ОЧЕРЕДЬ**\n━━━━━━━━━━━━━━━━━━\n"

        if not queue:
            text += "📭 Очередь пуста"
        else:
            for q in queue[:20]:
                text += f"\n👤 @{q['username']}\n"
                text += f"  Аккаунт: {q['account_id']}\n"
                text += f"  Шаг: {q['step']}\n"

        await query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
        )

    # ============ ОБРАБОТЧИКИ КНОПОК ============

    async def handle_callback(self, update, context):
        query = update.callback_query
        await query.answer()
        data = query.data

        # Навигация
        if data == "back_main":
            await self.main_menu(update)
            return

        if data == "menu_accounts":
            await self.menu_accounts(update)
            return

        if data == "menu_group":
            await self.menu_group(update)
            return

        if data == "menu_reply_group":
            await self.menu_reply_group(update)
            return

        if data == "menu_texts":
            await self.menu_texts(update)
            return

        if data == "menu_found":
            await self.menu_found(update)
            return

        if data == "menu_stats":
            await self.menu_stats(update)
            return

        if data == "menu_queue":
            await self.menu_queue(update)
            return

        # Добавление аккаунта
        if data == "add_account":
            await self.add_account_start(update, context)
            return

        # Группы
        if data == "set_group":
            await self.set_group_start(update, context)
            return

        if data == "set_reply_group":
            await self.set_reply_group_start(update, context)
            return

        # Тексты
        if data.startswith("edit_text_"):
            await self.edit_text_start(update, context)
            return

        # Включение/выключение аккаунта
        if data.startswith("toggle_acc_"):
            account_id = int(data.split("_")[2])
            acc = self.db.get_account(account_id)
            if acc:
                new_status = not bool(acc['is_active'])
                self.db.toggle_account(account_id, new_status)
                if not new_status:
                    await self.bot_manager.stop_account(account_id)
                await self.menu_accounts(update)
            return

        # Включение/выключение мониторинга
        if data.startswith("toggle_mon_"):
            account_id = int(data.split("_")[2])
            acc = self.db.get_account(account_id)
            if acc:
                new_status = not bool(acc.get('is_monitoring', 1))
                self.db.toggle_monitoring(account_id, new_status)
                await self.menu_accounts(update)
            return

        # Удаление аккаунта
        if data.startswith("del_acc_"):
            account_id = int(data.split("_")[2])
            await self.bot_manager.stop_account(account_id)
            self.db.delete_account(account_id)
            await self.menu_accounts(update)
            return

        # Написать или пропустить
        if data.startswith("write_"):
            parts = data.split("_")
            found_id = int(parts[1])
            account_id = int(parts[2])

            # Отправляем 1-й текст
            result = await self.bot_manager.send_first_message(found_id, account_id)

            if result:
                await query.edit_message_text(
                    f"✅ Сообщение отправлено!\n"
                    f"Пользователь получит 1-й текст.\n"
                    f"После ответа будет отправлен 2-й текст."
                )
            else:
                await query.edit_message_text(
                    f"❌ Ошибка отправки.\n"
                    f"Проверьте аккаунт и текст."
                )
            return

        if data.startswith("skip_"):
            parts = data.split("_")
            found_id = int(parts[1])

            self.db.skip_found_user(found_id)
            await query.edit_message_text("✅ Пользователь пропущен")
            return

    # ============ ЗАПУСК ============

    async def run(self):
        self.bot_app = Application.builder().token(BOT_TOKEN).build()
        self.bot_manager.set_bot_app(self.bot_app)

        # ConversationHandler
        conv = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.add_account_start, pattern="^add_account$"),
                CallbackQueryHandler(self.set_group_start, pattern="^set_group$"),
                CallbackQueryHandler(self.set_reply_group_start, pattern="^set_reply_group$"),
                CallbackQueryHandler(self.edit_text_start, pattern="^edit_text_"),
            ],
            states={
                AUTH_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_phone)],
                AUTH_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_code)],
                AUTH_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_password)],
                ADD_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_group_confirm)],
                EDIT_TEXTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.edit_text_confirm)],
            },
            fallbacks=[CommandHandler('cancel', self.cancel)],
        )

        self.bot_app.add_handler(conv)
        self.bot_app.add_handler(CommandHandler('start', self.start))
        self.bot_app.add_handler(CommandHandler('menu', self.start))
        self.bot_app.add_handler(CallbackQueryHandler(self.handle_callback))

        # Запускаем юзерботов
        asyncio.create_task(self.bot_manager.run_all())

        await self.bot_app.initialize()
        await self.bot_app.start()
        await self.bot_app.updater.start_polling()

        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            await self.bot_manager.stop_all()
            await self.bot_app.updater.stop()
            await self.bot_app.stop()

    async def start(self, update, context):
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Доступ запрещён")
            return
        await self.main_menu(update)

    async def cancel(self, update, context):
        context.user_data.clear()
        await update.message.reply_text("✅ Операция отменена")
        await self.main_menu(update)


# ==================== ЗАПУСК ====================

async def main():
    system = BotSystem()
    await system.run()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹ Система остановлена")
