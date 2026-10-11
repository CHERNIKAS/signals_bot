#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Точка входа приложения:
- Загрузка конфигурации
- Инициализация Telegram Application
- Регистрация хэндлеров и задач
- Запуск бота
"""

import os
from datetime import timedelta
from telegram.ext import ApplicationBuilder

from config.config import CONFIG, load_env_with_fallback


# ============================================================================
# ФУНКЦИЯ: run
# ОПИСАНИЕ: Главная функция запуска бота
#           Инициализирует Telegram-бот, регистрирует хендлеры и задачи
# ============================================================================
async def error_handler(update, context):
    """Глобальный обработчик ошибок"""
    import traceback
    from telegram.error import Conflict, NetworkError, TimedOut
    
    error = context.error
    
    # Конфликт polling (другая инстанция запущена)
    if isinstance(error, Conflict):
        print("[ERROR] Polling conflict: another bot instance is running with the same token.")
        print("        Stop duplicate processes or ensure a single active replica.")
        print("        Skipping hard exit in error_handler to avoid random process shutdowns.")
        return
    
    # Сетевые ошибки (временные)
    if isinstance(error, (NetworkError, TimedOut)):
        print(f"⚠️ [WARN] Сетевая ошибка: {error}")
        return  # Автоматически повторится
    
    # Остальные ошибки логируем
    print(f"❌ [ERROR] Необработанная ошибка: {error}")
    traceback.print_exc()


def run():
    """Основная точка входа приложения"""
    
    # Загрузка .env и инициализация БД
    load_env_with_fallback()
    
    # Кэш больше не используется - точность определяется динамически
    print("[MAIN] Price formatting: dynamic precision enabled")
    
    # Получение токена
    token = os.getenv(CONFIG["telegram"]["token_env"])
    if not token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в .env")
    
    # Создание Application
    app = ApplicationBuilder().token(token).build()
    
    # Регистрация обработчика ошибок
    app.add_error_handler(error_handler)
    
    # Регистрация хэндлеров
    from handlers.handlers import register
    register(app)
    
    # Примечание: handlers.py будет содержать:
    # - Все CommandHandler (/start, /stop, /status и т.д.)
    # - MessageHandler для текстовых сообщений
    # - CallbackQueryHandler для кнопок
    # - Регистрацию периодических задач через app.job_queue
    
    # Временная заглушка для базового запуска
    print("[MAIN] Application initialized")
    print("[MAIN] Note: handlers registration is pending")
    print("[MAIN] Run 'python -m app.main' to start")
    
    # Запуск бота
    print("Unified bot starting... (Type /start in Telegram)")
    
    import time
    retry_delay = int(os.getenv("POLLING_RETRY_SECONDS", "10"))

    while True:
        try:
            app.run_polling(
                close_loop=False,
                drop_pending_updates=True  # skip old updates on startup
            )
            break
        except KeyboardInterrupt:
            print("\n[MAIN] Stop signal received (Ctrl+C)")
            break
        except Exception as e:
            from telegram.error import TimedOut, NetworkError

            if isinstance(e, (TimedOut, NetworkError)):
                print(f"[MAIN] Telegram API error: {e}")
                print(f"[MAIN] Retrying polling in {retry_delay}s...")
                time.sleep(retry_delay)
                continue

            print(f"[MAIN] Critical error: {e}")
            import traceback
            traceback.print_exc()
            raise


# Защита для прямого запуска
if __name__ == "__main__":
    run()
# ============ КОНЕЦ ФУНКЦИИ: run ============

