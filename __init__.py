#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Unified Signal Bot - модульная архитектура

Пакет app/ содержит:
- config: настройки и константы
- utils: утилиты
- binance: API и расчеты
- database: PostgreSQL БД
- keyboards: клавиатуры Telegram
- messages: форматирование сообщений
- strategy: логика сигналов
- panels: панели управления
- handlers: команды и коллбэки
- jobs: периодические задачи
- analytics: статистика
- main: точка входа

Запуск:
    python -m app
    python -m app.main
    python main.py
"""

__version__ = "2.0.0"
__author__ = "Signal Bot Team"



