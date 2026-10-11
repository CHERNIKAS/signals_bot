#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Панели управления сделками"""

from panels.panels import (
    ensure_panel_message,
    recreate_panel_message,
    edit_panel,
    panel_toggle_auto_close,
    cb_trades_router,
    _show_close_trade_menu,
    _show_close_size_selection,
    _show_close_confirmation,
    _execute_partial_close,
)

__all__ = [
    "ensure_panel_message",
    "recreate_panel_message",
    "edit_panel",
    "panel_toggle_auto_close",
    "cb_trades_router",
    "_show_close_trade_menu",
    "_show_close_size_selection",
    "_show_close_confirmation",
    "_execute_partial_close",
]
