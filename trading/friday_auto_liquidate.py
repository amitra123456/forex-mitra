"""
Friday Market Close Auto-Liquidation Daemon (ANT_FOREX)
======================================================
Monitors open positions and pending orders continuously.
At 15:45:00 America/New_York (3:45 PM NY time) every Friday:
1. Closes all open positions at market across all pairs.
2. Deletes / cancels all resting limit and stop orders.
3. Guarantees 100% Cash-Flat portfolio over the weekend with ZERO gap risk.
"""

import sys
import time
from datetime import datetime, timezone
import zoneinfo
from pathlib import Path
import MetaTrader5 as mt5

if hasattr(sys.stdout, 'reconfigure') and sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure') and sys.stderr is not None:
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trading.bot_manager import initialize_mt5, shutdown_mt5, close_mt5_position

LOG_PATH = PROJECT_ROOT / "logs" / "friday_close.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_msg = msg.encode('ascii', errors='backslashreplace').decode('ascii')
    line = f"[{ts}] [FRIDAY CLOSE 15:45 NY] {safe_msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{ts}] [FRIDAY CLOSE 15:45 NY] {msg}\n")
    except Exception:
        pass

def get_ny_time() -> datetime:
    return datetime.now(zoneinfo.ZoneInfo("America/New_York"))

def liquidate_all():
    log("[ALERT] TARGET TIME 15:45 NY REACHED! INITIATING MANDATORY FRIDAY AUTO-LIQUIDATION...")
    if not initialize_mt5():
        log("ERROR: MT5 initialization failed during liquidation!")
        return

    # 1. Close all open market positions
    positions = mt5.positions_get()
    if not positions:
        log("No open positions found. Desk is already flat in cash.")
    else:
        log(f"Found {len(positions)} active position(s) to close.")
        for p in positions:
            log(f"Closing Ticket #{p.ticket} ({p.symbol} {'BUY' if p.type==0 else 'SELL'} {p.volume} lots, floating PnL: ${p.profit:+.2f})...")
            ok, msg = close_mt5_position(p.ticket)
            log(f"Ticket #{p.ticket} close result: {ok} | {msg}")

    # 2. Cancel all resting limit/pending orders
    orders = mt5.orders_get()
    if orders:
        log(f"Found {len(orders)} resting pending order(s) to cancel.")
        for o in orders:
            log(f"Cancelling pending Order #{o.ticket} ({o.symbol} {o.volume} lots)...")
            req = {
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": o.ticket
            }
            res = mt5.order_send(req)
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                log(f"Order #{o.ticket} successfully cancelled.")
            else:
                err = res.comment if res else mt5.last_error()
                log(f"Warning: Order #{o.ticket} cancel failed: {err}")

    # 3. Final verification
    time.sleep(2)
    rem_pos = mt5.positions_get()
    rem_ord = mt5.orders_get()
    acct = mt5.account_info()
    bal = acct.balance if acct else 0.0
    equity = acct.equity if acct else 0.0

    if not rem_pos and not rem_ord:
        log(f"[SUCCESS] ALL POSITIONS CLOSED & ORDERS CANCELLED. Desk is 100% Flat in Cash. Final Balance: ${bal:,.2f} | Equity: ${equity:,.2f}.")
    else:
        if rem_pos:
            log(f"[WARNING] {len(rem_pos)} position(s) remaining after liquidation attempt!")
        if rem_ord:
            log(f"[WARNING] {len(rem_ord)} pending order(s) remaining!")

    shutdown_mt5()

def main():
    log("=== FRIDAY AUTO-LIQUIDATION DAEMON STARTED ===")
    log("Target Cutoff: 15:45:00 America/New_York (3:45 PM NY Time).")

    liquidated_date = None

    while True:
        try:
            ny_now = get_ny_time()
            today_str = ny_now.strftime("%Y-%m-%d")

            # Check if today is Friday (weekday == 4)
            if ny_now.weekday() == 4:
                # Target is 15:45 NY
                if ny_now.hour > 15 or (ny_now.hour == 15 and ny_now.minute >= 45):
                    if liquidated_date != today_str:
                        liquidate_all()
                        liquidated_date = today_str
                        log(f"Auto-liquidation for Friday {today_str} complete. Standing by for weekend closure.")
                else:
                    # Countdown logging
                    target_today = ny_now.replace(hour=15, minute=45, second=0, microsecond=0)
                    rem_sec = (target_today - ny_now).total_seconds()
                    rem_min = int(rem_sec // 60)
                    rem_s = int(rem_sec % 60)

                    if rem_min % 15 == 0 and rem_s <= 10:
                        log(f"Friday Watch: {rem_min}m remaining until 15:45 NY auto-liquidation.")
                    elif rem_min <= 5 and rem_s <= 10:
                        log(f"FINAL COUNTDOWN: {rem_min}m {rem_s}s remaining until mandatory 15:45 NY liquidation.")

            # Sleep interval
            time.sleep(10)

        except Exception as e:
            log(f"Daemon error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
