#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""База данных PostgreSQL: DDL, миграции и все db_* функции"""

import os
from typing import List, Dict, Any, Optional


# =============================================================================
# ОСНОВНЫЕ ФУНКЦИИ ПОДКЛЮЧЕНИЯ
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_enabled
# ОПИСАНИЕ: Проверяет, включена ли база данных (наличие DATABASE_URL)
#           Возвращает True если БД доступна
# ============================================================================
def db_enabled() -> bool:
    return bool(os.getenv("DATABASE_URL"))
# ============ КОНЕЦ ФУНКЦИИ: db_enabled ============


# ============================================================================
# ФУНКЦИЯ: _db_conn
# ОПИСАНИЕ: Создает и возвращает соединение с PostgreSQL базой данных
#           Использует параметры из DATABASE_URL и PGSSL
# ============================================================================
def _db_conn():
    import psycopg2
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set")
    return psycopg2.connect(dsn, sslmode=os.getenv("PGSSL", "require"))
# ============ КОНЕЦ ФУНКЦИИ: _db_conn ============


# =============================================================================
# DDL (СХЕМА БАЗЫ ДАННЫХ)
# =============================================================================

DDL = """
create table if not exists users(
  user_id bigint primary key,    -- telegram chat id
  username text,
  timezone text default 'UTC',
  created_at timestamptz default now()
);

create table if not exists user_settings (
  user_id      bigint primary key references users(user_id) on delete cascade,
  schedule_min int     not null default 1,
  send_debug   boolean not null default false,
  tf_mode      text    not null default 'stacked'
);

create table if not exists symbols(
  symbol text primary key
);

create table if not exists user_symbols(
  user_id bigint references users(user_id) on delete cascade,
  symbol  text references symbols(symbol) on delete cascade,
  is_active boolean not null default true,
  added_at timestamptz default now(),
  primary key(user_id, symbol)
);

create table if not exists user_timeframes(
  user_id bigint references users(user_id) on delete cascade,
  timeframe text,
  is_active boolean not null default true,
  primary key(user_id, timeframe)
);

create table if not exists signals_log(
  id bigserial primary key,
  user_id bigint references users(user_id) on delete set null,
  symbol text,
  timeframe text,
  side text,
  entry double precision,
  sl double precision,
  tp1 double precision,
  tp2 double precision,
  reason text,
  sent_at timestamptz default now()
);

-- дополнительные поля результата по сделке (добавляем ОТДЕЛЬНО)
alter table signals_log
  add column if not exists outcome text,               -- 'TP' | 'SL' | 'OPEN'
  add column if not exists outcome_price double precision,
  add column if not exists outcome_ts bigint;          -- ms

alter table signals_log
  add column if not exists bar_ts timestamptz;

-- Поля для фиксации событий TP1/TP2/SL (для статистики путей исходов)
alter table signals_log
  add column if not exists hit_tp1_price double precision,
  add column if not exists hit_tp1_ts bigint,      -- ms
  add column if not exists hit_tp2_price double precision,
  add column if not exists hit_tp2_ts bigint,      -- ms
  add column if not exists hit_sl_price double precision,
  add column if not exists hit_sl_ts bigint,       -- ms
  add column if not exists hit_tp_mid_price double precision,
  add column if not exists hit_tp_mid_ts bigint;    -- ms

-- Outcome v2: новый формат для статистики (jsonb)
-- {"status": "SL|TP1|TP1→SL|TP1+TP2|OPEN|CANCEL", "r": float, "time_to_tp1_min": int}
alter table signals_log
  add column if not exists outcome_v2 jsonb;

-- Категорийные поля для v2 стратегии (mode, trigger_type)
-- Эти поля добавляются отдельно в bootstrap_schema() для надёжности

-- BAN LOG: сохранение забаненных сигналов для статистики фильтров
create table if not exists signals_ban_log (
  id bigserial primary key,
  user_id bigint references users(user_id) on delete cascade,
  symbol text not null,
  timeframe text not null,
  side text not null,
  ban_ts timestamptz default now(),
  ban_reasons text[],        -- Array of ban reason strings
  log_json jsonb             -- Полный SignalLog.to_dict()
);

create index if not exists idx_ban_log_user_ts on signals_ban_log(user_id, ban_ts desc);

-- Index created later in DDL


create table if not exists user_levels(
  user_id    bigint references users(user_id) on delete cascade,
  symbol     text not null,
  timeframe  text not null,
  level      numeric not null,
  note       text,
  created_at timestamptz default now(),
  primary key(user_id, symbol, timeframe, level)
);

create index if not exists idx_user_symbols_active on user_symbols(user_id) where is_active;
create index if not exists idx_user_tfs_active     on user_timeframes(user_id) where is_active;

insert into symbols(symbol) values
('BTCUSDT'),('ETHUSDT'),('BNBUSDT'),('XRPUSDT'),('SOLUSDT'),
('DOGEUSDT'),('ADAUSDT'),('LINKUSDT'),('TONUSDT'),('DOTUSDT'),
('TRXUSDT'),('AVAXUSDT'),('NEARUSDT'),('ARBUSDT'),('OPUSDT')
on conflict do nothing;

-- running flag + сессии
ALTER TABLE user_settings
  ADD COLUMN IF NOT EXISTS is_running boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS user_sessions(
  session_id bigserial PRIMARY KEY,
  user_id    bigint REFERENCES users(user_id) ON DELETE CASCADE,
  started_at timestamptz DEFAULT now(),
  ended_at   timestamptz
);

-- bar_ts column already added above

-- Link to Telegram message id to reply in thread
ALTER TABLE signals_log
  ADD COLUMN IF NOT EXISTS tg_message_id bigint;

-- Пользовательские сделки на основании сигналов
create table if not exists user_trades (
  id            bigserial primary key,
  user_id       bigint not null references users(user_id) on delete cascade,
  signal_id     bigint not null references signals_log(id) on delete cascade,
  amount_usdt   numeric(18,6) not null,
  side          text not null,
  entry_price_user   numeric(18,8) not null,
  entry_price_signal numeric(18,8) not null,
  status        text not null default 'ACTIVE',
  close_mode    text,
  tp1           numeric(18,8),
  tp2           numeric(18,8),
  sl            numeric(18,8),
  timeframe     text not null,
  symbol        text not null,
  opened_at     timestamptz default now(),
  closed_at     timestamptz,
  outcome       text,
  exit_price_user numeric(18,8),
  pnl_abs       numeric(18,6),
  pnl_pct       numeric(9,4),
  -- Новые поля для частичного закрытия
  base_qty      numeric(18,10), -- полное количество на входе
  remaining_qty numeric(18,10), -- остаток количества
  fee_rate_bps  numeric(8,4) default 5, -- комиссия в базисных пунктах (0.05%)
  fee_open      numeric(18,8) default 0, -- комиссия на вход
  pnl_gross_total numeric(18,8) default 0, -- сумма по закрытым частям (грязный)
  fee_close_total numeric(18,8) default 0, -- сумма комиссий закрытия
  pnl_net_total numeric(18,8) default 0, -- чистый PnL
  -- Поля для режима входа (по количеству или по USDT)
  entry_mode text check (entry_mode in ('QTY','USDT')) default 'USDT',
  entry_request_value numeric, -- что ввёл юзер: qty или usdt
  entry_qty numeric(18,10), -- фактически рассчитанное количество лотов
  entry_notional_usdt numeric(18,8), -- фактическая стоимость позиции в USDT
  unique(user_id, signal_id)
);
-- Indexes will be created after columns are added

-- Частичные закрытия сделок
create table if not exists user_trade_fills (
  id            bigserial primary key,
  trade_id      bigint not null references user_trades(id) on delete cascade,
  type          text not null, -- 'TP1', 'TP2', 'SL', 'MANUAL'
  exit_price    numeric(18,8) not null,
  qty           numeric(18,10) not null, -- закрытое количество
  amount_usdt   numeric(18,8) not null, -- qty * entry_price_user
  pnl_gross     numeric(18,8) not null, -- грязный PnL
  fee_close     numeric(18,8) not null default 0, -- комиссия закрытия
  pnl_net       numeric(18,8) not null, -- чистый PnL
  created_at    timestamptz default now()
);

-- User preferences for auto-close
create table if not exists user_prefs (
  user_id bigint primary key references users(user_id) on delete cascade,
  auto_close_by_signal boolean default false,
  entry_mode text check (entry_mode in ('QTY','USDT')) default 'USDT',
  entry_value numeric,
  daily_digest_enabled boolean default true,
  created_at timestamptz default now()
);

-- Daily digest tracking
create table if not exists daily_digest (
  id bigserial primary key,
  user_id bigint not null references users(user_id) on delete cascade,
  digest_date date not null,
  sent_at timestamptz default now(),
  message_id bigint,
  unique(user_id, digest_date)
);

-- НОВОЕ: раздельные ТФ
create table if not exists user_tfs_analysis(
  user_id   bigint references users(user_id) on delete cascade,
  timeframe text,
  is_active boolean not null default true,
  primary key(user_id, timeframe)
);

create table if not exists user_tfs_signal(
  user_id   bigint references users(user_id) on delete cascade,
  timeframe text,
  is_active boolean not null default true,
  primary key(user_id, timeframe)
);

-- Миграция: если раньше был единый список ТФ (user_timeframes) — скопировать в оба набора
do $$
begin
  if exists (select 1 from information_schema.tables where table_name='user_timeframes') then
    insert into user_tfs_analysis(user_id,timeframe,is_active)
    select user_id,timeframe,is_active from user_timeframes
    on conflict (user_id,timeframe) do nothing;

    insert into user_tfs_signal(user_id,timeframe,is_active)
    select user_id,timeframe,is_active from user_timeframes
    on conflict (user_id,timeframe) do nothing;
  end if;
end $$;

-- Настройки уведомлений
CREATE TABLE IF NOT EXISTS user_notifications (
  user_id bigint PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  stats_notifications boolean DEFAULT false,
  drawdown_alerts boolean DEFAULT false,
  weekly_reports boolean DEFAULT false,
  created_at timestamptz DEFAULT now()
);

-- История статистики для сравнения
CREATE TABLE IF NOT EXISTS stats_history (
  id bigserial PRIMARY KEY,
  user_id bigint REFERENCES users(user_id) ON DELETE CASCADE,
  period_type text NOT NULL, -- 'day', 'week', 'month'
  period_start timestamptz NOT NULL,
  winrate numeric,
  total_signals int,
  tp_count int,
  sl_count int,
  created_at timestamptz DEFAULT now()
);

-- Create unique index for signals_log after all columns are created
CREATE UNIQUE INDEX IF NOT EXISTS uniq_signal_per_bar
  ON signals_log(user_id, symbol, timeframe, bar_ts);

-- Миграция для частичного закрытия: обновить существующие записи
DO $$
BEGIN
  -- Добавить новые колонки если их нет
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='user_trades' AND column_name='base_qty') THEN
    ALTER TABLE user_trades ADD COLUMN base_qty numeric(18,10);
    ALTER TABLE user_trades ADD COLUMN remaining_qty numeric(18,10);
    ALTER TABLE user_trades ADD COLUMN fee_rate_bps numeric(8,4) DEFAULT 5;
    ALTER TABLE user_trades ADD COLUMN fee_open numeric(18,8) DEFAULT 0;
    ALTER TABLE user_trades ADD COLUMN pnl_gross_total numeric(18,8) DEFAULT 0;
    ALTER TABLE user_trades ADD COLUMN fee_close_total numeric(18,8) DEFAULT 0;
    ALTER TABLE user_trades ADD COLUMN pnl_net_total numeric(18,8) DEFAULT 0;
  END IF;
  
  -- Обновить существующие записи
  UPDATE user_trades 
  SET base_qty = amount_usdt / entry_price_user,
      remaining_qty = amount_usdt / entry_price_user,
      fee_open = amount_usdt * 5 / 10000  -- 0.05% комиссия
  WHERE base_qty IS NULL;
END $$;
"""


