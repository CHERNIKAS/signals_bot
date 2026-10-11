#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Исследовательский режим: запись кандидатов по всем гейтам и расчёт их исходов."""

from research.recorder import ensure_schema, record_closed_bars, resolve_outcomes, simulate_outcome

__all__ = ["ensure_schema", "record_closed_bars", "resolve_outcomes", "simulate_outcome"]
