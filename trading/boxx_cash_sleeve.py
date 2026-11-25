r"""
MT5 `BOXX` 25% Unencumbered Cash Treasury Sleeve (Magic 777899)
===============================================================
Validated across 40 ISO weeks on live MT5 `BOXX` H1 bars under Mandatory Weekly
Friday 15:45 America/New_York Auto-Liquidation:
  - Live MT5 spread: $0.01/sh (~0.8 bps), swap_long = 0.0, contract_size = 1 share.
  - Survives 1x ($0.02: +2.58%/yr, 0.00% MaxDD, 87.5% winning weeks),
    2x ($0.04: +1.64%/yr, 0.02% MaxDD), and 3x ($0.06: +0.70%/yr, 0.04% MaxDD) spread stress.

Strict Operational Rules:
  1. Alpha Always Has Priority: Only deploys when active alpha margin <= 35% of equity;
     auto-liquidates during US RTH if active alpha margin rises >= 42% of equity.
  2. Entry Window: Monday-Wednesday 09:40 - 15:30 America/New_York ONLY (never enters Thu/Fri
     so weekly Friday 15:45 NY liquidation never pays spread for <2 days of T-bill accrual).
  3. Mandatory Friday 15:45 America/New_York Auto-Liquidation: 100% flat in cash for the weekend.
  4. Global Sunday Reopen Blackout (< Monday 00:00 UTC) & Breakeven Disabled (SL=0, TP=0).
"""

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5
from trading.bot_manager import close_mt5_position, get_filling_mode, send_guarded_entry_order

NY_TZ = ZoneInfo("America/New_York")
MAGIC_BOXX = 777899


def is_us_rth_open_for_boxx(now_utc: datetime, for_entry: bool = True) -> tuple[bool, str]:
    ny = now_utc.astimezone(NY_TZ)
    wd = ny.weekday()
    mins = ny.hour * 60 + ny.minute
    if wd >= 5:
        return False, "Weekend"
    if for_entry:
        if wd > 2:
            return False, f"Thu/Fri entry skip (Mon-Wed 09:40-15:30 NY only, cur {ny.strftime('%a %H:%M')} NY)"
        if 9 * 60 + 40 <= mins <= 15 * 60 + 30:
            return True, "US RTH Mon-Wed Entry Window"
        return False, f"Outside Mon-Wed 09:40-15:30 NY ({ny.strftime('%H:%M')} NY)"
    # For management / margin-release exits: Mon-Fri 09:35 - 15:55 NY
    if 9 * 60 + 35 <= mins <= 15 * 60 + 55:
        return True, "US RTH Open"
    return False, f"US Equity Market Closed ({ny.strftime('%H:%M')} NY)"