# ============================================================================
# ФУНКЦИЯ: bootstrap_schema
# ОПИСАНИЕ: Инициализирует и обновляет схему базы данных PostgreSQL
#           Создает все необходимые таблицы, индексы и выполняет миграции
# ============================================================================
def bootstrap_schema():
    import psycopg2
    from psycopg2 import OperationalError, InterfaceError
    
    print("[MIGRATION] Starting database schema bootstrap...")
    
    try:
        with _db_conn() as c:
            # DDL/migrations can include optional steps that may fail on old/new schemas.
            # Autocommit prevents one failed step from rolling back the whole bootstrap.
            c.autocommit = True
            cur = c.cursor()
            # Execute DDL as a single transaction
            print("[MIGRATION] Executing main DDL...")
            try:
                cur.execute(DDL)
                print("[MIGRATION] ✅ Main DDL executed successfully")
                c.commit()
                print("[MIGRATION] ✅ Main DDL committed")
            except Exception as e:
                print(f"❌ Main DDL failed: {e}")
                # Rollback and try individual parts
                c.rollback()
                print("[MIGRATION] Rolling back and trying individual parts...")
                
                # Execute DDL in parts, but handle multi-line blocks properly
                ddl_parts = []
                current_part = ""
                in_dollar_quote = False
                dollar_tag = ""
                
                for line in DDL.split('\n'):
                    line = line.strip()
                    if not line or line.startswith('--'):
                        continue
                        
                    # Handle dollar-quoted strings
                    if '$$' in line:
                        if not in_dollar_quote:
                            # Start of dollar quote
                            dollar_start = line.find('$$')
                            dollar_tag = line[dollar_start:dollar_start+2]
                            in_dollar_quote = True
                            current_part += line + '\n'
                        else:
                            # End of dollar quote
                            current_part += line + '\n'
                            if dollar_tag in line:
                                in_dollar_quote = False
                                if current_part.strip():
                                    ddl_parts.append(current_part.strip())
                                current_part = ""
                        continue
                    
                    if in_dollar_quote:
                        current_part += line + '\n'
                        continue
                    
                    # Regular SQL
                    current_part += line + '\n'
                    if line.endswith(';'):
                        if current_part.strip():
                            ddl_parts.append(current_part.strip())
                        current_part = ""
            
                # Execute parts
                for i, part in enumerate(ddl_parts):
                    if not part:
                        continue
                    try:
                        print(f"[MIGRATION] Executing part {i+1}/{len(ddl_parts)}")
                        cur.execute(part)
                    except Exception as e:
                        print(f"Warning: DDL part {i+1} failed: {e}")
                        print(f"Part: {part[:100]}...")
                c.commit()
                print("[MIGRATION] ✅ Partial DDL committed")
            
            # Добавляем поле timezone если его нет
            try:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone text DEFAULT 'UTC'")
                print("[MIGRATION] ✅ timezone column ensured")
            except Exception as e:
                print(f"Warning: Could not add timezone column: {e}")
            
            # Миграция для user_trades - добавляем недостающие колонки
            # Всегда выполняем (IF NOT EXISTS безопасен)
            print("[MIGRATION] Adding user_trades columns...")
            try:
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS entry_price_user numeric(18,8)")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS entry_price_signal numeric(18,8)")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS status text DEFAULT 'ACTIVE'")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS close_mode text")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS tp1 numeric(18,8)")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS tp2 numeric(18,8)")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS sl numeric(18,8)")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS timeframe text")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS symbol text")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS opened_at timestamptz DEFAULT now()")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS closed_at timestamptz")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS outcome text")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS exit_price_user numeric(18,8)")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS pnl_abs numeric(18,6)")
                cur.execute("ALTER TABLE user_trades ADD COLUMN IF NOT EXISTS pnl_pct numeric(9,4)")
                print("✅ user_trades migration completed")
            except Exception as e:
                print(f"❌ user_trades migration failed (table may not exist yet): {e}")
                try:
                    c.rollback()
                except Exception:
                    pass
            
            # Миграция для signals_log - добавляем tg_message_id и поля для событий TP1/TP2/SL
            # Всегда выполняем (IF NOT EXISTS безопасен)
            print("[MIGRATION] Adding signals_log columns...")
            try:
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS tg_message_id bigint")
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS hit_tp1_price double precision")
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS hit_tp1_ts bigint")
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS hit_tp2_price double precision")
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS hit_tp2_ts bigint")
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS hit_sl_price double precision")
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS hit_sl_ts bigint")
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS hit_tp_mid_price double precision")
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS hit_tp_mid_ts bigint")
                # Добавляем mode и trigger_type для v2 стратегии
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS mode text")
                cur.execute("ALTER TABLE signals_log ADD COLUMN IF NOT EXISTS trigger_type text")
                print("✅ signals_log migration completed")
            except Exception as e:
                print(f"❌ signals_log migration failed (table may not exist yet): {e}")
                try:
                    c.rollback()
                except Exception:
                    pass
            
            # Создаем таблицу user_prefs если её нет
            print("[MIGRATION] Creating user_prefs table...")
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_prefs (
                        user_id bigint PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                        auto_close_by_signal boolean DEFAULT false,
                        created_at timestamptz DEFAULT now(),
                        panel_message_id bigint
                    )
                """)
                print("✅ user_prefs table ensured")
            except Exception as e:
                print(f"❌ user_prefs creation failed: {e}")
            # ensure column exists for existing DBs
            try:
                cur.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS panel_message_id bigint")
            except Exception:
                pass
            
            # Создаем индексы для user_trades
            # Всегда выполняем (IF NOT EXISTS безопасен)
            print("[MIGRATION] Creating user_trades indexes...")
            try:
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_trades_user_period ON user_trades(user_id, opened_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_trades_user_status ON user_trades(user_id, status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_trades_user_symbol_status ON user_trades(user_id, symbol, status)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_trades_opened_at ON user_trades(opened_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_trades_closed_at ON user_trades(closed_at)")
                print("✅ user_trades indexes ensured")
            except Exception as e:
                print(f"❌ user_trades indexes creation failed (table may not exist yet): {e}")
                try:
                    c.rollback()
                except Exception:
                    pass

            # Ensure partial close columns (idempotent)
            try:
                cur.execute("""
                    ALTER TABLE user_trades
                      ADD COLUMN IF NOT EXISTS base_qty numeric(18,10),
                      ADD COLUMN IF NOT EXISTS remaining_qty numeric(18,10),
                      ADD COLUMN IF NOT EXISTS fee_rate_bps numeric(8,4) DEFAULT 5,
                      ADD COLUMN IF NOT EXISTS fee_open numeric(18,8) DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS pnl_gross_total numeric(18,8) DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS fee_close_total numeric(18,8) DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS pnl_net_total numeric(18,8) DEFAULT 0;
                """)
                # Backfill values for existing trades
                cur.execute("""
                    UPDATE user_trades
                       SET base_qty = COALESCE(base_qty, amount_usdt / NULLIF(entry_price_user,0)),
                           remaining_qty = COALESCE(remaining_qty, amount_usdt / NULLIF(entry_price_user,0)),
                           fee_open = COALESCE(fee_open, amount_usdt * COALESCE(fee_rate_bps,5) / 10000.0)
                     WHERE base_qty IS NULL OR remaining_qty IS NULL OR fee_open IS NULL;
                """)
                # Ensure fills table exists
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_trade_fills (
                      id            bigserial primary key,
                      trade_id      bigint not null references user_trades(id) on delete cascade,
                      type          text not null,
                      exit_price    numeric(18,8) not null,
                      qty           numeric(18,10) not null,
                      amount_usdt   numeric(18,8) not null,
                      pnl_gross     numeric(18,8) not null,
                      fee_close     numeric(18,8) not null default 0,
                      pnl_net       numeric(18,8) not null,
                      created_at    timestamptz default now()
                    );
                """)
                print("✅ partial close columns ensured")
            except Exception as e:
                print(f"❌ ensuring partial close columns failed: {e}")

            # ---- NEW: levels schema ----
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_levels (
                        id bigserial PRIMARY KEY,
                        user_id bigint NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                        symbol text NOT NULL,
                        timeframe text NOT NULL,
                        type text NOT NULL,
                        price_low numeric(18,8) NOT NULL,
                        price_high numeric(18,8),
                        strength int NOT NULL DEFAULT 1,
                        note text,
                        created_at timestamptz DEFAULT now(),
                        updated_at timestamptz DEFAULT now()
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_levels_user_pair_tf ON user_levels(user_id, symbol, timeframe)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_levels_price ON user_levels(user_id, symbol, timeframe, price_low)")
                print("✅ user_levels table ensured")
            except Exception as e:
                print(f"❌ user_levels ensure failed: {e}")
                # Rollback и продолжаем - таблица может уже существовать
                try:
                    c.rollback()
                except Exception:
                    pass

            # ---- MIGRATION: user_levels columns ----
            # Всегда выполняем миграцию (IF NOT EXISTS безопасен)
            print("[MIGRATION] Adding user_levels columns...")
            try:
                cur.execute("ALTER TABLE user_levels ADD COLUMN IF NOT EXISTS price_low numeric(18,8)")
                cur.execute("ALTER TABLE user_levels ADD COLUMN IF NOT EXISTS price_high numeric(18,8)")
                cur.execute("ALTER TABLE user_levels ADD COLUMN IF NOT EXISTS strength int DEFAULT 1")
                cur.execute("ALTER TABLE user_levels ADD COLUMN IF NOT EXISTS note text")
                cur.execute("ALTER TABLE user_levels ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now()")
                cur.execute("ALTER TABLE user_levels ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now()")
                print("✅ user_levels migration completed")
            except Exception as e:
                print(f"❌ user_levels migration failed: {e}")
                try:
                    c.rollback()
                except Exception:
                    pass

            try:
                cur.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS levels_config jsonb")
                print("✅ user_prefs.levels_config ensured")
            except Exception as e:
                print(f"❌ levels_config add failed: {e}")
                try:
                    c.rollback()
                except Exception:
                    pass
                # try again in a fresh transaction
                try:
                    cur.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS levels_config jsonb")
                    print("✅ user_prefs.levels_config ensured after rollback")
                except Exception as e2:
                    print(f"❌ levels_config add retry failed: {e2}")
            # Ensure TPmid config column
            try:
                cur.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS tp_mid_config jsonb")
                print("✅ user_prefs.tp_mid_config ensured")
            except Exception as e:
                print(f"❌ tp_mid_config add failed: {e}")
                try:
                    c.rollback()
                except Exception:
                    pass
                try:
                    cur.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS tp_mid_config jsonb")
                    print("✅ user_prefs.tp_mid_config ensured after rollback")
                except Exception as e2:
                    print(f"❌ tp_mid_config add retry failed: {e2}")
            
            # Ensure entry mode columns (for order entry by qty or USDT)
            try:
                cur.execute("""
                    ALTER TABLE user_prefs
                      ADD COLUMN IF NOT EXISTS entry_mode text CHECK (entry_mode IN ('QTY','USDT')) DEFAULT 'USDT',
                      ADD COLUMN IF NOT EXISTS entry_value numeric,
                      ADD COLUMN IF NOT EXISTS daily_digest_enabled boolean DEFAULT true;
                """)
                cur.execute("""
                    ALTER TABLE user_trades
                      ADD COLUMN IF NOT EXISTS entry_mode text CHECK (entry_mode IN ('QTY','USDT')) DEFAULT 'USDT',
                      ADD COLUMN IF NOT EXISTS entry_request_value numeric,
                      ADD COLUMN IF NOT EXISTS entry_qty numeric(18,10),
                      ADD COLUMN IF NOT EXISTS entry_notional_usdt numeric(18,8);
                """)
                # Backfill entry_qty and entry_notional_usdt for existing trades
                cur.execute("""
                    UPDATE user_trades
                       SET entry_qty = COALESCE(entry_qty, base_qty),
                           entry_notional_usdt = COALESCE(entry_notional_usdt, amount_usdt),
                           entry_request_value = COALESCE(entry_request_value, amount_usdt)
                     WHERE entry_qty IS NULL OR entry_notional_usdt IS NULL OR entry_request_value IS NULL;
                """)
                print("✅ entry mode columns ensured")
            except Exception as e:
                print(f"❌ entry mode columns failed: {e}")
            
            # Ensure daily_digest table
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS daily_digest (
                        id bigserial PRIMARY KEY,
                        user_id bigint NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                        digest_date date NOT NULL,
                        sent_at timestamptz DEFAULT now(),
                        message_id bigint,
                        UNIQUE(user_id, digest_date)
                    )
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_daily_digest_user_date ON daily_digest(user_id, digest_date)")
                print("✅ daily_digest table ensured")
            except Exception as e:
                print(f"❌ daily_digest table failed: {e}")
            
            # Создаем таблицы user_tfs_analysis и user_tfs_signal если их нет
            print("[MIGRATION] Creating user_tfs tables...")
            try:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_tfs_analysis(
                        user_id bigint REFERENCES users(user_id) ON DELETE CASCADE,
                        timeframe text,
                        is_active boolean NOT NULL DEFAULT true,
                        PRIMARY KEY(user_id, timeframe)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_tfs_signal(
                        user_id bigint REFERENCES users(user_id) ON DELETE CASCADE,
                        timeframe text,
                        is_active boolean NOT NULL DEFAULT true,
                        PRIMARY KEY(user_id, timeframe)
                    )
                """)
                print("✅ user_tfs tables ensured")
            except Exception as e:
                print(f"❌ user_tfs tables creation failed: {e}")
            
            print("[MIGRATION] Committing changes...")
            c.commit()
            print("[MIGRATION] ✅ Database schema bootstrap completed successfully!")
    except (OperationalError, InterfaceError, psycopg2.OperationalError) as e:
        print(f"⚠️ [MIGRATION] Database connection failed: {e}")
        print("⚠️ [MIGRATION] Application will continue without database initialization.")
        print("⚠️ [MIGRATION] Database features will be unavailable until connection is restored.")
    except Exception as e:
        print(f"❌ [MIGRATION] Unexpected error during schema bootstrap: {e}")
        import traceback
        traceback.print_exc()
        print("⚠️ [MIGRATION] Application will continue, but database may be in inconsistent state.")
# ============ КОНЕЦ ФУНКЦИИ: bootstrap_schema ============


# =============================================================================
# USER MANAGEMENT
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_get_or_create_user
# ОПИСАНИЕ: Получает пользователя из БД или создает нового если не существует
#           Возвращает информацию о пользователе
# ============================================================================
def db_get_or_create_user(uid: int, username: str | None):
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # 1) создать/обновить users
        cur.execute(
            "insert into users(user_id, username) values(%s,%s) "
            "on conflict (user_id) do update set username=excluded.username "
            "returning user_id",
            (uid, username)
        )
        cur.fetchone()

        # 2) создать user_settings, если нет
        cur.execute("insert into user_settings(user_id) values(%s) on conflict do nothing", (uid,))

        # 3) ДОБАВЛЕНИЕ ПАР ПО УМОЛЧАНИЮ
        cur.executemany(
            "insert into user_symbols(user_id, symbol) values (%s, %s) on conflict do nothing",
            [(uid, "BTCUSDT"), (uid, "ETHUSDT")]
        )

        # 4) ТАЙМФРЕЙМЫ (analysis/signal)
        for tf in ("1h", "4h", "1d"):
            cur.execute(
                "insert into user_tfs_analysis(user_id,timeframe) values(%s,%s) on conflict do nothing",
                (uid, tf)
            )
            cur.execute(
                "insert into user_tfs_signal(user_id,timeframe) values(%s,%s) on conflict do nothing",
                (uid, tf)
            )

        # 5) закоммитить
        c.commit()
# ============ КОНЕЦ ФУНКЦИИ: db_get_or_create_user ============


# ============================================================================
# ФУНКЦИЯ: db_get_user_timezone
# ОПИСАНИЕ: Получить часовой пояс пользователя
# ============================================================================
def db_get_user_timezone(uid: int) -> str:
    """Получить часовой пояс пользователя (по умолчанию 'UTC')"""
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("SELECT timezone FROM users WHERE user_id = %s", (uid,))
        row = cur.fetchone()
        return row[0] if row and row[0] else "UTC"
# ============ КОНЕЦ ФУНКЦИИ: db_get_user_timezone ============


def db_set_user_timezone(uid: int, tz: str):
    """Установить часовой пояс пользователя"""
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("""
            UPDATE users 
            SET timezone = %s 
            WHERE user_id = %s
        """, (tz, uid))
        c.commit()
# ============ КОНЕЦ ФУНКЦИИ: db_set_user_timezone ============


# ============================================================================
# ФУНКЦИЯ: db_user_exists
# ОПИСАНИЕ: Проверяет существование пользователя в базе данных
#           Возвращает True если пользователь найден
# ============================================================================
def db_user_exists(uid:int)->bool:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("select 1 from users where user_id=%s",(uid,))
        return bool(cur.fetchone())
# ============ КОНЕЦ ФУНКЦИИ: db_user_exists ============


# ============================================================================
# ФУНКЦИЯ: db_list_users
# ОПИСАНИЕ: Возвращает список ID всех пользователей в системе
#           Используется для рассылок и администрирования
# ============================================================================
def db_list_users()->List[int]:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("select user_id from users")
        return [r[0] for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_list_users ============


# =============================================================================
# PAIRS MANAGEMENT
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_user_pairs
# ОПИСАНИЕ: Получает список активных торговых пар пользователя
#           Возвращает массив символов (BTCUSDT, ETHUSDT и т.д.)
# ============================================================================
def db_user_pairs(uid:int)->List[str]:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("select symbol from user_symbols where user_id=%s and is_active", (uid,))
        return [r[0] for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_user_pairs ============


# ============================================================================
# ФУНКЦИЯ: db_add_pair
# ОПИСАНИЕ: Добавляет торговую пару пользователю
#           Создает символ в справочнике если не существует и активирует для пользователя
# ============================================================================
def db_add_pair(uid: int, symbol: str) -> None:
    sym = (symbol or "").upper().strip()
    with _db_conn() as c, c.cursor() as cur:
        # 1) гарантия, что тикер есть в справочнике (для FK)
        cur.execute(
            "INSERT INTO symbols(symbol) VALUES (%s) ON CONFLICT DO NOTHING",
            (sym,)
        )
        # 2) включаем пару пользователю
        cur.execute(
            """
            INSERT INTO user_symbols(user_id, symbol, is_active)
            VALUES (%s, %s, TRUE)
            ON CONFLICT (user_id, symbol) DO UPDATE SET is_active = TRUE
            """,
            (uid, sym)
        )
        c.commit()
# ============ КОНЕЦ ФУНКЦИИ: db_add_pair ============


# ============================================================================
# ФУНКЦИЯ: db_rm_pair
# ОПИСАНИЕ: Удаляет (деактивирует) торговую пару у пользователя
#           Возвращает True если пара была найдена и удалена
# ============================================================================
def db_rm_pair(uid:int, symbol:str)->bool:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("update user_symbols set is_active=false where user_id=%s and symbol=%s",(uid,symbol))
        c.commit(); return cur.rowcount>0
# ============ КОНЕЦ ФУНКЦИИ: db_rm_pair ============

    
# ============================================================================
# ФУНКЦИЯ: db_toggle_pair
# ОПИСАНИЕ: Переключает состояние торговой пары (вкл/выкл)
#           Если пары нет - добавляет и включает, если есть - меняет состояние
# ============================================================================
def db_toggle_pair(uid: int, symbol: str) -> bool:
    sym = (symbol or "").upper().strip()
    with _db_conn() as c, c.cursor() as cur:
        # есть ли запись?
        cur.execute("select is_active from user_symbols where user_id=%s and symbol=%s", (uid, sym))
        row = cur.fetchone()
        if row is None:
            # гарантируем символ в справочнике (FK)
            cur.execute("insert into symbols(symbol) values(%s) on conflict do nothing", (sym,))
            # включаем
            cur.execute(
                "insert into user_symbols(user_id, symbol, is_active) values (%s,%s,true) "
                "on conflict (user_id, symbol) do update set is_active=excluded.is_active",
                (uid, sym)
            )
            c.commit()
            return True
        else:
            new_state = not bool(row[0])
            cur.execute("update user_symbols set is_active=%s where user_id=%s and symbol=%s",
                        (new_state, uid, sym))
            c.commit()
            return new_state
# ============ КОНЕЦ ФУНКЦИИ: db_toggle_pair ============    


# =============================================================================
# TIMEFRAMES MANAGEMENT (старый формат)
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_user_tfs
# ОПИСАНИЕ: Получает список активных таймфреймов пользователя
#           Используется в старом формате хранения ТФ
# ============================================================================
def db_user_tfs(uid:int)->List[str]:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("select timeframe from user_timeframes where user_id=%s and is_active",(uid,))
        return [r[0] for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_user_tfs ============


# ============================================================================
# ФУНКЦИЯ: db_add_tf
# ОПИСАНИЕ: Добавляет таймфрейм пользователю
#           Если уже существует - активирует его
# ============================================================================
def db_add_tf(uid:int, tf:str)->None:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("insert into user_timeframes(user_id,timeframe,is_active) values(%s,%s,true) "
                    "on conflict (user_id,timeframe) do update set is_active=true", (uid,tf))
        c.commit()
# ============ КОНЕЦ ФУНКЦИИ: db_add_tf ============


# ============================================================================
# ФУНКЦИЯ: db_rm_tf
# ОПИСАНИЕ: Удаляет (деактивирует) таймфрейм у пользователя
#           Возвращает True если таймфрейм был найден
# ============================================================================
def db_rm_tf(uid:int, tf:str)->bool:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("update user_timeframes set is_active=false where user_id=%s and timeframe=%s",(uid,tf))
        c.commit(); return cur.rowcount>0
# ============ КОНЕЦ ФУНКЦИИ: db_rm_tf ============


# ============================================================================
# ФУНКЦИЯ: db_toggle_tf
# ОПИСАНИЕ: Переключает состояние таймфрейма (вкл/выкл)
#           Если не существует - создает и включает, если есть - меняет состояние
# ============================================================================
def db_toggle_tf(uid: int, tf: str) -> bool:
    timeframe = str(tf).strip()
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("select is_active from user_timeframes where user_id=%s and timeframe=%s",
                    (uid, timeframe))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "insert into user_timeframes(user_id, timeframe, is_active) values (%s,%s,true) "
                "on conflict (user_id, timeframe) do update set is_active=excluded.is_active",
                (uid, timeframe)
            )
            c.commit()
            return True
        else:
            new_state = not bool(row[0])
            cur.execute("update user_timeframes set is_active=%s where user_id=%s and timeframe=%s",
                        (new_state, uid, timeframe))
            c.commit()
            return new_state
# ============ КОНЕЦ ФУНКЦИИ: db_toggle_tf ============


# =============================================================================
# TIMEFRAMES MANAGEMENT (новый формат: analysis & signal)
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_user_tfs_analysis
# ОПИСАНИЕ: Получает список активных таймфреймов для анализа рынка
#           Эти ТФ используются для расчета контекста и фильтрации сигналов
# ============================================================================
def db_user_tfs_analysis(uid: int) -> list[str]:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("select timeframe from user_tfs_analysis where user_id=%s and is_active", (uid,))
        return [r[0] for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_user_tfs_analysis ============


# ============================================================================
# ФУНКЦИЯ: db_user_tfs_signal
# ОПИСАНИЕ: Получает список активных таймфреймов для генерации сигналов
#           С этих ТФ реально отправляются торговые сигналы
# ============================================================================
def db_user_tfs_signal(uid: int) -> list[str]:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("select timeframe from user_tfs_signal where user_id=%s and is_active", (uid,))
        return [r[0] for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_user_tfs_signal ============


# ============================================================================
# ФУНКЦИЯ: _db_toggle_tf
# ОПИСАНИЕ: Внутренняя функция переключения таймфрейма в указанной таблице
#           Универсальная функция для работы с обеими таблицами ТФ
# ============================================================================
def _db_toggle_tf(table: str, uid: int, tf: str) -> bool:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute(f"select is_active from {table} where user_id=%s and timeframe=%s", (uid, tf))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                f"insert into {table}(user_id, timeframe, is_active) values (%s,%s,true) "
                f"on conflict (user_id, timeframe) do update set is_active=excluded.is_active",
                (uid, tf)
            )
            c.commit()
            return True
        new_state = not bool(row[0])
        cur.execute(f"update {table} set is_active=%s where user_id=%s and timeframe=%s",
                    (new_state, uid, tf))
        c.commit()
        return new_state
# ============ КОНЕЦ ФУНКЦИИ: _db_toggle_tf ============


# ============================================================================
# ФУНКЦИЯ: db_toggle_tf_analysis
# ОПИСАНИЕ: Переключает таймфрейм в списке анализа
#           Включает/выключает ТФ для расчета контекста
# ============================================================================
def db_toggle_tf_analysis(uid: int, tf: str) -> bool:
    return _db_toggle_tf("user_tfs_analysis", uid, tf)
# ============ КОНЕЦ ФУНКЦИИ: db_toggle_tf_analysis ============


# ============================================================================
# ФУНКЦИЯ: db_toggle_tf_signal
# ОПИСАНИЕ: Переключает таймфрейм в списке сигналов
#           Включает/выключает ТФ для генерации торговых сигналов
# ============================================================================
def db_toggle_tf_signal(uid: int, tf: str) -> bool:
    return _db_toggle_tf("user_tfs_signal", uid, tf)
# ============ КОНЕЦ ФУНКЦИИ: db_toggle_tf_signal ============


# Helper functions для добавления/удаления ТФ
def db_add_tf_analysis(uid: int, tf: str) -> None:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("insert into user_tfs_analysis(user_id,timeframe,is_active) values(%s,%s,true) "
                    "on conflict (user_id,timeframe) do update set is_active=true", (uid,tf))
        c.commit()

def db_rm_tf_analysis(uid: int, tf: str) -> bool:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("update user_tfs_analysis set is_active=false where user_id=%s and timeframe=%s",(uid,tf))
        c.commit(); return cur.rowcount>0

def db_add_tf_signal(uid: int, tf: str) -> None:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("insert into user_tfs_signal(user_id,timeframe,is_active) values(%s,%s,true) "
                    "on conflict (user_id,timeframe) do update set is_active=true", (uid,tf))
        c.commit()

def db_rm_tf_signal(uid: int, tf: str) -> bool:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("update user_tfs_signal set is_active=false where user_id=%s and timeframe=%s",(uid,tf))
        c.commit(); return cur.rowcount>0


# =============================================================================
# SETTINGS MANAGEMENT
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_get_settings
# ОПИСАНИЕ: Получает настройки пользователя из БД
#           Возвращает расписание, режим отладки и режим таймфреймов
# ============================================================================
def db_get_settings(uid:int)->Dict[str,Any]:
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("select schedule_min, send_debug, tf_mode from user_settings where user_id=%s",(uid,))
        row = cur.fetchone()
        return dict(row) if row else {"schedule_min":1,"send_debug":False,"tf_mode":"stacked"}
# ============ КОНЕЦ ФУНКЦИИ: db_get_settings ============

    
# ============================================================================
# ФУНКЦИЯ: db_set_running
# ОПИСАНИЕ: Устанавливает флаг активности бота для пользователя
#           Определяет, запущен ли бот для генерации сигналов
# ============================================================================
def db_set_running(uid: int, running: bool) -> None:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("update user_settings set is_running=%s where user_id=%s", (running, uid))
        c.commit()
# ============ КОНЕЦ ФУНКЦИИ: db_set_running ============


# ============================================================================
# ФУНКЦИЯ: db_is_running
# ОПИСАНИЕ: Проверяет, запущен ли бот для пользователя
#           Возвращает True если бот активен
# ============================================================================
def db_is_running(uid: int) -> bool:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("select is_running from user_settings where user_id=%s", (uid,))
        row = cur.fetchone()
        return bool(row[0]) if row else False
# ============ КОНЕЦ ФУНКЦИИ: db_is_running ============


# ============================================================================
# ФУНКЦИЯ: db_set_tf_mode
# ОПИСАНИЕ: Устанавливает режим работы с таймфреймами (stacked/any)
#           stacked = консенсус ТФ, any = достаточно одного
# ============================================================================
def db_set_tf_mode(uid:int, mode:str)->None:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("update user_settings set tf_mode=%s where user_id=%s",(mode,uid))
        c.commit()
# ============ КОНЕЦ ФУНКЦИИ: db_set_tf_mode ============


# =============================================================================
# SESSIONS
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_session_is_open
# ОПИСАНИЕ: Проверяет, открыта ли торговая сессия пользователя
#           Используется для отслеживания периодов активной торговли
# ============================================================================
def db_session_is_open(uid: int) -> bool:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("select 1 from user_sessions where user_id=%s and ended_at is null", (uid,))
        result = bool(cur.fetchone())
        print(f"[SESSION] db_session_is_open({uid}) = {result}")
        return result
# ============ КОНЕЦ ФУНКЦИИ: db_session_is_open ============


# ============================================================================
# ФУНКЦИЯ: db_session_start
# ОПИСАНИЕ: Начинает новую торговую сессию для пользователя
#           Закрывает все открытые сессии и создает новую
# ============================================================================
def db_session_start(uid: int) -> int | None:
    print(f"[SESSION] db_session_start({uid}) called")
    with _db_conn() as c, c.cursor() as cur:
        # Сначала закроем все открытые сессии для этого пользователя
        cur.execute("update user_sessions set ended_at=now() where user_id=%s and ended_at is null", (uid,))
        print(f"[SESSION] Closed existing sessions for user {uid}")
        
        # Проверяем, что нет открытых сессий
        cur.execute("select session_id from user_sessions where user_id=%s and ended_at is null", (uid,))
        existing = cur.fetchone()
        if existing:
            print(f"[SESSION] Still found open session after close: {existing[0]}")
            return existing[0]
        
        # Создаем новую сессию с обработкой конфликта
        try:
            # Пытаемся вставить новую запись
            cur.execute("insert into user_sessions(user_id) values(%s) returning session_id", (uid,))
            sid = cur.fetchone()[0]
            c.commit()
            print(f"[SESSION] Created new session: {sid}")
            return sid
        except Exception as e:
            print(f"[SESSION] Error creating session: {e}")
            c.rollback()
            
            # Если есть конфликт, попробуем обновить существующую запись
            try:
                cur.execute("""
                    update user_sessions 
                    set ended_at = null, started_at = now() 
                    where user_id = %s 
                    returning session_id
                """, (uid,))
                row = cur.fetchone()
                if row:
                    c.commit()
                    print(f"[SESSION] Reactivated existing session: {row[0]}")
                    return row[0]
            except Exception as e2:
                print(f"[SESSION] Error reactivating session: {e2}")
                c.rollback()
            
            return None
# ============ КОНЕЦ ФУНКЦИИ: db_session_start ============


# ============================================================================
# ФУНКЦИЯ: db_session_end
# ОПИСАНИЕ: Завершает текущую торговую сессию пользователя
#           Устанавливает время окончания для открытой сессии
# ============================================================================
def db_session_end(uid: int) -> int | None:
    print(f"[SESSION] db_session_end({uid}) called")
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("update user_sessions set ended_at=now() where user_id=%s and ended_at is null returning session_id", (uid,))
        row = cur.fetchone()
        if row:
            c.commit()
            print(f"[SESSION] Closed session: {row[0]}")
            return row[0]
        print(f"[SESSION] No open session found to close")
        return None
# ============ КОНЕЦ ФУНКЦИИ: db_session_end ============


# =============================================================================
# SIGNALS
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_log_signal
# ОПИСАНИЕ: Сохраняет торговый сигнал в базу данных
#           Записывает все параметры сигнала: символ, ТФ, направление, цены
# ============================================================================
def db_log_signal(uid:int, symbol:str, tf:str, bar_ts:int,
                  side:str, entry, sl, tp1, tp2, reason:str,
                  mode: str = None, trigger_type: str = None):
    # bar_ts теперь в миллисекундах
    # Возвращает ID только для НОВЫХ сигналов, None для дубликатов
    print(f"[DB_LOG] bar_ts={bar_ts} (ms), symbol={symbol}, tf={tf}, mode={mode}, trigger={trigger_type}")
    with _db_conn() as c, c.cursor() as cur:
        # Сначала проверяем, существует ли уже этот сигнал
        cur.execute("""
            SELECT id FROM signals_log
            WHERE user_id = %s 
              AND symbol = %s 
              AND timeframe = %s 
              AND bar_ts = to_timestamp(%s/1000.0)
        """, (uid, symbol, tf, bar_ts))
        existing = cur.fetchone()
        
        if existing:
            # Сигнал уже есть - не отправляем в Telegram!
            print(f"[DB_LOG] Signal already exists: id={existing[0]}, skipping Telegram")
            return None
        
        # Новый сигнал - вставляем
        cur.execute("""
            INSERT INTO signals_log(user_id, symbol, timeframe, bar_ts, side, entry, sl, tp1, tp2, reason, mode, trigger_type)
            VALUES (%s, %s, %s, to_timestamp(%s/1000.0), %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (uid, symbol, tf, bar_ts, side, entry, sl, tp1, tp2, reason, mode, trigger_type))
        row = cur.fetchone()
        c.commit()
        sig_id = row[0] if row else None
        if sig_id:
            print(f"[DB_LOG] New signal inserted: id={sig_id} (mode={mode}, trigger={trigger_type})")
        return sig_id
# ============ КОНЕЦ ФУНКЦИИ: db_log_signal ============


# ============================================================================
# ФУНКЦИЯ: db_set_signal_message_id
# ОПИСАНИЕ: Сохраняет ID Telegram-сообщения для сигнала
#           Используется для привязки сообщения к сигналу в БД
# ============================================================================
def db_set_signal_message_id(sig_id: int, tg_message_id: int) -> None:
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("update signals_log set tg_message_id=%s where id=%s", (tg_message_id, sig_id))
        c.commit()
# ============ КОНЕЦ ФУНКЦИИ: db_set_signal_message_id ============


# ============================================================================
# ФУНКЦИЯ: db_get_signals
# ОПИСАНИЕ: Получает список сигналов пользователя за определенный период
#           Возвращает все параметры сигналов включая результаты
# ============================================================================
def db_get_signals(uid: int, since_ms: int) -> list[dict]:
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            select id, user_id, symbol, timeframe,
                   extract(epoch from bar_ts)*1000 as bar_ts,
                   side, entry, sl, tp1, tp2, reason,
                   outcome, outcome_price, outcome_ts
              from signals_log
             where user_id = %s
               and bar_ts >= to_timestamp(%s/1000.0)
             order by bar_ts desc
        """, (uid, since_ms))
        return [dict(r) for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_get_signals ============


# ============================================================================
# ФУНКЦИЯ: db_get_signals_with_events
# ОПИСАНИЕ: Получает сигналы со всеми событиями (TP1, TP2, SL) для статистики
#           Включает данные о достижении каждого уровня с ценой и временем
# ============================================================================
def db_get_signals_with_events(uid: int, since_ms: int) -> list[dict]:
    """Получает сигналы со всеми событиями (TP1, TP2, SL) для статистики путей"""
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # Проверяем, существуют ли новые колонки
        try:
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'signals_log' 
                  AND column_name = 'hit_tp1_price'
            """)
            has_event_columns = cur.fetchone() is not None
            
            # Проверяем наличие колонок TPmid
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'signals_log' 
                  AND column_name = 'hit_tp_mid_price'
            """)
            has_tp_mid_columns = cur.fetchone() is not None
        except Exception:
            has_event_columns = False
            has_tp_mid_columns = False
        
        # Проверяем наличие колонки outcome_v2
        try:
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'signals_log' 
                  AND column_name = 'outcome_v2'
            """)
            has_outcome_v2 = cur.fetchone() is not None
        except Exception:
            has_outcome_v2 = False
        
        # Определяем какие поля включать в запрос
        outcome_v2_field = "outcome_v2" if has_outcome_v2 else "NULL::jsonb as outcome_v2"
        
        if has_event_columns:
            # Проверяем тип колонок
            cur.execute("""
                SELECT data_type 
                FROM information_schema.columns 
                WHERE table_name = 'signals_log' 
                  AND column_name = 'hit_tp1_ts'
            """)
            col_type_row = cur.fetchone()
            col_type = col_type_row[0] if col_type_row else 'bigint'
            
            # Используем новый запрос с событиями, преобразуя timestamptz в bigint если нужно
            tp_mid_fields = "hit_tp_mid_price, extract(epoch from hit_tp_mid_ts)*1000 as hit_tp_mid_ts" if has_tp_mid_columns else "NULL::double precision as hit_tp_mid_price, NULL::bigint as hit_tp_mid_ts"
            if 'timestamp' in col_type:
                cur.execute(f"""
                    select id, user_id, symbol, timeframe,
                           extract(epoch from bar_ts)*1000 as bar_ts,
                           side, entry, sl, tp1, tp2, reason,
                           outcome, outcome_price, outcome_ts,
                           {outcome_v2_field},
                           hit_tp1_price, extract(epoch from hit_tp1_ts)*1000 as hit_tp1_ts,
                           hit_tp2_price, extract(epoch from hit_tp2_ts)*1000 as hit_tp2_ts,
                           hit_sl_price, extract(epoch from hit_sl_ts)*1000 as hit_sl_ts,
                           {tp_mid_fields}
                      from signals_log
                     where user_id = %s
                       and bar_ts >= to_timestamp(%s/1000.0)
                     order by bar_ts desc
                """, (uid, since_ms))
            else:
                tp_mid_fields_bigint = "hit_tp_mid_price, hit_tp_mid_ts" if has_tp_mid_columns else "NULL::double precision as hit_tp_mid_price, NULL::bigint as hit_tp_mid_ts"
                cur.execute(f"""
                    select id, user_id, symbol, timeframe,
                           extract(epoch from bar_ts)*1000 as bar_ts,
                           side, entry, sl, tp1, tp2, reason,
                           outcome, outcome_price, outcome_ts,
                           {outcome_v2_field},
                           hit_tp1_price, hit_tp1_ts,
                           hit_tp2_price, hit_tp2_ts,
                           hit_sl_price, hit_sl_ts,
                           {tp_mid_fields_bigint}
                      from signals_log
                     where user_id = %s
                       and bar_ts >= to_timestamp(%s/1000.0)
                     order by bar_ts desc
                """, (uid, since_ms))
        else:
            # Fallback: используем старый запрос без событий
            print("[WARN] hit_tp1_price columns not found, using fallback query")
            cur.execute("""
                select id, user_id, symbol, timeframe,
                       extract(epoch from bar_ts)*1000 as bar_ts,
                       side, entry, sl, tp1, tp2, reason,
                       outcome, outcome_price, outcome_ts,
                       NULL::jsonb as outcome_v2,
                       NULL::double precision as hit_tp1_price, NULL::bigint as hit_tp1_ts,
                       NULL::double precision as hit_tp2_price, NULL::bigint as hit_tp2_ts,
                       NULL::double precision as hit_sl_price, NULL::bigint as hit_sl_ts,
                       NULL::double precision as hit_tp_mid_price, NULL::bigint as hit_tp_mid_ts
                  from signals_log
                 where user_id = %s
                   and bar_ts >= to_timestamp(%s/1000.0)
                 order by bar_ts desc
            """, (uid, since_ms))
        
        return [dict(r) for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_get_signals_with_events ============


# ============================================================================
# ФУНКЦИЯ: db_get_closed_trades_with_fills
# ОПИСАНИЕ: Возвращает закрытые сделки пользователя с частичными закрытиями
#           для расчёта дневного equity-результата
# ============================================================================
def db_get_closed_trades_with_fills(uid: int, since_ms: int) -> list[dict]:
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """
            SELECT t.id,
                   t.entry_price_user,
                   t.side,
                   t.base_qty,
                   t.fee_rate_bps,
                   t.fee_open,
                   t.fee_close_total,
                   t.entry_notional_usdt,
                   EXTRACT(EPOCH FROM t.closed_at) AS closed_ts
              FROM user_trades t
             WHERE t.user_id=%s
               AND t.closed_at IS NOT NULL
               AND t.closed_at >= to_timestamp(%s/1000.0)
               AND COALESCE(t.status,'') <> 'ACTIVE'
            ORDER BY t.closed_at ASC
            """,
            (uid, since_ms),
        )
        trades = [dict(r) for r in cur.fetchall()]

        if not trades:
            return []

        trade_ids = tuple(t["id"] for t in trades)
        cur.execute(
            f"""
            SELECT f.trade_id, f.type, f.exit_price, f.qty, EXTRACT(EPOCH FROM f.created_at) AS ts
              FROM user_trade_fills f
             WHERE f.trade_id IN %s
             ORDER BY f.created_at ASC
            """,
            (trade_ids,),
        )
        fills_by_trade: dict[int, list[dict]] = {}
        for r in cur.fetchall():
            d = dict(r)
            fills_by_trade.setdefault(d["trade_id"], []).append(d)

        # Соберём структуру
        result = []
        for t in trades:
            base_qty = float(t.get("base_qty") or 0.0)
            legs = []
            for f in fills_by_trade.get(t["id"], []):
                qty = float(f.get("qty") or 0.0)
                qty_frac = (qty / base_qty) if base_qty > 0 else 0.0
                legs.append({
                    "type": str(f.get("type") or ""),
                    "price": float(f.get("exit_price") or 0.0),
                    "qty_frac": qty_frac,
                    "time": float(f.get("ts") or 0.0),
                })

            # Рассчитаем комиссию (round-trip pct)
            notional = float(t.get("entry_notional_usdt") or 0.0)
            fee_open = float(t.get("fee_open") or 0.0)
            fee_close_total = float(t.get("fee_close_total") or 0.0)
            fee_rate_bps = float(t.get("fee_rate_bps") or 0.0)
            if notional > 0:
                fee_round_pct = (fee_open + fee_close_total) / notional
            else:
                fee_round_pct = (2.0 * fee_rate_bps) / 10000.0 if fee_rate_bps else 0.0

            result.append({
                "entry_price": float(t.get("entry_price_user") or 0.0),
                "side": str(t.get("side") or "long").lower(),
                "legs": legs,
                "fee_round_pct": fee_round_pct,
                "close_time": float(t.get("closed_ts") or 0.0),
            })

        return result
