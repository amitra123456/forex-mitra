"""
Persistent Cooldown Manager for ANT_FOREX Execution Engines.
Guarantees that post-exit cooldowns (e.g. 15-minute / 1-bar pause) persist across:
1. Bot process restarts (by Watchdog or manually)
2. Operating System / Server reboots
3. MT5 disconnects / reconnects

Mechanisms:
- Queries active state file on disk (`trading/cooldown_state.json`)
- Queries broker terminal deal history (`history_deals_get`) comparing broker tick time
  against the latest closed exit deal (entry in [1, 3]) for the matching symbol/magic.
"""

import sys
import time
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional, Union, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

COOLDOWN_STATE_FILE = PROJECT_ROOT / "trading" / "cooldown_state.json"


def _ensure_mt5():
    """Ensure MetaTrader5 is initialized."""
    try:
        from trading.mt5_feed import initialize_mt5
        if initialize_mt5():
            import MetaTrader5 as mt5
            return mt5
    except Exception:
        pass
    try:
        import MetaTrader5 as mt5
        if mt5.terminal_info() is None:
            if mt5.initialize():
                return mt5
        else:
            return mt5
    except Exception:
        pass
    return None


def save_cooldown_to_disk(symbol: str, engine: str, cooldown_expiry_ts: float):
    """Persist active cooldown expiry to disk for crash/restart recovery."""
    try:
        data = {}
        if COOLDOWN_STATE_FILE.exists():
            try:
                with open(COOLDOWN_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        key = f"{symbol}_{engine}"
        data[key] = {
            "symbol": symbol,
            "engine": engine,
            "expiry_ts": float(cooldown_expiry_ts),
            "expiry_iso": datetime.fromtimestamp(cooldown_expiry_ts).strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # Write atomically or directly
        with open(COOLDOWN_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def activate_cooldown(symbol: str, engine: str, cooldown_seconds: float = 900.0) -> float:
    """
    Activates cooldown for an engine on symbol, persists to disk, and returns the expiry timestamp.
    """
    expiry_ts = time.time() + float(cooldown_seconds)
    save_cooldown_to_disk(symbol, engine, expiry_ts)
    return expiry_ts


def get_remaining_cooldown(
    symbol: str,
    magic: Union[int, List[int]] = 0,
    cooldown_seconds: float = 900.0,
    engine_name: str = ""
) -> float:
    """
    Checks MT5 deal history and local disk state for the latest closed position matching symbol and magic.
    Returns remaining cooldown seconds (> 0 if locked out, 0.0 if clear).
    Works reliably across all bot and server restarts.
    """
    rem_disk = 0.0

    # 1. Check disk state
    key = f"{symbol}_{engine_name}" if engine_name else None
    if key and COOLDOWN_STATE_FILE.exists():
        try:
            with open(COOLDOWN_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if key in data:
                exp = float(data[key].get("expiry_ts", 0.0))
                now_ts = time.time()
                if exp > now_ts:
                    rem_disk = exp - now_ts
        except Exception:
            pass

    # 2. Check MT5 Deal History directly
    rem_mt5 = 0.0
    mt5 = _ensure_mt5()
    if mt5 is not None:
        try:
            # Query recent deal history (2 days prior to 2 days ahead to cover all broker timezones)
            now = datetime.now()
            from_time = now - timedelta(days=2)
            to_time = now + timedelta(days=2)
            deals = mt5.history_deals_get(from_time, to_time)
            if deals:
                target_magics = [magic] if isinstance(magic, int) else list(magic)
                # entry == 1 (DEAL_ENTRY_OUT), entry == 3 (DEAL_ENTRY_OUT_BY)
                if target_magics and target_magics[0] > 0:
                    exit_deals = [
                        d for d in deals
                        if d.symbol == symbol and d.magic in target_magics and d.entry in (1, 3)
                    ]
                else:
                    exit_deals = [
                        d for d in deals
                        if d.symbol == symbol and d.entry in (1, 3)
                    ]

                if exit_deals:
                    from trading.mt5_feed import mt5_ts_to_utc_epoch
                    latest_deal = max(exit_deals, key=lambda d: d.time)
                    deal_utc_ts = mt5_ts_to_utc_epoch(latest_deal.time)
                    elapsed_sec = max(0.0, time.time() - deal_utc_ts)
                    if elapsed_sec < cooldown_seconds:
                        rem_mt5 = float(cooldown_seconds - elapsed_sec)
        except Exception:
            pass

    # Return the maximum of disk or MT5 query
    remaining = max(rem_disk, rem_mt5, 0.0)

    # Sync back to disk if MT5 discovered an active cooldown that wasn't on disk
    if remaining > 0 and engine_name and (remaining > rem_disk + 1.0):
        save_cooldown_to_disk(symbol, engine_name, time.time() + remaining)

    return remaining


def is_in_cooldown(
    symbol: str,
    magic: Union[int, List[int]] = 0,
    cooldown_seconds: float = 900.0,
    engine_name: str = ""
) -> Tuple[bool, float]:
    """
    Returns (in_cooldown, remaining_seconds).
    """
    rem = get_remaining_cooldown(symbol, magic, cooldown_seconds, engine_name)
    return (rem > 0.0, rem)


def get_cooldown_expiry_timestamp(
    symbol: str,
    magic: Union[int, List[int]] = 0,
    cooldown_seconds: float = 900.0,
    engine_name: str = ""
) -> float:
    """
    Returns the absolute epoch timestamp (time.time() + remaining) when the cooldown expires.
    If no cooldown is active, returns current time.time().
    """
    rem = get_remaining_cooldown(symbol, magic, cooldown_seconds, engine_name)
    return time.time() + rem
