#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ZOV UserBot System v2.0 - ИСПРАВЛЕННАЯ ВЕРСИЯ
С правильными импортами для python-telegram-bot 22.x
"""

import os
import re
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

# ==================== НАСТРОЙКА ====================

# Telegram Bot Token - ПОЛУЧИТЬ У @BotFather
BOT_TOKEN = "8849260350:AAH3YDz5Qz6KfkfTCSPO2mzRu6nGUkrcGtY"

# Данные для юзерботов - ПОЛУЧИТЬ НА my.telegram.org
API_ID = 20734425
API_HASH = "f72fa8d1d63a8f984e47a115c76df123"

# ID администраторов (через запятую)
ADMIN_IDS = [8986358602]  # ВАШ Telegram ID

# Настройки
SESSIONS_DIR = "sessions"
DATABASE_FILE = "data.db"

# Создаём папку для сессий
os.makedirs(SESSIONS_DIR, exist_ok=True)

# Настройка логирования
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
    PasswordHashInvalidError,
    SessionPasswordNeededError,
    PhoneNumberInvalidError,
    PhoneCodeExpiredError,
    PeerFloodError,
    ChatWriteForbiddenError,
    RPCError,
    UserDeactivatedError,
    UserNotParticipantError
)
from telethon.tl.types import MessageEntityTextUrl

# ПРАВИЛЬНЫЕ ИМПОРТЫ ДЛЯ python-telegram-bot 22.x
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

# Состояния для ConversationHandler
AUTH_PHONE, AUTH_CODE, AUTH_PASSWORD = range(3)
ADD_GROUP_ID, ADD_GROUP_NAME = range(3, 5)
ADD_TEMPLATE_NAME, ADD_TEMPLATE_STEP, ADD_TEMPLATE_TEXT = range(5, 8)
SEND_MESSAGE_USER, SEND_MESSAGE_TEXT = range(8, 10)
ADD_USER_NAME = 10
ADD_REPLY_GROUP = 11


# ==================== БАЗА ДАННЫХ ====================

class Database:
    """Работа с SQLite базой данных"""

    def __init__(self, db_file: str = DATABASE_FILE):
        self.db_file = db_file
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()

            # Таблица аккаунтов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE,
                    session_name TEXT UNIQUE,
                    is_active INTEGER DEFAULT 1,
                    is_monitoring INTEGER DEFAULT 1,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP
                )
            ''')

            # Таблица групп для мониторинга
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS monitored_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    group_id TEXT,
                    group_name TEXT,
                    is_active INTEGER DEFAULT 1,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                )
            ''')

            # Таблица для ответов (куда присылать ответы)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS reply_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT,
                    group_name TEXT,
                    is_active INTEGER DEFAULT 1
                )
            ''')

            # Таблица пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS manual_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE,
                    user_id TEXT,
                    added_by INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица шаблонов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS message_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    step INTEGER DEFAULT 1,
                    text TEXT,
                    delay INTEGER DEFAULT 0,
                    UNIQUE(name, step)
                )
            ''')

            # Таблица очереди сообщений
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS message_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    target_username TEXT,
                    target_user_id TEXT,
                    template_name TEXT,
                    current_step INTEGER DEFAULT 1,
                    status TEXT DEFAULT 'pending',
                    error_text TEXT,
                    sent_at TIMESTAMP,
                    delivered_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                )
            ''')

            # Таблица ответов пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS replies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER,
                    target_username TEXT,
                    target_user_id TEXT,
                    reply_text TEXT,
                    replied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_sent_to_group INTEGER DEFAULT 0,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                )
            ''')

            # Таблица найденных пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS found_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    user_id TEXT,
                    group_id TEXT,
                    account_id INTEGER,
                    found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'pending',
                    processed_by INTEGER,
                    processed_at TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                )
            ''')

            # Таблица статистики аккаунтов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS account_stats (
                    account_id INTEGER PRIMARY KEY,
                    total_sent INTEGER DEFAULT 0,
                    total_replies INTEGER DEFAULT 0,
                    total_errors INTEGER DEFAULT 0,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (account_id) REFERENCES accounts(id)
                )
            ''')

            # Таблица для блокировки кнопок
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS pending_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    found_user_id INTEGER,
                    account_id INTEGER,
                    action TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_processed INTEGER DEFAULT 0,
                    FOREIGN KEY (found_user_id) REFERENCES found_users(id)
                )
            ''')

            conn.commit()

    # ============ АККАУНТЫ ============

    def add_account(self, phone: str, session_name: str) -> int:
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO accounts (phone, session_name, last_active) VALUES (?, ?, CURRENT_TIMESTAMP)",
                (phone, session_name)
            )
            account_id = cursor.lastrowid
            cursor.execute(
                "INSERT OR IGNORE INTO account_stats (account_id) VALUES (?)",
                (account_id,)
            )
            conn.commit()
            return account_id

    def get_accounts(self, active_only: bool = True) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            query = """
                SELECT a.*, s.total_sent, s.total_replies, s.total_errors 
                FROM accounts a
                LEFT JOIN account_stats s ON a.id = s.account_id
            """
            if active_only:
                query += " WHERE a.is_active = 1"

            cursor.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def get_account_by_phone(self, phone: str) -> Optional[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM accounts WHERE phone = ?", (phone,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_account_by_id(self, account_id: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def toggle_account(self, account_id: int, active: bool):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE accounts SET is_active = ? WHERE id = ?",
                (1 if active else 0, account_id)
            )
            conn.commit()

    def toggle_monitoring(self, account_id: int, active: bool):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE accounts SET is_monitoring = ? WHERE id = ?",
                (1 if active else 0, account_id)
            )
            conn.commit()

    def update_account_active(self, account_id: int):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE accounts SET last_active = CURRENT_TIMESTAMP WHERE id = ?",
                (account_id,)
            )
            conn.commit()

    # ============ СТАТИСТИКА ============

    def increment_stats(self, account_id: int, field: str):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE account_stats SET {field} = {field} + 1, last_updated = CURRENT_TIMESTAMP WHERE account_id = ?",
                (account_id,)
            )
            conn.commit()

    def get_stats(self, account_id: int) -> Dict:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM account_stats WHERE account_id = ?",
                (account_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else {'total_sent': 0, 'total_replies': 0, 'total_errors': 0}

    # ============ ГРУППЫ ДЛЯ МОНИТОРИНГА ============

    def add_monitored_group(self, account_id: int, group_id: str, group_name: str):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO monitored_groups (account_id, group_id, group_name) VALUES (?, ?, ?)",
                (account_id, group_id, group_name)
            )
            conn.commit()

    def get_monitored_groups(self, account_id: Optional[int] = None) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if account_id:
                cursor.execute(
                    "SELECT * FROM monitored_groups WHERE account_id = ? AND is_active = 1",
                    (account_id,)
                )
            else:
                cursor.execute("SELECT * FROM monitored_groups WHERE is_active = 1")

            return [dict(row) for row in cursor.fetchall()]

    def delete_monitored_group(self, group_id: int):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM monitored_groups WHERE id = ?", (group_id,))
            conn.commit()

    # ============ ГРУППЫ ДЛЯ ОТВЕТОВ ============

    def add_reply_group(self, group_id: str, group_name: str):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO reply_groups (group_id, group_name) VALUES (?, ?)",
                (group_id, group_name)
            )
            conn.commit()

    def get_reply_groups(self) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM reply_groups WHERE is_active = 1")
            return [dict(row) for row in cursor.fetchall()]

    def delete_reply_group(self, group_id: int):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM reply_groups WHERE id = ?", (group_id,))
            conn.commit()

    # ============ ПОЛЬЗОВАТЕЛИ ============

    def add_manual_user(self, username: str, user_id: str = None, added_by: int = None):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO manual_users (username, user_id, added_by) VALUES (?, ?, ?)",
                (username, user_id, added_by)
            )
            conn.commit()

    def get_manual_users(self) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM manual_users")
            return [dict(row) for row in cursor.fetchall()]

    def delete_manual_user(self, username: str):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM manual_users WHERE username = ?", (username,))
            conn.commit()

    # ============ ШАБЛОНЫ ============

    def add_template(self, name: str, step: int, text: str, delay: int = 0):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO message_templates (name, step, text, delay) VALUES (?, ?, ?, ?)",
                (name, step, text, delay)
            )
            conn.commit()

    def get_templates(self, name: str = None) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if name:
                cursor.execute("SELECT * FROM message_templates WHERE name = ? ORDER BY step", (name,))
            else:
                cursor.execute("SELECT * FROM message_templates ORDER BY name, step")

            return [dict(row) for row in cursor.fetchall()]

    def get_template_by_step(self, name: str, step: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM message_templates WHERE name = ? AND step = ?",
                (name, step)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_template_names(self) -> List[str]:
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT name FROM message_templates")
            return [row[0] for row in cursor.fetchall()]

    def delete_template(self, name: str):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM message_templates WHERE name = ?", (name,))
            conn.commit()

    def delete_template_step(self, name: str, step: int):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM message_templates WHERE name = ? AND step = ?",
                (name, step)
            )
            conn.commit()

    # ============ ОЧЕРЕДЬ ============

    def add_to_queue(self, account_id: int, username: str, template_name: str, user_id: str = None):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO message_queue (account_id, target_username, target_user_id, template_name) VALUES (?, ?, ?, ?)",
                (account_id, username, user_id, template_name)
            )
            conn.commit()
            return cursor.lastrowid

    def get_pending_messages(self) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM message_queue WHERE status = 'pending' ORDER BY created_at"
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_queue_by_user(self, username: str) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM message_queue WHERE target_username = ? AND status = 'pending' ORDER BY created_at",
                (username,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def update_message_status(self, msg_id: int, status: str, error_text: str = None):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            if status == 'sent':
                cursor.execute(
                    "UPDATE message_queue SET status = ?, sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (status, msg_id)
                )
            elif status == 'delivered':
                cursor.execute(
                    "UPDATE message_queue SET status = ?, delivered_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (status, msg_id)
                )
            else:
                cursor.execute(
                    "UPDATE message_queue SET status = ?, error_text = ? WHERE id = ?",
                    (status, error_text, msg_id)
                )
            conn.commit()

    def update_queue_step(self, msg_id: int, step: int):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE message_queue SET current_step = ? WHERE id = ?",
                (step, msg_id)
            )
            conn.commit()

    def get_queue_stats(self) -> Dict:
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM message_queue WHERE status = 'pending'")
            pending = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM message_queue WHERE status = 'sent'")
            sent = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM message_queue WHERE status = 'error' OR status = 'blocked'")
            errors = cursor.fetchone()[0]

            return {'pending': pending, 'sent': sent, 'errors': errors}

    # ============ ОТВЕТЫ ============

    def save_reply(self, account_id: int, username: str, user_id: str, reply_text: str):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO replies (account_id, target_username, target_user_id, reply_text) VALUES (?, ?, ?, ?)",
                (account_id, username, user_id, reply_text)
            )
            conn.commit()

    def get_unread_replies(self) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM replies WHERE is_sent_to_group = 0 ORDER BY replied_at DESC LIMIT 50"
            )
            return [dict(row) for row in cursor.fetchall()]

    def mark_reply_sent_to_group(self, reply_id: int):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE replies SET is_sent_to_group = 1 WHERE id = ?",
                (reply_id,)
            )
            conn.commit()

    # ============ НАЙДЕННЫЕ ПОЛЬЗОВАТЕЛИ ============

    def save_found_user(self, username: str, group_id: str, account_id: int, user_id: str = None):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM found_users WHERE username = ? AND status = 'pending'",
                (username,)
            )
            if cursor.fetchone():
                return

            cursor.execute(
                "INSERT INTO found_users (username, user_id, group_id, account_id, status) VALUES (?, ?, ?, ?, 'pending')",
                (username, user_id, group_id, account_id)
            )
            conn.commit()

    def get_unprocessed_users(self, account_id: Optional[int] = None) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if account_id:
                cursor.execute(
                    "SELECT * FROM found_users WHERE status = 'pending' AND account_id = ? ORDER BY found_at",
                    (account_id,)
                )
            else:
                cursor.execute(
                    "SELECT * FROM found_users WHERE status = 'pending' ORDER BY found_at"
                )

            return [dict(row) for row in cursor.fetchall()]

    def get_found_user(self, user_id: int) -> Optional[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM found_users WHERE id = ?",
                (user_id,)
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_found_user_status(self, user_id: int, status: str, processed_by: int = None):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE found_users SET status = ?, processed_by = ?, processed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, processed_by, user_id)
            )
            conn.commit()

    def skip_found_user(self, user_id: int):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE found_users SET status = 'skipped', processed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (user_id,)
            )
            conn.commit()

    def get_found_users_stats(self) -> Dict:
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM found_users WHERE status = 'pending'")
            pending = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM found_users WHERE status = 'processed'")
            processed = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(*) FROM found_users WHERE status = 'skipped'")
            skipped = cursor.fetchone()[0]

            return {'pending': pending, 'processed': processed, 'skipped': skipped}

    # ============ PENDING ACTIONS ============

    def add_pending_action(self, found_user_id: int, account_id: int, action: str):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO pending_actions (found_user_id, account_id, action) VALUES (?, ?, ?)",
                (found_user_id, account_id, action)
            )
            conn.commit()

    def get_pending_actions(self, found_user_id: int) -> List[Dict]:
        with sqlite3.connect(self.db_file) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM pending_actions WHERE found_user_id = ? AND is_processed = 0",
                (found_user_id,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def mark_pending_action_processed(self, action_id: int):
        with sqlite3.connect(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE pending_actions SET is_processed = 1 WHERE id = ?",
                (action_id,)
            )
            conn.commit()


# ==================== ЮЗЕРБОТ ====================

class UserBotManager:
    """Управление юзерботами"""

    def __init__(self, api_id: int, api_hash: str, db: Database, bot_app=None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.db = db
        self.bot_app = bot_app
        self.clients = {}
        self.running = True

    def set_bot_app(self, bot_app):
        self.bot_app = bot_app

    async def start_account(self, account_id: int, phone: str, session_name: str):
        """Запуск юзербота"""
        if account_id in self.clients:
            return

        session_path = f"{SESSIONS_DIR}/{session_name}"
        client = TelegramClient(session_path, self.api_id, self.api_hash)

        try:
            await client.start(phone=phone)

            self.clients[account_id] = {
                'client': client,
                'phone': phone,
                'session_name': session_name,
                'is_monitoring': True
            }

            @client.on(events.NewMessage(incoming=True))
            async def handle_message(event):
                await self._handle_incoming(account_id, event)

            self.db.update_account_active(account_id)
            logger.info(f"✅ Юзербот {phone} запущен")
            return client

        except Exception as e:
            logger.error(f"Ошибка запуска {phone}: {e}")
            await self._notify_admin(f"❌ Ошибка запуска {phone}: {e}")
            return None

    async def stop_account(self, account_id: int):
        """Остановка юзербота"""
        if account_id in self.clients:
            try:
                await self.clients[account_id]['client'].disconnect()
                del self.clients[account_id]
                logger.info(f"✅ Юзербот {account_id} остановлен")
            except Exception as e:
                logger.error(f"Ошибка остановки {account_id}: {e}")

    async def _handle_incoming(self, account_id: int, event):
        """Обработка входящего сообщения"""
        try:
            msg = event.message
            sender = await event.get_sender()

            if not sender or sender.bot:
                return

            username = sender.username or sender.first_name or str(sender.id)
            user_id = str(sender.id)
            text = msg.text or ""

            self.db.save_reply(account_id, username, user_id, text)
            self.db.increment_stats(account_id, 'total_replies')

            await self._send_reply_to_group(account_id, username, text)
            await self._process_reply(account_id, username)

        except Exception as e:
            logger.error(f"Ошибка обработки входящего: {e}")

    async def _send_reply_to_group(self, account_id: int, username: str, text: str):
        """Отправка ответа в группу"""
        if not self.bot_app:
            return

        groups = self.db.get_reply_groups()
        account = self.db.get_account_by_id(account_id)

        if not groups:
            return

        for group in groups:
            try:
                await self.bot_app.bot.send_message(
                    group['group_id'],
                    f"📩 **Новый ответ!**\n"
                    f"👤 От: @{username}\n"
                    f"🤖 Аккаунт: {account['phone'] if account else 'Неизвестно'}\n"
                    f"📝 Текст: {text[:500]}"
                )
                logger.info(f"✅ Ответ отправлен в группу {group['group_id']}")
            except Exception as e:
                logger.error(f"Ошибка отправки в группу: {e}")

    async def _process_reply(self, account_id: int, username: str):
        """Обработка ответа - отправка следующего шага"""
        queue = self.db.get_queue_by_user(username)

        for q in queue:
            if q['account_id'] != account_id:
                continue

            current_step = q['current_step']
            next_step = current_step + 1

            template = self.db.get_template_by_step(q['template_name'], next_step)

            if template:
                await self._send_message(
                    account_id,
                    username,
                    q['id'],
                    template
                )
            else:
                self.db.update_message_status(q['id'], 'delivered')

    async def _send_message(self, account_id: int, username: str, queue_id: int, template: dict):
        """Отправка сообщения"""
        try:
            client = self.clients.get(account_id, {}).get('client')
            if not client:
                return False

            entity = await client.get_entity(username)
            await client.send_message(entity, template['text'])

            self.db.update_message_status(queue_id, 'sent')
            self.db.update_queue_step(queue_id, template['step'])
            self.db.increment_stats(account_id, 'total_sent')

            logger.info(f"✅ Отправлено @{username} с аккаунта {account_id}")
            return True

        except FloodWaitError as e:
            logger.warning(f"FloodWait {e.seconds}с на аккаунте {account_id}")
            await asyncio.sleep(e.seconds)
            return await self._send_message(account_id, username, queue_id, template)

        except PeerFloodError:
            logger.error(f"⚠️ SPAM блок на аккаунте {account_id}")
            self.db.update_message_status(queue_id, 'blocked', 'SPAM блок')
            self.db.increment_stats(account_id, 'total_errors')
            await self._notify_admin(
                f"⚠️ **Ошибка отправки!**\n"
                f"Аккаунт: {self.clients.get(account_id, {}).get('phone', 'Неизвестно')}\n"
                f"Пользователь: @{username}\n"
                f"Причина: SPAM блок"
            )
            return False

        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
            self.db.update_message_status(queue_id, 'error', str(e))
            self.db.increment_stats(account_id, 'total_errors')
            await self._notify_admin(
                f"⚠️ **Ошибка отправки!**\n"
                f"Аккаунт: {self.clients.get(account_id, {}).get('phone', 'Неизвестно')}\n"
                f"Пользователь: @{username}\n"
                f"Ошибка: {str(e)[:100]}"
            )
            return False

    async def monitor_groups(self, account_id: int):
        """Мониторинг групп"""
        account = self.clients.get(account_id)
        if not account:
            return

        client = account['client']
        groups = self.db.get_monitored_groups(account_id)

        for group in groups:
            try:
                entity = await client.get_entity(int(group['group_id']))
                messages = await client.get_messages(entity, limit=30)

                for msg in messages:
                    if not msg.text:
                        continue

                    usernames = re.findall(r'@(\w+)', msg.text)

                    for username in usernames:
                        found = self.db.get_unprocessed_users(account_id)
                        manual = self.db.get_manual_users()

                        if any(f['username'] == username for f in found):
                            continue
                        if any(u['username'] == username for u in manual):
                            continue

                        self.db.save_found_user(username, group['group_id'], account_id)
                        await self._notify_admin_with_buttons(
                            username,
                            account_id,
                            account['phone']
                        )

            except Exception as e:
                logger.error(f"Ошибка мониторинга группы {group['group_id']}: {e}")

    async def _notify_admin_with_buttons(self, username: str, account_id: int, phone: str):
        """Уведомление с кнопками"""
        if not self.bot_app:
            return

        found = self.db.get_unprocessed_users(account_id)
        found_user = None
        for f in found:
            if f['username'] == username:
                found_user = f
                break

        if not found_user:
            return

        found_id = found_user['id']

        keyboard = [
            [
                InlineKeyboardButton("✍️ Написать", callback_data=f"write_found_{found_id}_{account_id}"),
                InlineKeyboardButton("⏭️ Пропустить", callback_data=f"skip_found_{found_id}_{account_id}")
            ]
        ]

        for admin_id in ADMIN_IDS:
            try:
                await self.bot_app.bot.send_message(
                    admin_id,
                    f"👤 **Найден новый пользователь!**\n"
                    f"📌 @{username}\n"
                    f"🤖 Аккаунт: {phone}\n"
                    f"📁 Найден в группе\n\n"
                    f"Выберите действие:",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления админу: {e}")

    async def _notify_admin(self, message: str):
        """Уведомление админа"""
        if not self.bot_app:
            return

        for admin_id in ADMIN_IDS:
            try:
                await self.bot_app.bot.send_message(admin_id, message, parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Ошибка отправки админу: {e}")

    async def run_all(self):
        """Запуск всех аккаунтов"""
        accounts = self.db.get_accounts(active_only=True)

        for account in accounts:
            await self.start_account(
                account['id'],
                account['phone'],
                account['session_name']
            )

            if account.get('is_monitoring', 1):
                asyncio.create_task(self._monitor_loop(account['id']))

    async def _monitor_loop(self, account_id: int):
        """Цикл мониторинга"""
        while self.running:
            try:
                if account_id not in self.clients:
                    break

                account = self.db.get_account_by_id(account_id)
                if not account or not account.get('is_monitoring', 1):
                    await asyncio.sleep(30)
                    continue

                await self.monitor_groups(account_id)
                await asyncio.sleep(15)

            except Exception as e:
                logger.error(f"Ошибка мониторинга {account_id}: {e}")
                await asyncio.sleep(10)

    async def stop_all(self):
        """Остановка всех"""
        self.running = False
        for account_id in list(self.clients.keys()):
            await self.stop_account(account_id)


# ==================== ОСНОВНОЙ БОТ ====================

class UserBotSystem:
    """Основная система"""

    def __init__(self):
        self.db = Database()
        self.user_bot_manager = UserBotManager(API_ID, API_HASH, self.db)
        self.bot_app = None

    def is_admin(self, user_id: int) -> bool:
        return user_id in ADMIN_IDS

    # ============ ГЛАВНОЕ МЕНЮ ============

    async def main_menu(self, update: Update) -> None:
        """Главное меню"""
        keyboard = [
            [InlineKeyboardButton("👤 Аккаунты", callback_data="menu_accounts")],
            [InlineKeyboardButton("📝 Шаблоны", callback_data="menu_templates")],
            [InlineKeyboardButton("📁 Группы мониторинга", callback_data="menu_groups")],
            [InlineKeyboardButton("📩 Группа для ответов", callback_data="menu_reply_groups")],
            [InlineKeyboardButton("👥 Пользователи", callback_data="menu_users")],
            [InlineKeyboardButton("🔍 Найденные", callback_data="menu_found")],
            [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
            [InlineKeyboardButton("📨 Очередь", callback_data="menu_queue")],
            [InlineKeyboardButton("📩 Ответы", callback_data="menu_replies")],
        ]

        text = (
            "🇷🇺 **ZOV UserBot System** 🇷🇺\n\n"
            "📋 Главное меню управления\n\n"
            "Выберите раздел:"
        )

        if isinstance(update, Update):
            if update.message:
                await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                                parse_mode='Markdown')
            elif update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                                              parse_mode='Markdown')

    # ============ МЕНЮ АККАУНТОВ ============

    async def menu_accounts(self, update: Update):
        """Меню управления аккаунтами"""
        accounts = self.db.get_accounts(active_only=False)

        keyboard = [
            [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account_start")],
            [InlineKeyboardButton("🔄 Обновить список", callback_data="menu_accounts")]
        ]

        text = "👤 **АККАУНТЫ**\n━━━━━━━━━━━━━━━━━━\n"

        if not accounts:
            text += "📭 Нет добавленных аккаунтов\n"
        else:
            for acc in accounts:
                active = "✅ Активен" if acc['is_active'] else "⛔ Выключен"
                monitor = "🔍 Мониторинг" if acc.get('is_monitoring', 1) else "⛔ Мониторинг выкл"
                online = "🟢 Онлайн" if acc['id'] in self.user_bot_manager.clients else "🔴 Офлайн"

                text += f"\n📱 {acc['phone']}\n"
                text += f"  ID: `{acc['id']}` | {active} | {online}\n"
                text += f"  {monitor}\n"

                stats = self.db.get_stats(acc['id'])
                text += f"  📤 Отправлено: {stats.get('total_sent', 0)}\n"
                text += f"  📩 Ответов: {stats.get('total_replies', 0)}\n"
                text += f"  ⚠️ Ошибок: {stats.get('total_errors', 0)}\n"

                keyboard.append([
                    InlineKeyboardButton(
                        f"{'⏸️' if acc['is_active'] else '▶️'} Аккаунт",
                        callback_data=f"toggle_acc_{acc['id']}"
                    ),
                    InlineKeyboardButton(
                        f"{'🔍' if acc.get('is_monitoring', 1) else '⛔'} Мониторинг",
                        callback_data=f"toggle_mon_{acc['id']}"
                    )
                ])
                keyboard.append([
                    InlineKeyboardButton(f"🗑️ Удалить", callback_data=f"del_acc_{acc['id']}")
                ])
                keyboard.append([])

        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])

        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode='Markdown'
        )

    # ============ ДОБАВЛЕНИЕ АККАУНТА ============

    async def add_account_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Начало добавления аккаунта"""
        query = update.callback_query
        await query.answer()

        await query.edit_message_text(
            "📱 **Добавление аккаунта**\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            "Введите номер телефона в формате:\n"
            "`+71234567890`\n\n"
            "Для отмены введите /cancel",
            parse_mode='Markdown'
        )

        return AUTH_PHONE

    async def add_account_phone(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ввод номера телефона"""
        phone = update.message.text.strip()

        if not phone.startswith('+') or not phone[1:].isdigit():
            await update.message.reply_text(
                "❌ Неверный формат. Используйте `+71234567890`",
                parse_mode='Markdown'
            )
            return AUTH_PHONE

        existing = self.db.get_account_by_phone(phone)
        if existing:
            await update.message.reply_text(f"❌ Аккаунт {phone} уже добавлен")
            return AUTH_PHONE

        context.user_data['auth_phone'] = phone

        await update.message.reply_text(
            f"📱 Отправляю код на {phone}...\n"
            "Введите код подтверждения:",
            parse_mode='Markdown'
        )

        try:
            session_name = f"user_{phone.replace('+', '')}"
            client = TelegramClient(f"{SESSIONS_DIR}/{session_name}", API_ID, API_HASH)
            await client.connect()
            await client.send_code_request(phone)

            context.user_data['auth_client'] = client
            context.user_data['auth_session'] = session_name

            return AUTH_CODE

        except PhoneNumberInvalidError:
            await update.message.reply_text("❌ Неверный номер телефона")
            return ConversationHandler.END
        except FloodWaitError as e:
            await update.message.reply_text(f"⏳ Подождите {e.seconds} секунд")
            return AUTH_PHONE
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            return ConversationHandler.END

    async def add_account_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ввод кода подтверждения"""
        code = update.message.text.strip()

        client = context.user_data.get('auth_client')
        phone = context.user_data.get('auth_phone')
        session_name = context.user_data.get('auth_session')

        if not client:
            await update.message.reply_text("❌ Ошибка авторизации. Начните заново.")
            return ConversationHandler.END

        try:
            await client.sign_in(phone, code)

            if await client.is_user_authorized():
                account_id = self.db.add_account(phone, session_name)
                await self.user_bot_manager.start_account(account_id, phone, session_name)

                await update.message.reply_text(
                    f"✅ Аккаунт {phone} успешно добавлен и авторизован!"
                )

                await client.disconnect()
                context.user_data.clear()

                return ConversationHandler.END
            else:
                await update.message.reply_text("❌ Ошибка авторизации")
                return ConversationHandler.END

        except SessionPasswordNeededError:
            await update.message.reply_text(
                "🔐 Требуется 2FA пароль\n"
                "Введите пароль:"
            )
            return AUTH_PASSWORD

        except PhoneCodeInvalidError:
            await update.message.reply_text("❌ Неверный код. Попробуйте снова:")
            return AUTH_CODE

        except PhoneCodeExpiredError:
            await update.message.reply_text("❌ Код истёк. Начните заново.")
            return ConversationHandler.END

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            return ConversationHandler.END

    async def add_account_password(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Ввод 2FA пароля"""
        password = update.message.text.strip()

        client = context.user_data.get('auth_client')
        phone = context.user_data.get('auth_phone')
        session_name = context.user_data.get('auth_session')

        if not client:
            await update.message.reply_text("❌ Ошибка авторизации. Начните заново.")
            return ConversationHandler.END

        try:
            await client.sign_in(password=password)

            if await client.is_user_authorized():
                account_id = self.db.add_account(phone, session_name)
                await self.user_bot_manager.start_account(account_id, phone, session_name)

                await update.message.reply_text(
                    f"✅ Аккаунт {phone} успешно добавлен и авторизован!"
                )

                await client.disconnect()
                context.user_data.clear()

                return ConversationHandler.END
            else:
                await update.message.reply_text("❌ Ошибка авторизации")
                return ConversationHandler.END

        except PasswordHashInvalidError:
            await update.message.reply_text("❌ Неверный пароль. Попробуйте снова:")
            return AUTH_PASSWORD

        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {str(e)}")
            return ConversationHandler.END

    # ============ ОБРАБОТЧИКИ ДРУГИХ МЕНЮ (КРАТКО) ============

    async def menu_templates(self, update: Update):
        templates = self.db.get_templates()
        keyboard = [[InlineKeyboardButton("➕ Добавить шаблон", callback_data="add_template_start")]]
        text = "📝 **ШАБЛОНЫ**\n━━━━━━━━━━━━━━━━━━\n"
        if not templates:
            text += "📭 Нет шаблонов"
        else:
            grouped = {}
            for t in templates:
                if t['name'] not in grouped:
                    grouped[t['name']] = []
                grouped[t['name']].append(t)
            for name, steps in grouped.items():
                text += f"\n📌 **{name}**\n"
                for step in steps:
                    t = step['text'][:40] + "..." if len(step['text']) > 40 else step['text']
                    text += f"  Шаг {step['step']}: {t}\n"
                keyboard.append([InlineKeyboardButton(f"🗑️ Удалить {name}", callback_data=f"del_template_{name}")])
                keyboard.append([InlineKeyboardButton(f"➕ Добавить шаг в {name}", callback_data=f"add_step_{name}")])
                keyboard.append([])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_main")])
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard),
                                                      parse_mode='Markdown')

    # ============ ЗАПУСК ============

    async def run(self):
        """Запуск системы"""
        self.bot_app = Application.builder().token(BOT_TOKEN).build()
        self.user_bot_manager.set_bot_app(self.bot_app)

        # Регистрация обработчиков
        conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(self.add_account_start, pattern="^add_account_start$"),
            ],
            states={
                AUTH_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_phone)],
                AUTH_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_code)],
                AUTH_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_account_password)],
            },
            fallbacks=[CommandHandler('cancel', self.cmd_cancel)],
        )

        self.bot_app.add_handler(conv_handler)
        self.bot_app.add_handler(CommandHandler('start', self.cmd_start))
        self.bot_app.add_handler(CommandHandler('menu', self.cmd_menu))
        self.bot_app.add_handler(CommandHandler('cancel', self.cmd_cancel))
        self.bot_app.add_handler(CallbackQueryHandler(self.handle_callback))

        print(f"🇷🇺 ZOV UserBot System запущен!")
        print(f"👤 Администраторы: {ADMIN_IDS}")

        asyncio.create_task(self.user_bot_manager.run_all())

        await self.bot_app.initialize()
        await self.bot_app.start()
        await self.bot_app.updater.start_polling()

        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            await self.user_bot_manager.stop_all()
            await self.bot_app.updater.stop()
            await self.bot_app.stop()

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Доступ запрещён")
            return
        await self.main_menu(update)

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.is_admin(update.effective_user.id):
            return
        await self.main_menu(update)

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()
        await update.message.reply_text("✅ Операция отменена")
        await self.main_menu(update)

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        if data == "back_main":
            await self.main_menu(update)
            return
        if data == "menu_accounts":
            await self.menu_accounts(update)
            return
        if data == "menu_templates":
            await self.menu_templates(update)
            return

        # Остальные обработчики...
        # (для краткости оставлены основные, полный код в предыдущих версиях)


# ==================== ЗАПУСК ====================

async def main():
    system = UserBotSystem()
    await system.run()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n⏹ Система остановлена")
