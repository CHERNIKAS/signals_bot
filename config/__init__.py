#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Конфигурация приложения"""

from config.config import (
    # Main config
    CONFIG,
    # UI flags
    SHOW_TRAIL_AS_ATR_HINT,
    SHOW_CONFLUENCE_BLOCK,
    HIDE_DELTA_BLOCKS,
    PCT_DECIMALS,
    XATR_DECIMALS,
    FULL_CONFLUENCE_DETAILS,
    # Constants
    MAX_WORKERS,
    EVAL_TIMEOUT_S,
    FRESH_ONLY,
    _TICK_TIMEOUT,
    PAGE_SIZE,
    SYMBOLS_PER_PAGE,
    # Functions
    load_env_with_fallback,
)

__all__ = [
    "CONFIG",
    "SHOW_TRAIL_AS_ATR_HINT",
    "SHOW_CONFLUENCE_BLOCK",
    "HIDE_DELTA_BLOCKS",
    "PCT_DECIMALS",
    "XATR_DECIMALS",
    "FULL_CONFLUENCE_DETAILS",
    "MAX_WORKERS",
    "EVAL_TIMEOUT_S",
    "FRESH_ONLY",
    "_TICK_TIMEOUT",
    "PAGE_SIZE",
    "SYMBOLS_PER_PAGE",
    "load_env_with_fallback",
]