# ============ КОНЕЦ ФУНКЦИИ: db_get_closed_trades_with_fills ============


# ============================================================================
# ФУНКЦИЯ: db_update_signal_events
# ОПИСАНИЕ: Обновляет события по сигналу (достижение TP1/TP2/SL)
#           Сохраняет цены и время срабатывания каждого уровня
# ============================================================================
def db_update_signal_events(sig_id: int, events: dict) -> None:
    """Обновляет события TP1/TP2/SL для сигнала"""
    with _db_conn() as c, c.cursor() as cur:
        # Проверяем, существуют ли новые колонки
        try:
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'signals_log' 
                  AND column_name = 'hit_tp1_price'
            """)
            has_event_columns = cur.fetchone() is not None
            
            # Проверяем наличие колонок TPmid
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'signals_log' 
                  AND column_name = 'hit_tp_mid_price'
            """)
            has_tp_mid_columns = cur.fetchone() is not None
        except Exception:
            has_event_columns = False
            has_tp_mid_columns = False
        
        if has_event_columns:
            # Проверяем тип колонок (bigint или timestamptz)
            cur.execute("""
                SELECT data_type 
                FROM information_schema.columns 
                WHERE table_name = 'signals_log' 
                  AND column_name = 'hit_tp1_ts'
            """)
            col_type_row = cur.fetchone()
            col_type = col_type_row[0] if col_type_row else 'bigint'
            
            # Обновляем новые колонки с учетом типа
            # Формируем SET часть для TPmid в зависимости от наличия колонок
            tp_mid_set = ""
            tp_mid_params = []
            if has_tp_mid_columns and ("hit_tp_mid_price" in events or "hit_tp_mid_ts" in events):
                if 'timestamp' in col_type:
                    tp_mid_set = ", hit_tp_mid_price = %s, hit_tp_mid_ts = CASE WHEN %s IS NOT NULL THEN to_timestamp(%s/1000.0) ELSE NULL END"
                    tp_mid_params = [
                        events.get("hit_tp_mid_price"),
                        events.get("hit_tp_mid_ts"), events.get("hit_tp_mid_ts")
                    ]
                else:
                    tp_mid_set = ", hit_tp_mid_price = %s, hit_tp_mid_ts = %s"
                    tp_mid_params = [
                        events.get("hit_tp_mid_price"),
                        events.get("hit_tp_mid_ts")
                    ]
            
            if 'timestamp' in col_type:
                # Колонки типа timestamptz - преобразуем миллисекунды
                cur.execute(f"""
                    update signals_log
                       set hit_tp1_price = %s, 
                           hit_tp1_ts = CASE WHEN %s IS NOT NULL THEN to_timestamp(%s/1000.0) ELSE NULL END,
                           hit_tp2_price = %s,
                           hit_tp2_ts = CASE WHEN %s IS NOT NULL THEN to_timestamp(%s/1000.0) ELSE NULL END,
                           hit_sl_price = %s,
                           hit_sl_ts = CASE WHEN %s IS NOT NULL THEN to_timestamp(%s/1000.0) ELSE NULL END
                           {tp_mid_set}
                     where id = %s
                """, (
                    events.get("hit_tp1_price"), 
                    events.get("hit_tp1_ts"), events.get("hit_tp1_ts"),
                    events.get("hit_tp2_price"),
                    events.get("hit_tp2_ts"), events.get("hit_tp2_ts"),
                    events.get("hit_sl_price"),
                    events.get("hit_sl_ts"), events.get("hit_sl_ts"),
                    *tp_mid_params,
                    sig_id
                ))
            else:
                # Колонки типа bigint - используем как есть
                cur.execute(f"""
                    update signals_log
                       set hit_tp1_price = %s, hit_tp1_ts = %s,
                           hit_tp2_price = %s, hit_tp2_ts = %s,
                           hit_sl_price = %s, hit_sl_ts = %s
                           {tp_mid_set}
                     where id = %s
                """, (
                    events.get("hit_tp1_price"), events.get("hit_tp1_ts"),
                    events.get("hit_tp2_price"), events.get("hit_tp2_ts"),
                    events.get("hit_sl_price"), events.get("hit_sl_ts"),
                    *tp_mid_params,
                    sig_id
                ))
            c.commit()
        else:
            # Колонок нет - пропускаем обновление
            print(f"[WARN] Cannot update signal events for {sig_id}: columns not found")
            pass
