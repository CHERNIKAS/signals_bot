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
    USE_STRATEGY_V2,
    # Adaptive Filters Constants
    GLOBAL_MIN_ATR,
    GLOBAL_MAX_ATR,
    TAKER_REQ_LOWLIQ,
    TAKER_REQ_NORMAL,
    TAKER_REQ_HIGHLIQ,
    QUIET_MULT_LOWVOL,
    QUIET_MULT_NORMAL,
    QUIET_MULT_HIGHVOL,
    LOW_VOL_THRESHOLD,
    HIGH_VOL_THRESHOLD,
    ATR_RELATIVE_MIN,
    ATR_RELATIVE_MAX,
    # Functions
    load_env_with_fallback,
    get_liquidity_regime,
    is_time_banned,
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
    "USE_STRATEGY_V2",
    # Adaptive Filters
    "GLOBAL_MIN_ATR",
    "GLOBAL_MAX_ATR",
    "TAKER_REQ_LOWLIQ",
    "TAKER_REQ_NORMAL",
    "TAKER_REQ_HIGHLIQ",
    "QUIET_MULT_LOWVOL",
    "QUIET_MULT_NORMAL",
    "QUIET_MULT_HIGHVOL",
    "LOW_VOL_THRESHOLD",
    "HIGH_VOL_THRESHOLD",
    "ATR_RELATIVE_MIN",
    "ATR_RELATIVE_MAX",
    "load_env_with_fallback",
    "get_liquidity_regime",
    "is_time_banned",
]
