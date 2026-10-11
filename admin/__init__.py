#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Админ-панель"""

from admin.admin import (
    show_admin_panel,
    handle_admin_callback,
    show_admin_users,
    show_admin_user_profile,
    show_admin_add_pairs_catalog,
    handle_admin_add_pairs_save,
    show_admin_del_pairs_list,
    handle_admin_del_pairs_confirm,
    handle_admin_export_pairs,
    handle_admin_text_input,
    db_search_users,
    db_get_user_info,
)

__all__ = [
    "show_admin_panel",
    "handle_admin_callback",
    "show_admin_users",
    "show_admin_user_profile",
    "show_admin_add_pairs_catalog",
    "handle_admin_add_pairs_save",
    "show_admin_del_pairs_list",
    "handle_admin_del_pairs_confirm",
    "handle_admin_export_pairs",
    "handle_admin_text_input",
    "db_search_users",
    "db_get_user_info",
]