# ============ КОНЕЦ ФУНКЦИИ: db_update_signal_events ============


# ============================================================================
# ФУНКЦИЯ: db_set_signal_outcome
# ОПИСАНИЕ: Устанавливает итоговый результат сигнала (TP/SL/OPEN)
#           Сохраняет финальную цену закрытия и время
# ============================================================================
def db_set_signal_outcome(sig_id: int, outcome: str, price: float | None, ts_ms: int | None):
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("""
            update signals_log
               set outcome=%s, outcome_price=%s, outcome_ts=%s
             where id=%s
        """, (outcome, price, ts_ms, sig_id))
        c.commit()
# ============ КОНЕЦ ФУНКЦИИ: db_set_signal_outcome ============


# =============================================================================
# PANEL PREFS
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_get_panel_message_id
# ОПИСАНИЕ: Получает ID сообщения панели сделок для пользователя из БД
#           Используется для обновления одного и того же сообщения
# ============================================================================
def db_get_panel_message_id(uid: int) -> Optional[int]:
    if not db_enabled():
        return None
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("select panel_message_id from user_prefs where user_id=%s", (uid,))
        row = cur.fetchone()
        return int(row[0]) if row and row[0] else None
# ============ КОНЕЦ ФУНКЦИИ: db_get_panel_message_id ============


