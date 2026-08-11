"""Trade storage — dual-mode (Postgres via DATABASE_URL, else local SQLite),
same pattern as the sibling bots. Every trade table is per `{universe_bot}/
{variant_key}` combination (12 total) — see engine/config.py for the new
naming convention this project uses instead of the sibling bots' `vN_M` keys.
"""

import os
import re
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytz
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.helpers import PROJECT_ROOT, get_logger
from engine.config import all_variant_ids

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")


def _now() -> datetime:
    """IST timestamp — never a naive datetime.now(), which is UTC on Streamlit
    Cloud's server but happens to be IST on a local dev machine. Real bug hit in
    the sibling bots' production (2026-07-14), ported as a permanent guard."""
    return datetime.now(IST)


def _variant_to_table_suffix(variant_id: str) -> str:
    """'bot_751/subh30_trailing_ema' -> 'bot_751__subh30_trailing_ema' — SQL table
    names can't contain '/', so the display/dict-key format uses '/' but table
    names use '__'. Only ever called on our own fixed config values (see
    VARIANT_TABLES below), never on external input."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", variant_id.replace("/", "__"))


VARIANT_TABLES = {v: f"ute_trades_{_variant_to_table_suffix(v)}" for v in all_variant_ids()}


def _table(variant_id: str) -> str:
    try:
        return VARIANT_TABLES[variant_id]
    except KeyError:
        raise ValueError(f"Unknown variant id: {variant_id!r}") from None


_TRADES_TABLE_COLUMNS = """
    id {pk},
    symbol TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('OPEN', 'CLOSED')),
    entry_time TEXT NOT NULL,
    entry_price REAL NOT NULL,
    quantity INTEGER NOT NULL,
    capital_used REAL NOT NULL,
    leverage REAL NOT NULL DEFAULT 1.0,
    entry_charges REAL NOT NULL,
    arm_cycle_id TEXT,
    peak_price REAL,
    trough_price REAL,
    target_hit INTEGER NOT NULL DEFAULT 0,
    exit_time TEXT,
    exit_price REAL,
    exit_reason TEXT,
    exit_charges REAL,
    gross_pnl REAL,
    total_charges REAL,
    net_pnl REAL,
    net_pnl_pct REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
"""


def _variant_tables_sql(pk: str) -> str:
    return "\n\n".join(
        f"CREATE TABLE IF NOT EXISTS {table} (" + _TRADES_TABLE_COLUMNS.format(pk=pk) + ");"
        for table in VARIANT_TABLES.values()
    )


_SHARED_SCHEMA_TAIL = """
CREATE TABLE IF NOT EXISTS ute_cycle_log (
    id {pk},
    cycle_time TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT,
    symbols_scanned INTEGER,
    message TEXT,
    warnings TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_ute_cycle_log_time ON ute_cycle_log(cycle_time);

CREATE TABLE IF NOT EXISTS ute_checkpoint_log (
    id {pk},
    trade_date TEXT NOT NULL,
    checkpoint_time TEXT NOT NULL,
    executed_at TEXT NOT NULL,
    signal_found INTEGER,
    symbol TEXT,
    trade_id INTEGER,
    variant_id TEXT NOT NULL
);

-- Small shared key/value store — NOT the same thing as engine.config's
-- settings.yaml (a LOCAL FILE). 2026-08-11 live finding: admin app (app.py)
-- and viewer app (viewer_app.py) are TWO SEPARATE Streamlit Cloud
-- deployments, each with its OWN isolated filesystem — a value admin saves
-- to its local settings.yaml is invisible to viewer, which runs in a
-- different container entirely. Anything that must be visible across BOTH
-- apps (like which variant is "public") has to go through the ONE thing
-- they actually share: this database. See get_setting()/set_setting().
CREATE TABLE IF NOT EXISTS ute_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

SQLITE_SCHEMA = _variant_tables_sql("INTEGER PRIMARY KEY AUTOINCREMENT") + "\n\n" \
    + _SHARED_SCHEMA_TAIL.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT")

POSTGRES_SCHEMA = _variant_tables_sql("SERIAL PRIMARY KEY") + "\n\n" \
    + _SHARED_SCHEMA_TAIL.format(pk="SERIAL PRIMARY KEY")

_engine: Engine | None = None


def _sqlite_path() -> Path:
    path = PROJECT_ROOT / "data" / "unified_trading_engine.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        _engine = create_engine(database_url, pool_pre_ping=True)
        logger.info(f"Using {_engine.dialect.name} database (DATABASE_URL set)")
    else:
        _engine = create_engine(f"sqlite:///{_sqlite_path()}")
        logger.info("Using local SQLite database (no DATABASE_URL set)")
    return _engine


def _lock_key_for_variant(variant_id: str) -> int:
    """Deterministic per-variant lock key (CRC32 of the variant_id, always a
    non-negative 32-bit int — fits Postgres advisory locks' bigint param with
    room to spare). Per-variant rather than one single global key (as this
    used to be) — 2026-08-10 code review flagged that a global key made an
    unrelated variant's REST network call (the trailing-exit candle fetch,
    which happens WHILE the lock is held) block every other variant's
    exit-check too, even though only calls for the SAME variant_id can ever
    actually race each other."""
    return zlib.crc32(variant_id.encode())


@contextmanager
def acquire_trade_lock(variant_id: str):
    """Serializes the read-check-then-write around opening/closing ONE
    variant's position across concurrent callers (2-min REST job vs
    engine.live_feed's tick-driven thread, or local dev + deployed Cloud app
    both on the same DB) — same pattern as the sibling bots' single-key
    version, scoped per-variant since 2026-08-10 (see _lock_key_for_variant)."""
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        yield
        return
    conn = engine.connect()
    trans = conn.begin()
    try:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key_for_variant(variant_id)})
        yield
        trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()


