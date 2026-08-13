#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ZOV PnL BOT v2.0 - Единый монолитный файл
Боевое решение для подсчёта ежедневного PnL TON-кошелька
Курсы валют: TON/USD через TonAPI, USD/RUB через Coingecko
"""

import json
import asyncio
from datetime import datetime, date, timezone, timedelta
from typing import Dict, List, Optional
import aiohttp
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ==================== КОНФИГУРАЦИЯ (ВСЁ ЗДЕСЬ) ====================

# --- Настройки бота ---
BOT_TOKEN = "8849260350:AAH3YDz5Qz6KfkfTCSPO2mzRu6nGUkrcGtY"  # Замените на реальный токен
WALLET_ADDRESS = "UQDfFJfq4mdt51_MD7PZ7MXnnOfXdw3nh18l9x4u8cNCqIh9"  # Замените на адрес кошелька

# --- Настройки TonAPI (tonapi.io) ---
# Получить ключ: https://t.me/tonapi_bot → /get_server_key
TONAPI_KEY = "d8e4df9a1f7c8a744ff7ad67446fee58c7038f918a17b8f051de7c3d8ff3d84b"  # Замените на реальный ключ
TONAPI_BASE_URL = "https://tonapi.io/v2"

# --- Настройки Coingecko (курс USD/RUB) ---
COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

# --- Настройки хранения ---
STATS_FILE = "stats.json"


# ==================== МОДУЛЬ РАБОТЫ С ХРАНИЛИЩЕМ ====================

class Storage:
    """Класс для хранения статистики в JSON-файле"""

    def __init__(self, filename: str = STATS_FILE):
        self.filename = filename
        self._ensure_file()
        self._lock = asyncio.Lock()

    def _ensure_file(self) -> None:
        if not os.path.exists(self.filename):
            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump({}, f, indent=2)

    async def get_today_stats(self, address: str) -> Dict:
        async with self._lock:
            today = date.today().isoformat()

            with open(self.filename, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if address not in data:
                data[address] = {}

            if today not in data[address]:
                data[address][today] = {
                    'incoming': 0.0,
                    'outgoing': 0.0,
                    'tx_count': 0
                }
                with open(self.filename, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)

            return data[address][today].copy()

    async def update_stats(self, address: str, incoming: float, outgoing: float, tx_count: int = 0) -> None:
        async with self._lock:
            today = date.today().isoformat()

            with open(self.filename, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if address not in data:
                data[address] = {}

            if today not in data[address]:
                data[address][today] = {
                    'incoming': 0.0,
                    'outgoing': 0.0,
                    'tx_count': 0
                }

            data[address][today]['incoming'] += incoming
            data[address][today]['outgoing'] += outgoing
            data[address][today]['tx_count'] += tx_count

            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

    async def clear_old_stats(self, days: int = 30) -> None:
        async with self._lock:
            cutoff = date.today() - timedelta(days=days)

            with open(self.filename, 'r', encoding='utf-8') as f:
                data = json.load(f)

            for address in data:
                for d in list(data[address].keys()):
                    if datetime.strptime(d, '%Y-%m-%d').date() < cutoff:
                        del data[address][d]

            with open(self.filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)


# ==================== МОДУЛЬ РАБОТЫ С TONAPI ====================

class TONClient:
    """Клиент для работы с TonAPI (tonapi.io)"""

    def __init__(self, api_key: str = TONAPI_KEY):
        self.api_key = api_key
        self.base_url = TONAPI_BASE_URL
        self.headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Accept': 'application/json'
        }

    async def get_transactions(self, address: str, limit: int = 100) -> List[Dict]:
        """Получение последних транзакций кошелька через TonAPI v2"""
        url = f"{self.base_url}/blockchain/account/{address}/transactions"
        params = {
            'limit': limit,
            'sort': 'desc'
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=self.headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"TonAPI error {resp.status}: {error_text}")
                data = await resp.json()
                return data.get('transactions', [])

    async def get_ton_usd_price(self) -> float:
        """Получение текущего курса TON/USD через TonAPI"""
        url = f"{self.base_url}/rates"
        params = {
            'tokens': 'ton',
            'currencies': 'usd'
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, headers=self.headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"TonAPI error {resp.status}: {error_text}")
                data = await resp.json()
                # Структура: {'rates': {'TON': {'prices': {'USD': 5.50}}}}
                return float(data.get('rates', {}).get('TON', {}).get('prices', {}).get('USD', 0.0))

    async def get_account_info(self, address: str) -> Dict:
        """Получение информации о кошельке (баланс и т.д.)"""
        url = f"{self.base_url}/blockchain/account/{address}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    raise Exception(f"TonAPI error {resp.status}: {error_text}")
                return await resp.json()


# ==================== МОДУЛЬ РАБОТЫ С КУРСАМИ ВАЛЮТ ====================

class CurrencyRates:
    """Получение актуальных курсов валют"""

    @staticmethod
    async def get_usd_rub_rate() -> float:
        """
        Получение курса USD/RUB через Coingecko API
        Возвращает курс (например, 90.0)
        """
        url = f"{COINGECKO_BASE_URL}/exchange_rates"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Ищем RUB в списке курсов
                        for rate in data.get('rates', []):
                            if rate.get('unit') == 'rub':
                                return float(rate.get('value', 0.0))
                    # fallback: пробуем альтернативный эндпоинт
                    return await CurrencyRates._get_usd_rub_alternative()
            except Exception:
                return await CurrencyRates._get_usd_rub_alternative()

    @staticmethod
    async def _get_usd_rub_alternative() -> float:
        """Альтернативный способ получения курса USD/RUB"""
        url = "https://api.exchangerate-api.com/v4/latest/USD"
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data.get('rates', {}).get('RUB', 0.0))
            except Exception:
                pass
        return 90.0  # fallback значение


# ==================== ОСНОВНАЯ ЛОГИКА БОТА ====================

storage = Storage()
ton_client = TONClient()
currency = CurrencyRates()


def parse_ton_amount(value: int) -> float:
    """Перевод из наноTON в TON (1 TON = 10^9 наноTON)"""
    return value / 1e9


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    await update.message.reply_text(
        "🇷🇺 Боевая система ZOV PnL активирована, мой господин.\n\n"
        "Доступные команды:\n"
        "/stats - получить отчёт PnL за сегодня\n"
        "/balance - текущий баланс кошелька\n"
        "/clear - очистить статистику старше 30 дней"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /stats — формирует отчёт PnL за сегодня"""
    await update.message.reply_text("⚡ Выполняю анализ транзакций, мой господин...")

    try:
        # Получаем транзакции за последние 200 операций
        txs = await ton_client.get_transactions(WALLET_ADDRESS, limit=200)
        today_utc = datetime.now(timezone.utc).date()

        incoming = 0.0
        outgoing = 0.0
        tx_count = 0

        for tx in txs:
            # Время транзакции в Unix timestamp
            tx_time = datetime.fromtimestamp(tx.get('utime', 0), tz=timezone.utc).date()
            if tx_time != today_utc:
                continue

            # Анализ входящих и исходящих сообщений
            in_msg = tx.get('in_msg', {})
            out_msgs = tx.get('out_msgs', [])

            # Проверяем входящую транзакцию
            if in_msg and in_msg.get('value', 0) > 0:
                # Если исходящих нет или это служебные, считаем пополнением
                if not out_msgs or len(out_msgs) == 0:
                    incoming += parse_ton_amount(in_msg['value'])
                    tx_count += 1
                else:
                    # Проверяем, не является ли это переводом с возвратом
                    # В TON транзакция может иметь и in, и out
                    pass

            # Обрабатываем исходящие транзакции (списания)
            for out_msg in out_msgs:
                dest = out_msg.get('destination', '')
                if dest and dest.lower() != WALLET_ADDRESS.lower():
                    value = parse_ton_amount(out_msg.get('value', 0))
                    if value > 0:
                        outgoing += value
                        tx_count += 1

        # Сохраняем статистику
        await storage.update_stats(WALLET_ADDRESS, incoming, outgoing, tx_count)
        daily_stats = await storage.get_today_stats(WALLET_ADDRESS)

        total_incoming = daily_stats['incoming']
        total_outgoing = daily_stats['outgoing']
        pnl_ton = total_incoming - total_outgoing

        # Получаем курсы валют
        ton_usd_price = await ton_client.get_ton_usd_price()
        usd_rub_price = await currency.get_usd_rub_rate()

        pnl_usd = pnl_ton * ton_usd_price
        pnl_rub = pnl_usd * usd_rub_price

        # Формируем отчёт
        status = "🔥 ПРИБЫЛЬ" if pnl_ton > 0 else "⚠️ УБЫТОК" if pnl_ton < 0 else "⏸️ НУЛЬ"

        report = (
            f"📊 **ОТЧЁТ ZOV PnL**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 {datetime.now().strftime('%d.%m.%Y')}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 **Пополнения:** +{total_incoming:.4f} TON\n"
            f"💸 **Списания:** -{total_outgoing:.4f} TON\n"
            f"📈 **PnL:** `{pnl_ton:+.4f}` TON\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🇺🇸 **USD:** `{pnl_usd:+.2f}` $\n"
            f"🇷🇺 **RUB:** `{pnl_rub:+.2f}` ₽\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📊 **Статус:** {status}\n"
            f"💱 TON/USD = {ton_usd_price:.4f} | USD/RUB = {usd_rub_price:.2f}\n"
            f"📦 Транзакций за день: {daily_stats.get('tx_count', 0)}"
        )

        await update.message.reply_text(report, parse_mode='Markdown')

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка, мой господин: {str(e)}")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /balance — показывает текущий баланс кошелька"""
    await update.message.reply_text("🔄 Запрашиваю баланс, мой господин...")

    try:
        account_info = await ton_client.get_account_info(WALLET_ADDRESS)
        balance_nano = account_info.get('balance', 0)
        balance_ton = parse_ton_amount(balance_nano)

        ton_usd_price = await ton_client.get_ton_usd_price()
        usd_rub_price = await currency.get_usd_rub_rate()

        balance_usd = balance_ton * ton_usd_price
        balance_rub = balance_usd * usd_rub_price

        msg = (
            f"🏦 **БАЛАНС КОШЕЛЬКА**\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💰 {balance_ton:.4f} TON\n"
            f"🇺🇸 {balance_usd:.2f} USD\n"
            f"🇷🇺 {balance_rub:.2f} RUB\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💱 TON/USD = {ton_usd_price:.4f}\n"
            f"💱 USD/RUB = {usd_rub_price:.2f}"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")


async def clear_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Очистка старой статистики"""
    await storage.clear_old_stats(30)
    await update.message.reply_text("✅ Статистика старше 30 дней очищена, мой господин.")


# ==================== ЗАПУСК БОТА ====================

def main():
    """Главная функция запуска бота"""
    print("🇷🇺 ZOV PnL BOT v2.0 запускается, мой господин...")
    print(f"📍 Кошелёк: {WALLET_ADDRESS}")
    print(f"📁 Файл статистики: {STATS_FILE}")

    app = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем команды
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('stats', stats))
    app.add_handler(CommandHandler('balance', balance))
    app.add_handler(CommandHandler('clear', clear_stats))

    print("✅ Бот готов к работе. Ожидаю приказов.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == '__main__':
    import os  # для работы с файлами

    main()