# ============================================================================
# ФУНКЦИЯ: db_set_panel_message_id
# ОПИСАНИЕ: Сохраняет ID сообщения панели сделок для пользователя в БД
#           Используется после создания нового сообщения панели
# ============================================================================
def db_set_panel_message_id(uid: int, message_id: int) -> None:
    if not db_enabled():
        return
    with _db_conn() as c, c.cursor() as cur:
        # upsert user_prefs record
        cur.execute(
            """
            insert into user_prefs(user_id, panel_message_id)
            values(%s,%s)
            on conflict (user_id) do update set panel_message_id=excluded.panel_message_id
            """,
            (uid, message_id),
        )
        c.commit()
# ============ КОНЕЦ ФУНКЦИИ: db_set_panel_message_id ============


# ============================================================================
# ФУНКЦИЯ: db_toggle_auto_close
# ОПИСАНИЕ: Переключает настройку автоматического закрытия сделок по сигналам
#           Возвращает новое значение настройки
# ============================================================================
def db_toggle_auto_close(uid: int) -> bool:
    """Toggle auto_close_by_signal and return new value."""
    if not db_enabled():
        return False
    with _db_conn() as c, c.cursor() as cur:
        cur.execute(
            """
            insert into user_prefs(user_id, auto_close_by_signal)
            values(%s, false)
            on conflict (user_id) do update set auto_close_by_signal = not user_prefs.auto_close_by_signal
            returning auto_close_by_signal
            """,
            (uid,),
        )
        val = cur.fetchone()[0]
        c.commit()
        return bool(val)