_SCAN_CYCLE_LOCK_KEY = 728193042  # arbitrary fixed key, distinct from any per-variant crc32 value


@contextmanager
def try_acquire_scan_lock():
    """Yields True if this caller got the lock, False if another
    run_full_scan_cycle() is already in progress — NON-blocking
    (pg_try_advisory_lock, not pg_advisory_xact_lock), because Stage 1+2 can
    take minutes of real network I/O; waiting would just queue redundant
    work, not add safety.

    2026-08-11 live-market finding: a scheduled cron run and a manual
    "Run Scan Now" click can overlap (only the scheduled job itself was ever
    protected via APScheduler's own max_instances=1, which doesn't cover a
    manual trigger racing it). Observed an overlap produce an inconsistent
    'entered' list in the cycle log — was_flat still correctly gated real
    double-entry (verified no duplicate trade rows), so this wasn't a
    capital-correctness bug, but cycle logs became misleading and Stage 1/2
    API load doubled up needlessly. This lock makes a second overlapping
    call skip cleanly instead."""
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        yield True
        return
    conn = engine.connect()
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _SCAN_CYCLE_LOCK_KEY}).scalar()
        try:
            yield bool(got)
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _SCAN_CYCLE_LOCK_KEY})
    finally:
        conn.close()


def init_db() -> None:
    engine = get_engine()
    schema = POSTGRES_SCHEMA if engine.dialect.name == "postgresql" else SQLITE_SCHEMA
    with engine.begin() as conn:
        for statement in schema.strip().split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(text(statement))


def open_trade(variant_id: str, symbol: str, entry_price: float, quantity: int, capital_used: float,
               entry_charges: float, arm_cycle_id: str | None, leverage: float = 1.0) -> int:
    table = _table(variant_id)
    now = _now().isoformat()
    with get_engine().begin() as conn:
        result = conn.execute(
            text(f"""INSERT INTO {table}
                    (symbol, status, entry_time, entry_price, quantity, capital_used, leverage,
                     entry_charges, arm_cycle_id, peak_price, trough_price, created_at, updated_at)
                    VALUES (:symbol, 'OPEN', :entry_time, :entry_price, :quantity, :capital_used, :leverage,
                            :entry_charges, :arm_cycle_id, :entry_price, :entry_price, :now, :now)
                    RETURNING id"""),
            {"symbol": symbol, "entry_time": now, "entry_price": entry_price, "quantity": quantity,
             "capital_used": capital_used, "leverage": leverage, "entry_charges": entry_charges,
             "arm_cycle_id": arm_cycle_id, "now": now},
        )
        return result.scalar_one()


