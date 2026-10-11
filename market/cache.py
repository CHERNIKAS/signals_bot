#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Потокобезопасный TTL-кэш, общий для всех пользователей.

Ключевая идея: данные рынка одинаковы для всех юзеров, поэтому качаем их один
раз и держим до момента, когда они реально могут измениться (например, до
закрытия следующей свечи). Пер-ключевой лок защищает от "стампида": если 8
потоков одновременно просят BTCUSDT 1h, в Binance уйдёт один запрос.
"""

import threading
import time
from typing import Any, Callable, Dict, Tuple


class TTLCache:
    def __init__(self) -> None:
        self._data: Dict[Any, Tuple[float, Any]] = {}
        self._locks: Dict[Any, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: Any) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock

    def get(self, key: Any) -> Any:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        return value if expires_at > time.time() else None

    def get_or_load(self, key: Any, loader: Callable[[], Tuple[Any, float]]) -> Any:
        """loader() -> (value, expires_at_epoch_seconds)."""
        value = self.get(key)
        if value is not None:
            return value
        with self._lock_for(key):
            value = self.get(key)  # мог загрузить соседний поток, пока ждали лок
            if value is not None:
                return value
            value, expires_at = loader()
            self._data[key] = (expires_at, value)
            return value

    def clear(self) -> None:
        self._data.clear()


MARKET_CACHE = TTLCache()
