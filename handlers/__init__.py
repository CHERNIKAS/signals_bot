#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Telegram handlers"""

from handlers.handlers import (
    cmd_start,
    cmd_stop,
    cmd_status,
    cmd_pairs,
    cmd_add,
    cmd_rm,
    cmd_tfs,
    cmd_tf_add,
    cmd_tf_rm,
    cmd_tf_mode,
    handle_text,
    on_cb,
    register,
)

__all__ = [
    "cmd_start",
    "cmd_stop",
    "cmd_status",
    "cmd_pairs",
    "cmd_add",
    "cmd_rm",
    "cmd_tfs",
    "cmd_tf_add",
    "cmd_tf_rm",
    "cmd_tf_mode",
    "handle_text",
    "on_cb",
    "register",
]