# ============ КОНЕЦ ФУНКЦИИ: db_toggle_auto_close ============


# =============================================================================
# ADMIN FUNCTIONS
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_list_all_symbols
# ОПИСАНИЕ: Получает список всех доступных торговых пар из справочника
#           Используется в админ-панели
# ============================================================================
def db_list_all_symbols() -> List[str]:
    """Получить список всех символов из справочника"""
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("SELECT symbol FROM symbols ORDER BY symbol")
        return [r[0] for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_list_all_symbols ============


# ============================================================================
# ФУНКЦИЯ: db_search_users
# ОПИСАНИЕ: Поиск пользователей по username или user_id
#           Поддерживает поиск как по числовому ID, так и по имени пользователя
# ============================================================================
def db_search_users(query: str) -> List[dict]:
    """Поиск пользователей по username или user_id"""
    with _db_conn() as c, c.cursor() as cur:
        # Если query - число, ищем по ID
        if query.strip().isdigit():
            cur.execute("""
                SELECT user_id, username, created_at
                FROM users
                WHERE user_id = %s
            """, (int(query.strip()),))
        else:
            # Иначе по username (убираем @ если есть)
            username = query.strip().lstrip('@')
            cur.execute("""
                SELECT user_id, username, created_at
                FROM users
                WHERE username ILIKE %s
                ORDER BY created_at DESC
                LIMIT 20
            """, (f"%{username}%",))
        
        return [{"user_id": r[0], "username": r[1], "created_at": r[2]} for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_search_users ============


# ============================================================================
# ФУНКЦИЯ: db_get_user_info
# ОПИСАНИЕ: Получает полную информацию о пользователе (ID, username, дата создания, часовой пояс)
#           Используется в админ-панели
# ============================================================================
def db_get_user_info(uid: int) -> dict:
    """Получить информацию о пользователе"""
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT user_id, username, created_at, timezone
            FROM users
            WHERE user_id = %s
        """, (uid,))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "user_id": row[0],
            "username": row[1],
            "created_at": row[2],
            "timezone": row[3]
        }
# ============ КОНЕЦ ФУНКЦИИ: db_get_user_info ============


# =============================================================================
# STATISTICS & ANALYTICS
# =============================================================================

# ============================================================================
# ФУНКЦИЯ: db_get_signals_by_symbol
# ОПИСАНИЕ: Группирует сигналы пользователя по торговым парам
#           Возвращает список сигналов, сгруппированных по символам для статистики
# ============================================================================
def db_get_signals_by_symbol(uid: int, since_ms: int) -> dict:
    """Получить статистику по символам"""
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            select symbol, 
                   count(*) as total,
                   count(case when outcome = 'TP' then 1 end) as tp_count,
                   count(case when outcome = 'SL' then 1 end) as sl_count,
                   count(case when outcome = 'OPEN' or outcome is null then 1 end) as open_count
            from signals_log
            where user_id = %s and bar_ts >= to_timestamp(%s/1000.0)
            group by symbol
            order by total desc
        """, (uid, since_ms))
        return [dict(r) for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_get_signals_by_symbol ============


# ============================================================================
# ФУНКЦИЯ: db_get_signals_by_timeframe
# ОПИСАНИЕ: Группирует сигналы пользователя по таймфреймам
#           Возвращает статистику сигналов по каждому ТФ
# ============================================================================
def db_get_signals_by_timeframe(uid: int, since_ms: int) -> dict:
    """Получить статистику по таймфреймам"""
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            select timeframe,
                   count(*) as total,
                   count(case when outcome = 'TP' then 1 end) as tp_count,
                   count(case when outcome = 'SL' then 1 end) as sl_count,
                   count(case when outcome = 'OPEN' or outcome is null then 1 end) as open_count
            from signals_log
            where user_id = %s and bar_ts >= to_timestamp(%s/1000.0)
            group by timeframe
            order by total desc
        """, (uid, since_ms))
        return [dict(r) for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_get_signals_by_timeframe ============


# ============================================================================
# ФУНКЦИЯ: db_get_signals_by_session
# ОПИСАНИЕ: Группирует сигналы по торговым сессиям (азиатская/европейская/американская)
#           Анализирует производительность сигналов в разное время суток
# ============================================================================
def db_get_signals_by_session(uid: int, since_ms: int) -> dict:
    """Получить статистику по торговым сессиям"""
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            select 
                case 
                    when extract(hour from bar_ts at time zone 'UTC') between 0 and 7 then 'asian'
                    when extract(hour from bar_ts at time zone 'UTC') between 8 and 15 then 'european'
                    else 'american'
                end as session,
                count(*) as total,
                count(case when outcome = 'TP' then 1 end) as tp_count,
                count(case when outcome = 'SL' then 1 end) as sl_count,
                count(case when outcome = 'OPEN' or outcome is null then 1 end) as open_count
            from signals_log
            where user_id = %s and bar_ts >= to_timestamp(%s/1000.0)
            group by session
            order by total desc
        """, (uid, since_ms))
        return [dict(r) for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_get_signals_by_session ============


# ============================================================================
# ФУНКЦИЯ: db_clear_signals
# ОПИСАНИЕ: Удаляет сигналы пользователя за период и возвращает количество
#           Очищает старые сигналы из базы данных
# ============================================================================
def db_clear_signals(uid: int, since_ms: int) -> int:
    """Удалить сигналы за период и вернуть количество удаленных"""
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("""
            delete from signals_log 
            where user_id = %s and bar_ts >= to_timestamp(%s/1000.0)
        """, (uid, since_ms))
        deleted_count = cur.rowcount
        c.commit()
        return deleted_count
# ============ КОНЕЦ ФУНКЦИИ: db_clear_signals ============


# ============================================================================
# ФУНКЦИЯ: db_list_levels
# ОПИСАНИЕ: Получить список уровней пользователя по символу и таймфрейму
# ============================================================================
def db_list_levels(uid: int, symbol: str, timeframe: str) -> List[Dict[str, Any]]:
    """Получить список уровней пользователя"""
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("""
            select symbol, timeframe, level, note, created_at
            from user_levels
            where user_id = %s and symbol = %s and timeframe = %s
            order by level
        """, (uid, symbol, timeframe))
        rows = cur.fetchall()
        return [
            {
                "symbol": r[0],
                "timeframe": r[1],
                "level": float(r[2]),
                "note": r[3],
                "created_at": r[4]
            }
            for r in rows
        ]
# ============ КОНЕЦ ФУНКЦИИ: db_list_levels ============


# ============================================================================
# ФУНКЦИЯ: db_levels_get_config
# ОПИСАНИЕ: Получить конфигурацию использования уровней для пользователя
# ============================================================================
def db_levels_get_config(uid: int) -> dict:
    """Получить конфигурацию уровней пользователя"""
    with _db_conn() as c, c.cursor() as cur:
        try:
            cur.execute("SELECT levels_config FROM user_prefs WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            cfg = row[0] if row and row[0] else {}
        except Exception as e:
            print(f"[WARN] levels_config missing, ensuring column: {e}")
            try:
                cur.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS levels_config jsonb")
                c.commit()
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
            cfg = {}
        
        # Дефолты
        defaults = {
            "use_levels": True,
            "block_against_level": True,
            "score_threshold": 3,
            "near_tight": 0.25,
            "near_moderate": 0.50,
            "near_wide": 1.00,
        }
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        
        return cfg
# ============ КОНЕЦ ФУНКЦИИ: db_levels_get_config ============


# ============================================================================
# ФУНКЦИИ: TPmid config getters/setters
# ОПИСАНИЕ: Персональные настройки промежуточного TP
#   tp_mid_config jsonb schema:
#     {
#       "enabled": bool,            # включено/выключено (default false)
#       "auto": bool,               # авто-режим (default true)
#       "delta_pct": float          # ручная дельта в процентах, со знаком, напр. 0.1 для +0.1% (default 0.1)
#     }
# ============================================================================
def db_get_tp_mid_config(uid: int) -> dict:
    """Получить TPmid конфиг пользователя с дефолтами."""
    if not db_enabled():
        return {"enabled": False, "auto": True, "delta_pct": 0.1}
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            cur.execute("SELECT tp_mid_config FROM user_prefs WHERE user_id=%s", (uid,))
            row = cur.fetchone()
            cfg = row[0] if row and row[0] else {}
        except Exception as e:
            print(f"[WARN] tp_mid_config missing, ensuring column: {e}")
            try:
                cur.execute("ALTER TABLE user_prefs ADD COLUMN IF NOT EXISTS tp_mid_config jsonb")
                c.commit()
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
            cfg = {}
    # Дефолты
    defaults = {"enabled": False, "auto": True, "delta_pct": 0.1}
    for k, v in defaults.items():
        cfg.setdefault(k, v)
    return cfg


def db_set_tp_mid_config(uid: int, cfg_updates: dict) -> dict:
    """Обновить TPmid конфиг (upsert) и вернуть актуальный конфиг."""
    if not db_enabled():
        return db_get_tp_mid_config(uid)
    import json
    with _db_conn() as c, c.cursor() as cur:
        # Прочитать текущий
        cur.execute("SELECT tp_mid_config FROM user_prefs WHERE user_id=%s", (uid,))
        row = cur.fetchone()
        current = row[0] if row and row[0] else {}
        merged = {
            "enabled": bool(current.get("enabled", False)),
            "auto": bool(current.get("auto", True)),
            "delta_pct": float(current.get("delta_pct", 0.1)) if current.get("delta_pct") is not None else 0.1,
        }
        for k, v in cfg_updates.items():
            if k == "delta_pct":
                try:
                    merged[k] = float(v)
                except Exception:
                    continue
            elif k in ("enabled", "auto"):
                merged[k] = bool(v)
        # upsert
        cur.execute(
            """
            INSERT INTO user_prefs(user_id, tp_mid_config)
            VALUES(%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET tp_mid_config = EXCLUDED.tp_mid_config
            """,
            (uid, json.dumps(merged))
        )
        c.commit()
        return merged

# ============================================================================
# ФУНКЦИЯ: db_update_signal_outcome
# ОПИСАНИЕ: Обновляет outcome_v2 для сигнала (новый формат для статистики)
# ============================================================================
def db_update_signal_outcome(sig_id: int, outcome: dict):
    """
    Обновляет outcome_v2 сигнала
    
    Args:
        sig_id: ID сигнала
        outcome: {"status": "TP1+TP2", "r": 2.5, "time_to_tp1_min": 45, ...}
    """
    import json
    with _db_conn() as c, c.cursor() as cur:
        cur.execute("""
            UPDATE signals_log
            SET outcome_v2 = %s
            WHERE id = %s
        """, (json.dumps(outcome), sig_id))
        c.commit()
    print(f"[DB] Updated outcome for signal {sig_id}: {outcome.get('status')}")
# ============ КОНЕЦ ФУНКЦИИ: db_update_signal_outcome ============


# ============================================================================
# ФУНКЦИЯ: db_log_signal_ban
# ОПИСАНИЕ: Сохраняет забаненный сигнал для статистики фильтров
# ============================================================================
def db_log_signal_ban(uid: int, log_dict: dict):
    """
    Сохраняет забаненный сигнал для статистики фильтров
    
    Args:
        uid: user_id
        log_dict: SignalLog.to_dict() с полями symbol, timeframe, side, ban_reasons
    """
    import json
    with _db_conn() as c, c.cursor() as cur:
        ban_reasons = log_dict.get('ban_reasons', [])
        cur.execute("""
            INSERT INTO signals_ban_log(user_id, symbol, timeframe, side, ban_reasons, log_json)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            uid,
            log_dict.get('symbol', ''),
            log_dict.get('timeframe', ''),
            log_dict.get('side', ''),
            ban_reasons,
            json.dumps(log_dict)
        ))
        c.commit()
    print(f"[DB] Logged BAN: {log_dict.get('symbol')} {log_dict.get('timeframe')} {log_dict.get('side')} - {len(ban_reasons)} reasons")
# ============ КОНЕЦ ФУНКЦИИ: db_log_signal_ban ============


# ============================================================================
# ФУНКЦИЯ: db_get_ban_logs
# ОПИСАНИЕ: Получить забаненные сигналы для пользователя
# ============================================================================
def db_get_ban_logs(uid: int, since_ms: int) -> list[dict]:
    """
    Получить забаненные сигналы
    
    Args:
        uid: user_id
        since_ms: timestamp в миллисекундах
    
    Returns:
        Список словарей с log_json
    """
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT log_json, ban_reasons, ban_ts
            FROM signals_ban_log
            WHERE user_id = %s
              AND ban_ts >= to_timestamp(%s/1000.0)
            ORDER BY ban_ts DESC
        """, (uid, since_ms))
        
        results = []
        for row in cur.fetchall():
            log_data = dict(row['log_json']) if row['log_json'] else {}
            log_data['ban_reasons'] = row['ban_reasons'] or []
            results.append(log_data)
        
        return results
# ============ КОНЕЦ ФУНКЦИИ: db_get_ban_logs ============


# ============================================================================
# ФУНКЦИЯ: db_get_open_trades
# ОПИСАНИЕ: Получает все открытые сделки пользователя для EOD-закрытия
# ============================================================================
def db_get_open_trades(uid: int) -> list[dict]:
    """Возвращает все открытые сделки пользователя"""
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT id, symbol, side, entry_price_user, remaining_qty,
                   tp1, tp2, sl, fee_rate_bps, timeframe, signal_id
            FROM user_trades
            WHERE user_id = %s
              AND status = 'ACTIVE'
              AND remaining_qty > 1e-10
            ORDER BY opened_at ASC
        """, (uid,))
        return [dict(r) for r in cur.fetchall()]
# ============ КОНЕЦ ФУНКЦИИ: db_get_open_trades ============


# ============================================================================
# ФУНКЦИЯ: db_close_trade_eod
# ОПИСАНИЕ: Закрывает сделку в конце дня по текущей цене
#           Возвращает тип закрытия: 'BE' или 'BELOW_ENTRY'
# ============================================================================
def db_close_trade_eod(uid: int, trade_id: int, exit_price: float, close_type: str) -> bool:
    """
    Закрывает сделку в конце дня.
    
    Args:
        uid: ID пользователя
        trade_id: ID сделки
        exit_price: Цена закрытия
        close_type: Тип закрытия ('EOD_BE' или 'EOD_BELOW_ENTRY')
    
    Returns:
        True если успешно, False если ошибка
    """
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            cur.execute("BEGIN")
            
            # Получаем данные сделки
            cur.execute("""
                SELECT remaining_qty, entry_price_user, side, symbol, fee_rate_bps,
                       pnl_gross_total, fee_close_total, pnl_net_total
                FROM user_trades
                WHERE id = %s AND user_id = %s AND status = 'ACTIVE'
            """, (trade_id, uid))
            
            trade = cur.fetchone()
            if not trade:
                cur.execute("ROLLBACK")
                return False
            
            trade = dict(trade)
            remaining_qty = float(trade['remaining_qty'] or 0)
            entry_price = float(trade['entry_price_user'] or 0)
            side = str(trade['side'] or 'LONG').upper()
            symbol = str(trade['symbol'] or '')
            fee_rate_bps = float(trade['fee_rate_bps'] or 5)
            
            current_pnl_gross_total = float(trade['pnl_gross_total'] or 0)
            current_fee_close_total = float(trade['fee_close_total'] or 0)
            current_pnl_net_total = float(trade['pnl_net_total'] or 0)
            
            if remaining_qty <= 1e-10:
                cur.execute("ROLLBACK")
                return False
            
            # Рассчитываем PnL
            exit_price_f = float(exit_price)
            close_amount_usdt = remaining_qty * exit_price_f
            
            if side == 'LONG':
                pnl_gross = (exit_price_f - entry_price) * remaining_qty
            else:  # SHORT
                pnl_gross = (entry_price - exit_price_f) * remaining_qty
            
            # Комиссия закрытия
            fee_close = close_amount_usdt * fee_rate_bps / 10000.0
            pnl_net = pnl_gross - fee_close
            
            # Обновляем totals
            new_pnl_gross_total = current_pnl_gross_total + pnl_gross
            new_fee_close_total = current_fee_close_total + fee_close
            new_pnl_net_total = current_pnl_net_total + pnl_net
            
            # Создаём fill запись
            cur.execute("""
                INSERT INTO user_trade_fills (
                    trade_id, type, exit_price, qty, amount_usdt,
                    pnl_gross, fee_close, pnl_net
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                trade_id, close_type, exit_price_f, remaining_qty,
                close_amount_usdt, pnl_gross, fee_close, pnl_net
            ))
            
            # Закрываем сделку полностью
            cur.execute("""
                UPDATE user_trades
                SET remaining_qty = 0,
                    pnl_gross_total = %s,
                    fee_close_total = %s,
                    pnl_net_total = %s,
                    status = 'CLOSED',
                    closed_at = now()
                WHERE id = %s
            """, (new_pnl_gross_total, new_fee_close_total, new_pnl_net_total, trade_id))
            
            cur.execute("COMMIT")
            return True
            
        except Exception as e:
            print(f"[EOD] Error closing trade {trade_id}: {e}")
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            return False
# ============ КОНЕЦ ФУНКЦИИ: db_close_trade_eod ============


# ============================================================================
# ФУНКЦИЯ: db_get_eod_closed_stats
# ОПИСАНИЕ: Получает статистику EOD-закрытий за день
# ============================================================================
def db_get_eod_closed_stats(uid: int, day_start_ts: int, day_end_ts: int) -> dict:
    """
    Возвращает статистику EOD-закрытий за день.
    
    Returns:
        {
            'n_be': int,
            'n_below_entry': int
        }
    """
    import psycopg2.extras
    with _db_conn() as c, c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT f.type, COUNT(*) as cnt
            FROM user_trade_fills f
            JOIN user_trades t ON f.trade_id = t.id
            WHERE t.user_id = %s
              AND f.created_at >= to_timestamp(%s/1000.0)
              AND f.created_at < to_timestamp(%s/1000.0)
              AND f.type IN ('EOD_BE', 'EOD_BELOW_ENTRY')
            GROUP BY f.type
        """, (uid, day_start_ts, day_end_ts))
        
        results = {r['type']: int(r['cnt']) for r in cur.fetchall()}
        
        return {
            'n_be': results.get('EOD_BE', 0),
            'n_below_entry': results.get('EOD_BELOW_ENTRY', 0)
        }
# ============ КОНЕЦ ФУНКЦИИ: db_get_eod_closed_stats ============