def update_price_extremes(variant_id: str, trade_id: int, peak_price: float, trough_price: float) -> None:
    table = _table(variant_id)
    now = _now().isoformat()
    with get_engine().begin() as conn:
        conn.execute(
            text(f"UPDATE {table} SET peak_price=:peak, trough_price=:trough, updated_at=:now WHERE id=:id"),
            {"peak": peak_price, "trough": trough_price, "now": now, "id": trade_id},
        )


def mark_target_hit(variant_id: str, trade_id: int) -> None:
    """Flips the position from fixed target/SL management into trailing-exit
    mode. Idempotent."""
    table = _table(variant_id)
    now = _now().isoformat()
    with get_engine().begin() as conn:
        conn.execute(
            text(f"UPDATE {table} SET target_hit=1, updated_at=:now WHERE id=:id"),
            {"now": now, "id": trade_id},
        )


def close_trade(variant_id: str, trade_id: int, exit_price: float, exit_reason: str, exit_charges: float) -> None:
    table = _table(variant_id)
    now = _now().isoformat()
    with get_engine().begin() as conn:
        row = conn.execute(
            text(f"SELECT entry_price, quantity, capital_used, entry_charges FROM {table} WHERE id=:id"),
            {"id": trade_id},
        ).mappings().first()
        if row is None:
            raise ValueError(f"Trade {trade_id} not found in {table}")

        gross_pnl = (exit_price - row["entry_price"]) * row["quantity"]
        total_charges = row["entry_charges"] + exit_charges
        net_pnl = gross_pnl - total_charges
        net_pnl_pct = net_pnl / row["capital_used"] * 100

        conn.execute(
            text(f"""UPDATE {table} SET status='CLOSED', exit_time=:now, exit_price=:exit_price,
                    exit_reason=:exit_reason, exit_charges=:exit_charges, gross_pnl=:gross_pnl,
                    total_charges=:total_charges, net_pnl=:net_pnl, net_pnl_pct=:net_pnl_pct, updated_at=:now
                    WHERE id=:id"""),
            {"now": now, "exit_price": exit_price, "exit_reason": exit_reason, "exit_charges": exit_charges,
             "gross_pnl": gross_pnl, "total_charges": total_charges, "net_pnl": net_pnl,
             "net_pnl_pct": net_pnl_pct, "id": trade_id},
        )


def get_open_trade(variant_id: str) -> dict | None:
    table = _table(variant_id)
    with get_engine().connect() as conn:
        row = conn.execute(
            text(f"SELECT * FROM {table} WHERE status='OPEN' ORDER BY id DESC LIMIT 1")
        ).mappings().first()
        return dict(row) if row else None


def get_closed_trades(variant_id: str) -> pd.DataFrame:
    table = _table(variant_id)
    return pd.read_sql_query(
        text(f"SELECT * FROM {table} WHERE status='CLOSED' ORDER BY exit_time DESC"), get_engine()
    )


def get_all_trades(variant_id: str) -> pd.DataFrame:
    table = _table(variant_id)
    return pd.read_sql_query(text(f"SELECT * FROM {table} ORDER BY entry_time DESC"), get_engine())


def get_arm_cycles_used_today(variant_id: str, symbol: str) -> set[str]:
    table = _table(variant_id)
    today = _now().date().isoformat()
    with get_engine().connect() as conn:
        rows = conn.execute(
            text(f"SELECT arm_cycle_id FROM {table} "
                 f"WHERE symbol=:symbol AND entry_time LIKE :pattern AND arm_cycle_id IS NOT NULL"),
            {"symbol": symbol, "pattern": f"{today}%"},
        ).all()
        return {r[0] for r in rows}


