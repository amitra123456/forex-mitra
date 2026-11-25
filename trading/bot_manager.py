"""
Bot Manager and MT5 Desk Operations (ANT_FOREX)
Handles background execution loop, MT5 order management, positions, account diagnostics, and logs.
"""

import os
import sys
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Tuple, Optional, List, Dict, Any
import psutil
import MetaTrader5 as mt5
from trading.market_hours import is_market_open

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "execution.log"


def initialize_mt5() -> bool:
    """Initialize connection to local MetaTrader 5 terminal if not already initialized."""
    if not mt5.initialize():
        return False
    return True


def shutdown_mt5():
    """Close connection to MT5 terminal."""
    try:
        mt5.shutdown()
    except Exception:
        pass


def find_bot_processes() -> List[psutil.Process]:
    """Find all running processes executing execution_loop.py."""
    matches = []
    current_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.pid == current_pid:
                continue
            pname = (proc.info.get("name") or "").lower()
            if "python" not in pname:
                continue
            cmdline = proc.info.get("cmdline")
            if cmdline and any("execution_loop.py" in str(c) for c in cmdline):
                if any("psutil" in str(c) for c in cmdline):
                    continue
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return matches


def is_bot_running() -> Tuple[bool, Optional[int]]:
    """Check if the execution loop bot is currently running. Returns (is_running, pid)."""
    procs = find_bot_processes()
    if procs:
        return True, procs[0].pid
    return False, None


def start_bot(symbols: List[str] = None, timeframe: str = "M15", lot: float = None, sl_pips: float = None, tp_pips: float = None, sleep_sec: int = 60) -> Tuple[bool, str]:
    """Launch the execution_loop bot in the background."""
    is_running, pid = is_bot_running()
    if is_running:
        return False, f"Bot is already running (PID {pid})."

    script_path = PROJECT_ROOT / "trading" / "execution_loop.py"
    if not script_path.exists():
        return False, f"Script not found: {script_path}"

    if isinstance(symbols, str):
        symbols_arg = symbols
    elif isinstance(symbols, (list, tuple)):
        symbols_arg = ",".join(symbols)
    else:
        symbols_arg = "EURUSD,GBPUSD,USDJPY,AUDUSD,GBPJPY,EURJPY,USDCAD,XAUUSD,AUDNZD"
    cmd = [
        sys.executable,
        "-u",
        str(script_path),
        "--symbols", symbols_arg,
        "--timeframe", str(timeframe),
        "--sleep", str(sleep_sec)
    ]
    if lot is not None:
        cmd.extend(["--lot", str(lot)])
    if sl_pips is not None:
        cmd.extend(["--sl", str(sl_pips)])
    if tp_pips is not None:
        cmd.extend(["--tp", str(tp_pips)])

    try:
        with open(LOG_FILE, "a", encoding="utf-8") as log_f:
            log_f.write(f"\n--- [BOT LAUNCH {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ---\n")
            log_f.write(f"Command: {' '.join(cmd)}\n")

        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

        log_f = open(LOG_FILE, "a", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
            creationflags=flags
        )
        return True, f"Bot successfully launched (PID {proc.pid})."
    except Exception as e:
        return False, f"Failed to start bot: {str(e)}"