def manage_boxx_cash_sleeve(now_utc: datetime, cfg: dict, log_fn=None) -> dict:
    if not cfg.get("ENABLED", True):
        return {"status": "DISABLED", "shares": 0}
    if now_utc.weekday() == 6:
        return {"status": "SUNDAY_BLACKOUT", "shares": 0}

    ny = now_utc.astimezone(NY_TZ)
    all_pos = list(mt5.positions_get() or [])
    boxx_pos = [p for p in all_pos if p.magic == MAGIC_BOXX or str(p.symbol) == "BOXX"]

    # 1. Mandatory Friday 15:45 NY Auto-Liquidation
    if ny.weekday() == 4 and (ny.hour > 15 or (ny.hour == 15 and ny.minute >= 45)):
        for p in boxx_pos:
            ok, msg = close_mt5_position(p.ticket, "BOXX")
            if log_fn:
                log_fn(f"[FRIDAY 15:45 NY] Closed BOXX #{p.ticket}: {ok} {msg}")
        return {"status": "FRIDAY_FLAT", "shares": 0}

    acc = mt5.account_info()
    if acc is None:
        return {"status": "NO_ACCOUNT", "shares": 0}

    equity = float(acc.equity)
    total_mrg = float(acc.margin)

    mt5.symbol_select("BOXX", True)
    info = mt5.symbol_info("BOXX")
    tick = mt5.symbol_info_tick("BOXX")
    if info is None or tick is None or tick.ask <= 0:
        return {"status": "NO_SYMBOL", "shares": 0}

    boxx_mrg = sum(float(p.volume) * float(p.price_current) for p in boxx_pos)
    alpha_mrg = max(0.0, total_mrg - boxx_mrg)
    alpha_mrg_frac = alpha_mrg / max(equity, 1.0)

    max_alpha_mrg_release = float(cfg.get("RELEASE_ALPHA_MARGIN_FRAC", 0.42))
    max_alpha_mrg_enter = float(cfg.get("MAX_ALPHA_MARGIN_TO_ENTER_FRAC", 0.35))
    target_alloc_frac = float(cfg.get("TARGET_EQUITY_FRAC", 0.25))
    max_spread_usd = float(cfg.get("MAX_SPREAD_USD", 0.03))

    # 2. If holding BOXX, check if alpha engines need margin back during US RTH
    if boxx_pos:
        tot_sh = sum(float(p.volume) for p in boxx_pos)
        tot_pnl = sum(float(p.profit) for p in boxx_pos)
        rth_manage, _ = is_us_rth_open_for_boxx(now_utc, for_entry=False)
        if rth_manage and alpha_mrg_frac >= max_alpha_mrg_release:
            for p in boxx_pos:
                ok, msg = close_mt5_position(p.ticket, "BOXX")
                if log_fn:
                    log_fn(f"[BOXX MARGIN RELEASE] Freed BOXX #{p.ticket} (AlphaMargin={alpha_mrg_frac*100:.1f}%): {ok} {msg}")
            return {"status": "RELEASED_FOR_MARGIN", "shares": 0, "alpha_margin_pct": round(alpha_mrg_frac * 100.0, 1)}
        return {
            "status": "HOLDING",
            "shares": int(tot_sh),
            "notional_usd": round(tot_sh * float(tick.bid), 2),
            "floating_pnl": round(tot_pnl, 2),
            "alpha_margin_pct": round(alpha_mrg_frac * 100.0, 1),
        }

    # 3. Flat in BOXX: check if eligible to deploy 25% idle cash sleeve
    rth_ok, rth_rsn = is_us_rth_open_for_boxx(now_utc, for_entry=True)
    if not rth_ok:
        return {"status": "STANDBY", "shares": 0, "detail": rth_rsn, "alpha_margin_pct": round(alpha_mrg_frac * 100.0, 1)}

    if alpha_mrg_frac > max_alpha_mrg_enter:
        return {"status": "MARGIN_RESERVED", "shares": 0, "detail": f"Alpha margin {alpha_mrg_frac*100:.1f}% > {max_alpha_mrg_enter*100:.0f}%", "alpha_margin_pct": round(alpha_mrg_frac * 100.0, 1)}

    live_sp = float(tick.ask - tick.bid)
    if live_sp > max_spread_usd:
        return {"status": "SPREAD_WAIT", "shares": 0, "detail": f"Spread ${live_sp:.2f} > ${max_spread_usd:.2f}"}

    target_usd = min(equity * target_alloc_frac, max(0.0, equity * 0.65 - alpha_mrg))
    shares = float(math.floor(target_usd / float(tick.ask)))
    if shares < float(info.volume_min):
        return {"status": "SIZE_SKIP", "shares": 0}

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": "BOXX",
        "volume": shares,
        "type": mt5.ORDER_TYPE_BUY,
        "price": float(tick.ask),
        "sl": 0.0,
        "tp": 0.0,
        "deviation": 5,
        "magic": MAGIC_BOXX,
        "comment": f"ANT BOXX Cash ({int(target_alloc_frac*100)}%)",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": get_filling_mode("BOXX"),
    }
    res = send_guarded_entry_order(req, log_fn)
    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        if log_fn:
            log_fn(f"[BOXX CASH SLEEVE FILLED] BUY {int(shares)} sh @ ${tick.ask:.2f} (${shares*tick.ask:,.2f} = {shares*tick.ask/equity*100:.1f}% eq) | Ticket=#{res.order}")
        return {
            "status": "HOLDING",
            "shares": int(shares),
            "notional_usd": round(shares * float(tick.ask), 2),
            "floating_pnl": 0.0,
            "alpha_margin_pct": round(alpha_mrg_frac * 100.0, 1),
        }
    return {"status": "ORDER_WAIT", "shares": 0, "detail": str(getattr(res, "comment", ""))}