def get_starting_capital(variant_id: str, default: float) -> float:
    """This variant's realized capital base = default starting capital + all of
    THIS variant's realized net P&L so far today. Each of the 12 variants is
    fully independent — no shared pool, per the sibling bots' established
    pattern for their own multi-variant designs."""
    table = _table(variant_id)
    today = _now().date().isoformat()
    with get_engine().connect() as conn:
        row = conn.execute(
            text(f"SELECT COALESCE(SUM(net_pnl), 0) AS total FROM {table} "
                 f"WHERE status='CLOSED' AND entry_time LIKE :pattern"),
            {"pattern": f"{today}%"},
        ).mappings().first()
        return default + (row["total"] or 0)


def log_cycle(status: str, stage: str = "", symbols_scanned: int = 0, message: str = "",
              warnings: str = "", error: str | None = None) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO ute_cycle_log (cycle_time, status, stage, symbols_scanned, message, warnings, error) "
                 "VALUES (:cycle_time, :status, :stage, :symbols_scanned, :message, :warnings, :error)"),
            {"cycle_time": _now().isoformat(), "status": status, "stage": stage,
             "symbols_scanned": symbols_scanned, "message": message, "warnings": warnings, "error": error},
        )


def get_cycle_logs(limit: int = 30) -> pd.DataFrame:
    return pd.read_sql_query(
        text("SELECT * FROM ute_cycle_log ORDER BY cycle_time DESC LIMIT :limit"),
        get_engine(), params={"limit": limit},
    )


def prune_cycle_logs(retention_days: int = 7) -> int:
    cutoff = (_now() - timedelta(days=retention_days)).isoformat()
    with get_engine().begin() as conn:
        result = conn.execute(
            text("DELETE FROM ute_cycle_log WHERE cycle_time < :cutoff"),
            {"cutoff": cutoff},
        )
        return result.rowcount


def get_setting(key: str, default: str | None = None) -> str | None:
    """Reads from ute_settings — the ONE thing admin and viewer actually
    share (see ute_settings' schema comment). Use this, not
    engine.config.load_settings(), for anything that must be visible to
    both apps."""
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT value FROM ute_settings WHERE key=:k"), {"k": key}).first()
        return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with get_engine().begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(
                text("INSERT INTO ute_settings (key, value) VALUES (:k, :v) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                {"k": key, "v": value},
            )
        else:
            conn.execute(text("DELETE FROM ute_settings WHERE key=:k"), {"k": key})
            conn.execute(text("INSERT INTO ute_settings (key, value) VALUES (:k, :v)"), {"k": key, "v": value})


def get_checkpoints_used_today(variant_id: str) -> set[str]:
    """Subh-30-min checkpoints (e.g. '09:20') already evaluated today FOR THIS
    variant_id. Only meaningful for subh30 variants — puradin variants never
    call this (they scan continuously, no checkpoint concept)."""
    today = _now().date().isoformat()
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT checkpoint_time FROM ute_checkpoint_log WHERE trade_date=:d AND variant_id=:v"),
            {"d": today, "v": variant_id},
        ).all()
        return {r[0] for r in rows}


def mark_checkpoint_used(variant_id: str, checkpoint_time: str, signal_found: bool,
                          symbol: str | None, trade_id: int | None = None) -> None:
    today = _now().date().isoformat()
    with get_engine().begin() as conn:
        conn.execute(
            text("INSERT INTO ute_checkpoint_log "
                 "(trade_date, checkpoint_time, executed_at, signal_found, symbol, trade_id, variant_id) "
                 "VALUES (:d, :c, :now, :sig, :sym, :tid, :v)"),
            {"d": today, "c": checkpoint_time, "now": _now().isoformat(),
             "sig": int(signal_found), "sym": symbol, "tid": trade_id, "v": variant_id},
        )


def reset_all_data() -> None:
    """Wipes all 12 variants' trades, cycle logs, and checkpoint logs —
    irreversible. Confirm-gated in the dashboard, never called automatically."""
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM ute_checkpoint_log"))
        for table in VARIANT_TABLES.values():
            conn.execute(text(f"DELETE FROM {table}"))
        conn.execute(text("DELETE FROM ute_cycle_log"))