def stop_bot() -> Tuple[bool, str]:
    """Gracefully terminate running execution_loop bot processes."""
    procs = find_bot_processes()
    if not procs:
        return True, "No active bot processes found."

    killed = 0
    for proc in procs:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
            killed += 1
        except Exception:
            pass

    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n--- [BOT TERMINATED {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ---\n")

    return True, f"Stopped {killed} bot process(es)."


def get_recent_logs(lines: int = 50) -> str:
    """Read the last N lines from the execution log."""
    if not LOG_FILE.exists():
        return "Log file empty. Start bot to generate execution telemetry."
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            return "".join(all_lines[-lines:])
    except Exception as e:
        return f"Error reading log file: {e}"


def get_mt5_account_info() -> Dict[str, Any]:
    """Retrieve MT5 terminal account statistics."""
    if not initialize_mt5():
        return {"connected": False, "error": "MT5 terminal not running or connection failed"}

    info = mt5.account_info()
    if info is None:
        return {"connected": False, "error": f"Failed to fetch account info: {mt5.last_error()}"}

    return {
        "connected": True,
        "login": info.login,
        "server": info.server,
        "balance": float(info.balance),
        "equity": float(info.equity),
        "margin": float(info.margin),
        "free_margin": float(info.margin_free),
        "margin_level": float(info.margin_level) if info.margin_level else 0.0,
        "profit": float(info.profit),
        "leverage": int(info.leverage),
        "currency": info.currency,
        "trade_allowed": bool(info.trade_allowed),
        "trade_expert": bool(info.trade_expert),
    }


def get_mt5_open_positions() -> List[Dict[str, Any]]:
    """Fetch live open positions from MT5."""
    if not initialize_mt5():
        return []

    positions = mt5.positions_get()
    if positions is None:
        return []

    results = []
    for p in positions:
        is_jpy = "JPY" in p.symbol
        pip_mult = 100.0 if is_jpy else 10000.0
        order_type_str = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"

        # Pip PnL calculation
        if p.type == mt5.ORDER_TYPE_BUY:
            pips = (p.price_current - p.price_open) * pip_mult
        else:
            pips = (p.price_open - p.price_current) * pip_mult

        results.append({
            "ticket": int(p.ticket),
            "symbol": p.symbol,
            "type": order_type_str,
            "volume": float(p.volume),
            "price_open": float(p.price_open),
            "sl": float(p.sl),
            "tp": float(p.tp),
            "price_current": float(p.price_current),
            "pips": round(float(pips), 1),
            "profit": round(float(p.profit), 2),
            "swap": float(p.swap),
            "time": datetime.fromtimestamp(p.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "comment": p.comment,
        })
    return results


def get_filling_mode(symbol: str) -> int:
    """Detect broker-supported filling mode for a symbol (FOK vs IOC)."""
    info = mt5.symbol_info(symbol)
    if not info:
        return mt5.ORDER_FILLING_IOC
    filling = info.filling_mode
    if filling & 1:
        return mt5.ORDER_FILLING_FOK
    elif filling & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def close_mt5_position(ticket: int, symbol: Optional[str] = None) -> Tuple[bool, str]:
    """Close an open position by ticket."""
    if not initialize_mt5():
        return False, "MT5 not initialized"

    pos = mt5.positions_get(ticket=ticket)
    if not pos or len(pos) == 0:
        return False, f"Position #{ticket} not found."

    p = pos[0]
    symbol = p.symbol
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False, f"Failed to get tick for {symbol}."

    order_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": p.volume,
        "type": order_type,
        "position": ticket,
        "price": price,
        "deviation": 10,
        "magic": p.magic if p.magic else 999999,
        "comment": f"ANT Bot Close #{ticket}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(symbol),
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return False, f"Close failed: [{result.retcode}] {result.comment}"
    return True, f"Position #{ticket} closed successfully at {price}."


def get_engine_family(magic: int) -> str:
    """Maps a bot magic number to its logical engine family (allowing 0.5%+0.5% split companion legs)."""
    m = int(magic or 0)
    if 999000 <= m < 1000000:
        return "CENTURION"
    if m == 777880:
        return "CROSS_80H"
    if m == 777490:
        return "GBPJPY_PURGED_WF"
    if m == 777390:
        return "EURUSD_BOND_CONV"
    if m == 777899:
        return "BOXX_CASH_SLEEVE"
    rem = m % 100
    if m == 888881 or rem in (1, 4):
        return "M1_M3"
    if m == 888882 or rem in (2, 3):
        return "SM1"
    if rem in (50, 51):
        return "SNIPER"
    if rem == 85:
        return "EEI"
    if rem == 90:
        return "VACUUM"
    return f"MAGIC_{m}"


def _order_type_side(order_type: int) -> Optional[str]:
    if order_type in (
        mt5.ORDER_TYPE_BUY,
        mt5.ORDER_TYPE_BUY_LIMIT,
        mt5.ORDER_TYPE_BUY_STOP,
        mt5.ORDER_TYPE_BUY_STOP_LIMIT,
    ):
        return "BUY"
    if order_type in (
        mt5.ORDER_TYPE_SELL,
        mt5.ORDER_TYPE_SELL_LIMIT,
        mt5.ORDER_TYPE_SELL_STOP,
        mt5.ORDER_TYPE_SELL_STOP_LIMIT,
    ):
        return "SELL"
    return None


def send_guarded_entry_order(req: Dict[str, Any], log_fn=None):
    """
    Central execution guard for all live entry orders across all 8 bots:
      1. Pass-through for closes (`position` set), SL/TP updates (`TRADE_ACTION_SLTP`), and cancellations (`TRADE_ACTION_REMOVE`).
      2. Enforces universal market-open & DST-aware 16:45-19:00 NY daily rollover blackout (`is_market_open`).
      3. Blocks duplicate same-direction stacking across different sub-engines on the same symbol (while permitting
         companion 0.5%+0.5% market+limit split legs within the same engine family).
      4. Prevents opposing long+short self-hedging on the same symbol: if an incoming EEI expansion swing (`*85`)
         opposes a stale lower-timeframe M1_M3/SM1/VACUUM position, closes the opposing leg(s) first; otherwise
         vetoes opposing entries against active positions.
    """
    from types import SimpleNamespace

    action = req.get("action")
    if req.get("position") or action not in (mt5.TRADE_ACTION_DEAL, mt5.TRADE_ACTION_PENDING):
        return mt5.order_send(req)

    symbol = str(req.get("symbol", ""))
    magic = int(req.get("magic", 0) or 0)
    prop_side = _order_type_side(int(req.get("type", -1)))
    prop_fam = get_engine_family(magic)

    # 1. Universal Market Open + Daily Rollover Shield (16:45-19:00 NY) + Weekend/Friday 15:45 NY Guard
    is_open, mkt_reason, _ = is_market_open(symbol)
    if not is_open:
        msg = f"[ROLLOVER/MARKET VETO] Blocked {symbol} {prop_side} ({prop_fam}): {mkt_reason}"
        if log_fn:
            log_fn(msg)
        return SimpleNamespace(retcode=10018, order=0, deal=0, comment=msg)

    if prop_side is not None:
        live_pos = list(mt5.positions_get(symbol=symbol) or [])
        live_ord = list(mt5.orders_get(symbol=symbol) or [])

        same_dir_other_fam = []
        opp_pos = []
        opp_ord = []

        for p in live_pos:
            p_side = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
            p_fam = get_engine_family(p.magic)
            if p_side == prop_side:
                if p_fam != prop_fam or p.magic == magic:
                    same_dir_other_fam.append(f"Pos#{p.ticket}({p_fam})")
            else:
                opp_pos.append((p, p_fam))

        for o in live_ord:
            o_side = _order_type_side(o.type)
            o_fam = get_engine_family(o.magic)
            if o_side == prop_side:
                if o_fam != prop_fam or o.magic == magic:
                    same_dir_other_fam.append(f"Ord#{o.ticket}({o_fam})")
            elif o_side is not None:
                opp_ord.append((o, o_fam))

        # 2. Block duplicate same-direction stacking across sub-engines (or duplicate same-magic order)
        if same_dir_other_fam:
            msg = f"[STACKING VETO] Blocked duplicate {symbol} {prop_side} ({prop_fam}); already active: {', '.join(same_dir_other_fam)}"
            if log_fn:
                log_fn(msg)
            return SimpleNamespace(retcode=10044, order=0, deal=0, comment=msg)

        # 3. Prevent simultaneous long+short self-hedging on the same symbol
        if opp_pos or opp_ord:
            can_reverse = (
                prop_fam == "EEI"
                and all(f in ("M1_M3", "SM1", "VACUUM") for _, f in opp_pos)
                and all(f in ("M1_M3", "SM1", "VACUUM") for _, f in opp_ord)
            )
            if can_reverse:
                for p, p_fam in opp_pos:
                    ok, cmsg = close_mt5_position(p.ticket, symbol)
                    if log_fn:
                        log_fn(f"[HYGIENE NETTING] Closed stale opposing {symbol} #{p.ticket} ({p_fam}) before EEI {prop_side}: {cmsg}")
                    if not ok:
                        abort_msg = f"[SELF-HEDGE VETO] Aborted EEI {prop_side} on {symbol}; failed to close opposing #{p.ticket}: {cmsg}"
                        if log_fn:
                            log_fn(abort_msg)
                        return SimpleNamespace(retcode=10044, order=0, deal=0, comment=abort_msg)
                for o, o_fam in opp_ord:
                    mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                    if log_fn:
                        log_fn(f"[HYGIENE NETTING] Cancelled opposing pending order #{o.ticket} ({o_fam}) before EEI {prop_side}")
            else:
                opp_desc = [f"Pos#{p.ticket}({f})" for p, f in opp_pos] + [f"Ord#{o.ticket}({f})" for o, f in opp_ord]
                msg = f"[SELF-HEDGE VETO] Blocked opposing {symbol} {prop_side} ({prop_fam}) against {', '.join(opp_desc)}"
                if log_fn:
                    log_fn(msg)
                return SimpleNamespace(retcode=10044, order=0, deal=0, comment=msg)

        # 4. Cross-Pair JPY Factor Direction Mutex (prevents simultaneous BUY XXXJPY + SELL YYYJPY synthetic cross spread burn)
        if symbol.endswith("JPY"):
            all_pos = list(mt5.positions_get() or [])
            jpy_conflicts = []
            for p in all_pos:
                psym = str(getattr(p, "symbol", ""))
                if psym.endswith("JPY") and psym != symbol:
                    p_side = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
                    if p_side != prop_side:
                        jpy_conflicts.append(f"{psym}:{p_side}#{p.ticket}({get_engine_family(p.magic)})")
            if jpy_conflicts:
                msg = f"[CROSS-PAIR JPY MUTEX VETO] Blocked {symbol} {prop_side} ({prop_fam}); opposes active JPY exposure {', '.join(jpy_conflicts)}"
                if log_fn:
                    log_fn(msg)
                return SimpleNamespace(retcode=10044, order=0, deal=0, comment=msg)

    return mt5.order_send(req)


def emergency_flatten_all() -> Tuple[int, List[str]]:
    """Emergency close all active open positions."""
    if not initialize_mt5():
        return 0, ["MT5 connection failed."]

    positions = mt5.positions_get()
    if not positions:
        return 0, ["No open positions found."]

    closed = 0
    msgs = []
    for p in positions:
        ok, msg = close_mt5_position(p.ticket)
        if ok:
            closed += 1
        msgs.append(f"#{p.ticket} ({p.symbol}): {msg}")

    return closed, msgs


def place_manual_order(symbol: str, is_buy: bool, volume: float, sl_pips: float, tp_pips: float) -> Tuple[bool, str]:
    """Place a manual test or discretionary paper order via MT5 with pip-based SL/TP."""
    if not initialize_mt5():
        return False, "MT5 not connected."

    is_open, mkt_reason, _ = is_market_open(symbol)
    if not is_open:
        return False, f"Order blocked by Market Hours Guard: {mkt_reason}"

    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return False, f"Failed to retrieve tick for {symbol}."

    is_xau = "XAU" in symbol or "GOLD" in symbol
    is_jpy = "JPY" in symbol
    pip_mult = 10.0 if is_xau else (100.0 if is_jpy else 10000.0)
    digits = 2 if is_xau else (3 if is_jpy else 5)

    order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
    price = tick.ask if is_buy else tick.bid

    if is_buy:
        sl = price - (sl_pips / pip_mult) if sl_pips > 0 else 0.0
        tp = price + (tp_pips / pip_mult) if tp_pips > 0 else 0.0
    else:
        sl = price + (sl_pips / pip_mult) if sl_pips > 0 else 0.0
        tp = price - (tp_pips / pip_mult) if tp_pips > 0 else 0.0

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": price,
        "sl": round(sl, digits),
        "tp": round(tp, digits),
        "deviation": 10,
        "magic": 999999,
        "comment": "ANT_FOREX Desk Order",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode(symbol),
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return False, f"Order rejected: [{result.retcode}] {result.comment}"
    return True, f"Order filled: Ticket #{result.order} | {symbol} {'BUY' if is_buy else 'SELL'} {volume} lots @ {price}"


def get_mt5_today_deals() -> List[Dict[str, Any]]:
    """Retrieve closed trades executed today from MT5 history deals."""
    if not initialize_mt5():
        return []

    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.now(ZoneInfo("America/New_York"))
        start_of_day_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        from_ts = int(start_of_day_et.timestamp())
        to_ts = int(now_et.timestamp()) + 86400
    except Exception:
        now = datetime.now(timezone.utc)
        from_ts = int((now - timedelta(hours=24)).timestamp())
        to_ts = int(now.timestamp()) + 86400

    try:
        deals = mt5.history_deals_get(from_ts, to_ts)
        if deals is None:
            return []

        closed_deals = []
        for d in deals:
            # Check if deal is an exit (closing out a position)
            if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT and d.symbol:
                is_jpy = "JPY" in d.symbol
                pip_mult = 1.0 if "XAU" in d.symbol else (100.0 if is_jpy else 10000.0)
                closed_deals.append({
                    "ticket": int(d.ticket),
                    "order": int(d.order),
                    "symbol": d.symbol,
                    "type": "SELL" if d.type == mt5.DEAL_TYPE_SELL else "BUY",
                    "volume": float(d.volume),
                    "price": float(d.price),
                    "profit": float(d.profit),
                    "swap": float(d.swap),
                    "commission": float(d.commission),
                    "time": datetime.fromtimestamp(d.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "comment": d.comment
                })
        return closed_deals
    except Exception:
        return []


from trading.cooldown_manager import (
    save_cooldown_to_disk,
    activate_cooldown,
    get_remaining_cooldown,
    is_in_cooldown,
    get_cooldown_expiry_timestamp,
    COOLDOWN_STATE_FILE
)

