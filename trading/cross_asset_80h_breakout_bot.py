r"""
Cross-Asset 80h Donchian Breakout + 20d/60d Trend + Live MT5 Positive-Carry Engine
==================================================================================
Production implementation of the audited Ref T12 structural trend-breakout system:
  - Trades diversified macro trend markets ONLY (Precious Metals: XAUUSD, XAUEUR;
    JPY Monetary-Divergence FX: USDJPY, EURJPY, GBPJPY, AUDJPY;
    US Equities: IVV, XLK; US Long Treasury Bonds: TLT; Crude Oil Energy: USO).
    Mean-reverting non-JPY G10 FX crosses (EURUSD, GBPUSD, AUDUSD, USDCAD) are excluded.
  - Evaluates strictly on completed H1 bars (signals from bar t close execute at bar t+1 open).
  - 24h CFD markets: 80h channel + 20d(480h)/60d(1440h) trend z-score > 0.30, Mon-Thu 06:00-16:00 UTC.
  - US Cash ETF markets (7 RTH H1 bars/day): calendar-equivalent 24-bar channel + 20d(140b)/60d(420b) trend,
    executed during NYSE/Nasdaq regular hours (09:35-15:50 America/New_York, Mon-Thu).
  - Stops & Exits: 2.5x ATR initial stop + continuous 4.0x ATR_entry ratcheting trailing stop.
    Breakeven protection is PERMANENTLY DISABLED per GEMINI.md.
  - Live MT5 Swap/Carry Gate: queries live mt5.symbol_info swap_long/swap_short, vetoes punitive
    negative-swap entries (< -0.020R/night unless |trend_z| >= 1.25), and ranks simultaneous
    breakouts by |trend_z| + 15 * swap_r_per_night to favor positive-carry & zero-swap trades.
  - Enforces Global Sunday Reopen Blackout (< Mon 00:00 UTC), Mon-Thu 16:45-19:00 NY Rollover Shield,
    and Mandatory Friday 15:45 America/New_York Auto-Liquidation across all open positions.
"""

import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import MetaTrader5 as mt5
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure") and sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from trading.mt5_feed import initialize_mt5, shutdown_mt5, fetch_candles, mt5_ts_to_utc_epoch
from trading.bot_manager import get_filling_mode, close_mt5_position, send_guarded_entry_order
from trading.market_hours import is_market_open
from trading.external_macro_feed import (
    get_live_macro_snapshot,
    compute_jpy_macro_score,
    evaluate_fomc_bond_confirmed_drift,
)
from trading.portfolio_risk_governor import get_governed_risk_state

NY_TZ = ZoneInfo("America/New_York")
CONFIG_PATH = PROJECT_ROOT / "trading" / "cross_asset_config.json"
STATE_PATH = PROJECT_ROOT / "trading" / "cross_asset_80h_state.json"
TELEMETRY_PATH = PROJECT_ROOT / "trading" / "telemetry_CROSS_ASSET_80H.json"
LOG_PATH = PROJECT_ROOT / "logs" / "cross_asset_80h_bot.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [CROSS_80H] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


class CrossAsset80hBreakoutEngine:
    def __init__(self):
        self.cfg = self.load_config()
        self.magic = int(self.cfg.get("MAGIC_BASE", 777880))
        self.state = self.load_state()
        self.last_eval_bar = {}
        log(f"Initialized Cross-Asset 80h Donchian + 20/60d Trend Engine (Magic={self.magic})")

    def load_config(self) -> dict:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def load_state(self) -> dict:
        if STATE_PATH.exists():
            try:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_state(self):
        try:
            tmp = STATE_PATH.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=2)
            tmp.replace(STATE_PATH)
        except Exception:
            pass

    def is_us_etf_rth_open(self, now_utc: datetime, for_entry: bool = True) -> tuple:
        dt_ny = now_utc.astimezone(NY_TZ)
        wd = dt_ny.weekday()
        if wd >= 5:
            return False, "Weekend closure"
        if for_entry and wd == 4:
            return False, "Friday entry cutoff (Mon-Thu entries only)"
        mins = dt_ny.hour * 60 + dt_ny.minute
        # NYSE/Nasdaq regular hours 09:30-16:00 NY; allow fills/updates 09:35-15:50 NY (15:45 Fri liquidation handled first)
        if for_entry and not (9 * 60 + 35 <= mins <= 15 * 60 + 30):
            return False, f"Outside US ETF entry window (09:35-15:30 NY, cur {dt_ny.strftime('%H:%M')} NY)"
        if not for_entry and not (9 * 60 + 31 <= mins <= 15 * 60 + 58):
            return False, "US ETF market closed"
        return True, "US ETF RTH Open"

    def estimate_swap_r_per_night(self, sym: str, info, direction: int, sl_dist: float) -> float:
        if info is None or sl_dist <= 0:
            return 0.0
        sw_pts = float(info.swap_long if direction == 1 else info.swap_short)
        if int(info.swap_mode) == 1:  # SYMBOL_SWAP_MODE_POINTS
            return (sw_pts * float(info.point)) / sl_dist
        return 0.0

    def dollar_per_price_unit_per_lot(self, sym: str, info) -> float:
        # Verified broker quirk: MT5 reports XAUUSD tick_value=0.10 for tick_size=0.01 on a 100oz contract ($100/pt)
        if sym.upper() == "XAUUSD":
            return 100.0
        if info.trade_tick_size > 0 and info.trade_tick_value > 0:
            return float(info.trade_tick_value) / float(info.trade_tick_size)
        return float(info.trade_contract_size or 1.0)

    def calculate_lot_and_margin(self, sym: str, asset_cfg: dict, sl_dist: float, entry_px: float, direction: int, equity: float, gov_state: dict = None):
        info = mt5.symbol_info(sym)
        if info is None or sl_dist <= 0 or entry_px <= 0:
            return 0.0, 0.0, 0.0

        sizing_cap = float(gov_state["sizing_capital"]) if (gov_state and gov_state.get("sizing_capital")) else equity
        eff_risk_pct = float(gov_state["effective_risk_pct"]) if (gov_state and gov_state.get("enabled")) else float(self.cfg.get("RISK_PCT_PER_TRADE", 0.80))
        risk_pct = eff_risk_pct / 100.0
        target_risk_usd = sizing_cap * risk_pct
        dpp = self.dollar_per_price_unit_per_lot(sym, info)
        raw_lots = target_risk_usd / max(sl_dist * dpp, 1e-6)

        # For 1:1 cash-margin ETFs, cap notional at 18% of sizing capital AND <= 25% of actual MT5 cash equity
        if asset_cfg.get("class") == "ETF":
            max_notional_usd = min(sizing_cap * float(self.cfg.get("ETF_MAX_NOTIONAL_FRAC", 0.18)), equity * 0.25)
            max_etf_lots = max_notional_usd / max(entry_px * float(info.trade_contract_size or 1.0), 1e-6)
            raw_lots = min(raw_lots, max_etf_lots)

        vmin, vmax, vstep = float(info.volume_min), float(info.volume_max), float(info.volume_step)
        if raw_lots < vmin * 0.5:
            return 0.0, 0.0, 0.0
        steps = math.floor(max(vmin, min(vmax, raw_lots)) / vstep + 1e-9)
        lot = round(max(vmin, steps * vstep), 2)

        # Pre-flight margin check via mt5.order_check
        req_chk = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym,
            "volume": float(lot),
            "type": mt5.ORDER_TYPE_BUY if direction == 1 else mt5.ORDER_TYPE_SELL,
            "price": float(entry_px),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": get_filling_mode(sym),
        }
        chk = mt5.order_check(req_chk)
        est_margin = float(chk.margin) if (chk and chk.margin is not None) else (lot * entry_px * float(info.trade_contract_size))
        actual_risk_pct = (lot * sl_dist * dpp / max(sizing_cap, 1.0)) * 100.0
        return lot, est_margin, actual_risk_pct

    def enforce_friday_liquidation(self, now_utc: datetime) -> bool:
        dt_ny = now_utc.astimezone(NY_TZ)
        if dt_ny.weekday() == 4 and (dt_ny.hour > 15 or (dt_ny.hour == 15 and dt_ny.minute >= 45)):
            positions = [p for p in (mt5.positions_get() or []) if p.magic == self.magic]
            if positions:
                log(f"[FRIDAY 15:45 NY AUTO-LIQUIDATION] Closing {len(positions)} active Cross-Asset 80h positions...")
                for p in positions:
                    ok, msg = close_mt5_position(p.ticket, p.symbol)
                    log(f"  Closed #{p.ticket} {p.symbol}: ok={ok} ({msg})")
            return True
        return False

    def manage_trailing_stops(self, now_utc: datetime):
        """Updates continuous 4.0 * ATR_entry ratcheting trailing stops (NO breakeven jump)."""
        trail_mult = float(self.cfg.get("TRAIL_ATR_MULT", 4.0))
        assets_cfg = self.cfg.get("ASSETS", {})
        positions = [p for p in (mt5.positions_get() or []) if p.magic == self.magic]
        live_tickets = {str(p.ticket) for p in positions}

        # Prune closed tickets from state
        removed = [k for k in list(self.state.keys()) if k not in live_tickets]
        for k in removed:
            del self.state[k]
        if removed:
            self.save_state()

        for p in positions:
            sym = p.symbol
            acfg = assets_cfg.get(sym, {})
            if acfg.get("class") == "ETF":
                rth_ok, _ = self.is_us_etf_rth_open(now_utc, for_entry=False)
                if not rth_ok:
                    continue
            else:
                mkt_ok, _, _ = is_market_open(sym, now_utc)
                if not mkt_ok:
                    continue

            t_key = str(p.ticket)
            info = mt5.symbol_info(sym)
            tick = mt5.symbol_info_tick(sym)
            if info is None or tick is None:
                continue

            is_buy = (p.type == mt5.ORDER_TYPE_BUY)
            st = self.state.get(t_key)
            if st is None:
                # Reconstruct state from H1 bars since position open
                bars = fetch_candles(sym, mt5.TIMEFRAME_H1, count=120, completed_only=False)
                open_utc = pd.Timestamp(datetime.fromtimestamp(mt5_ts_to_utc_epoch(p.time), tz=timezone.utc))
                atr_n = int(acfg.get("atr_bars", 20))
                if bars is not None and len(bars) > atr_n + 2:
                    c, h, l = bars["close"], bars["high"], bars["low"]
                    tr = np.maximum(h - l, np.maximum((h - c.shift(1)).abs(), (l - c.shift(1)).abs()))
                    atr_est = float(tr.rolling(atr_n).mean().iloc[-1])
                    since = bars[bars["time"] >= open_utc - pd.Timedelta(hours=1)]
                    best_p = float(since["high"].max()) if (is_buy and len(since)) else (float(since["low"].min()) if len(since) else float(p.price_open))
                else:
                    atr_est = abs(p.price_open - p.sl) / 2.5 if p.sl > 0 else float(p.price_open) * 0.005
                    best_p = float(p.price_current)
                st = {"sym": sym, "atr_entry": atr_est, "best_p": best_p}
                self.state[t_key] = st

            cur_ext = float(tick.bid) if is_buy else float(tick.ask)
            updated_state = False
            if is_buy and cur_ext > st["best_p"]:
                st["best_p"] = cur_ext
                updated_state = True
            elif (not is_buy) and cur_ext < st["best_p"]:
                st["best_p"] = cur_ext
                updated_state = True

            atr_e = float(st["atr_entry"])
            pos_trail_mult = float(st.get("trail_mult", trail_mult))
            pt = float(info.point)
            digits = int(info.digits)

            if is_buy:
                new_sl = round(st["best_p"] - pos_trail_mult * atr_e, digits)
                # Strict continuous trail only: must improve existing SL by >= 3 points and sit safely below bid
                if new_sl > float(p.sl) + 3.0 * pt and new_sl < float(tick.bid) - 5.0 * pt:
                    res = mt5.order_send({
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": p.ticket,
                        "symbol": sym,
                        "sl": float(new_sl),
                        "tp": float(p.tp),
                    })
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        log(f"[TRAIL RATCHET] #{p.ticket} {sym} BUY SL -> {new_sl:.{digits}f} (Best={st['best_p']:.{digits}f}, Trail={pos_trail_mult*atr_e:.{digits}f})")
            else:
                new_sl = round(st["best_p"] + pos_trail_mult * atr_e, digits)
                if (p.sl == 0 or new_sl < float(p.sl) - 3.0 * pt) and new_sl > float(tick.ask) + 5.0 * pt:
                    res = mt5.order_send({
                        "action": mt5.TRADE_ACTION_SLTP,
                        "position": p.ticket,
                        "symbol": sym,
                        "sl": float(new_sl),
                        "tp": float(p.tp),
                    })
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        log(f"[TRAIL RATCHET] #{p.ticket} {sym} SELL SL -> {new_sl:.{digits}f} (Best={st['best_p']:.{digits}f}, Trail={pos_trail_mult*atr_e:.{digits}f})")

            if updated_state:
                self.save_state()

    def scan_and_execute(self):
        self.cfg = self.load_config()
        now_utc = datetime.now(timezone.utc)

        # 1. Sunday Reopen Blackout (< Monday 00:00:00 UTC)
        if now_utc.weekday() == 6:
            self.write_telemetry(now_utc, [], "HALTED: Global Sunday Reopen Blackout (< Mon 00:00 UTC)")
            return

        # 2. Mandatory Friday 15:45 NY Auto-Liquidation
        if self.enforce_friday_liquidation(now_utc):
            self.write_telemetry(now_utc, [], "FLAT: Friday 15:45 NY Weekend Auto-Liquidation Active")
            return

        # 3. Continuous Trailing Stop Management on Active Positions
        self.manage_trailing_stops(now_utc)

        if not self.cfg.get("ENABLED", True):
            self.write_telemetry(now_utc, [], "DISABLED in cross_asset_config.json")
            return

        acc = mt5.account_info()
        equity = float(acc.equity) if acc else 100000.0
        max_margin_usd = equity * float(self.cfg.get("MAX_TOTAL_MARGIN_FRAC", 0.60))

        all_live_pos = list(mt5.positions_get() or [])
        boxx_mrg = sum(float(p.volume) * float(p.price_current) for p in all_live_pos if p.magic == 777899 or str(p.symbol) == "BOXX")
        used_margin = max(0.0, (float(acc.margin) if acc else 0.0) - boxx_mrg)
        open_80h = [p for p in all_live_pos if p.magic == self.magic]
        open_syms = {p.symbol for p in all_live_pos if p.magic != 777899}
        max_open = int(self.cfg.get("MAX_CONCURRENT_POSITIONS", 5))

        assets_cfg = self.cfg.get("ASSETS", {})
        z_thr = float(self.cfg.get("TREND_Z_THRESH", 0.30))
        sl_mult = float(self.cfg.get("SL_ATR_MULT", 2.5))
        swap_min_r = float(self.cfg.get("SWAP_VETO_MIN_R_PER_NIGHT", -0.020))
        swap_ext_z = float(self.cfg.get("SWAP_OVERRIDE_EXTREME_TREND_Z", 1.25))
        carry_w = float(self.cfg.get("CARRY_PRIORITY_WEIGHT", 15.0))
        use_jpy_macro = bool(self.cfg.get("ENABLE_EXTERNAL_JPY_MACRO_GATE", True))
        jpy_macro_min = float(self.cfg.get("JPY_MACRO_VETO_MIN_SCORE", -0.20))
        macro_prio_w = float(self.cfg.get("MACRO_PRIORITY_WEIGHT", 0.75))
        use_fomc_drift = bool(self.cfg.get("ENABLE_FOMC_BOND_DRIFT", True))

        macro_snap = get_live_macro_snapshot()
        gov_state = get_governed_risk_state()
        radar = []
        candidates = []

        # 3B. Check Scheduled FOMC (14:00 NY -> 15:00 NY H1 Close) Bond-Confirmed Drift Signals
        if use_fomc_drift:
            for fsig in evaluate_fomc_bond_confirmed_drift(now_utc):
                fsym = fsig["sym"]
                if fsym in open_syms or self.last_eval_bar.get(fsym) == fsig["bar_t"]:
                    continue
                acfg = assets_cfg.get(fsym, {})
                info = mt5.symbol_info(fsym)
                sw_r = self.estimate_swap_r_per_night(fsym, info, fsig["sig"], fsig["sl_dist"]) if info else 0.0
                candidates.append((5.0, fsym, acfg, fsig["sig"], fsig["sl_dist"], fsig["atr"], fsig["bar_t"], fsig["ief_ret"], sw_r, "FOMC_DRIFT"))

        for sym, acfg in assets_cfg.items():
            if not acfg.get("enabled", True):
                continue
            mt5.symbol_select(sym, True)
            info = mt5.symbol_info(sym)
            if info is None:
                continue

            m60_n = int(acfg.get("m60_bars", 1440))
            m20_n = int(acfg.get("m20_bars", 480))
            ch_n = int(acfg.get("chan_bars", 80))
            atr_n = int(acfg.get("atr_bars", 20))

            bars = fetch_candles(sym, mt5.TIMEFRAME_H1, count=m60_n + 200, completed_only=True)
            if bars is None or len(bars) < m60_n + 60:
                continue

            c = bars["close"]
            h = bars["high"]
            l = bars["low"]
            prev_c = c.shift(1)
            tr = np.maximum(h - l, np.maximum((h - prev_c).abs(), (l - prev_c).abs()))
            atr_v = float(tr.rolling(atr_n).mean().iloc[-1])
            if np.isnan(atr_v) or atr_v <= 0:
                continue

            r_vol = float(np.log(c / prev_c).rolling(atr_n * 5, min_periods=atr_n * 2).std().iloc[-1])
            m20_z = float(np.log(c.iloc[-1] / c.iloc[-1 - m20_n]) / (r_vol * math.sqrt(float(m20_n)) + 1e-9))
            m60_z = float(np.log(c.iloc[-1] / c.iloc[-1 - m60_n]) / (r_vol * math.sqrt(float(m60_n)) + 1e-9))
            m_tr = 0.5 * m20_z + 0.5 * m60_z

            # Prior ch_n completed bars BEFORE the latest completed bar: bars.iloc[-1 - ch_n : -1]
            hi_ch = float(h.iloc[-1 - ch_n : -1].max())
            lo_ch = float(l.iloc[-1 - ch_n : -1].min())
            last_c = float(c.iloc[-1])
            last_bar_t = str(bars["time"].iloc[-1])

            sl_dist = sl_mult * atr_v
            sw_l_r = self.estimate_swap_r_per_night(sym, info, +1, sl_dist)
            sw_s_r = self.estimate_swap_r_per_night(sym, info, -1, sl_dist)

            # Determine session eligibility
            if acfg.get("class") == "ETF":
                sess_ok, sess_reason = self.is_us_etf_rth_open(now_utc, for_entry=True)
            else:
                mkt_ok, mkt_rsn, _ = is_market_open(sym, now_utc)
                if not mkt_ok:
                    sess_ok, sess_reason = False, mkt_rsn
                elif now_utc.weekday() > 3:
                    sess_ok, sess_reason = False, "Friday entry cutoff (Mon-Thu 06:00-16:00 UTC)"
                elif not (6 <= now_utc.hour <= 16):
                    sess_ok, sess_reason = False, f"Outside 06:00-16:00 UTC active window ({now_utc.hour:02d}:00 UTC)"
                else:
                    sess_ok, sess_reason = True, "Active Window"

            raw_sig = 0
            if last_c > hi_ch and m_tr > z_thr:
                raw_sig = 1
            elif last_c < lo_ch and m_tr < -z_thr:
                raw_sig = -1

            eval_dir = raw_sig if raw_sig != 0 else (1 if m_tr >= 0 else -1)
            sw_dir_r = sw_l_r if eval_dir == 1 else sw_s_r
            carry_veto = (raw_sig != 0) and (sw_dir_r < swap_min_r) and (abs(m_tr) < swap_ext_z)

            is_jpy = (acfg.get("group") == "JPY")
            macro_sc = compute_jpy_macro_score(sym, eval_dir, macro_snap) if (use_jpy_macro and is_jpy) else 0.0
            macro_veto = (raw_sig != 0) and use_jpy_macro and is_jpy and (macro_sc < jpy_macro_min)
            jpy_opp = [
                f"{p.symbol}:{'BUY' if p.type == mt5.ORDER_TYPE_BUY else 'SELL'}"
                for p in all_live_pos
                if is_jpy and str(p.symbol).endswith("JPY") and p.symbol != sym and ((1 if p.type == mt5.ORDER_TYPE_BUY else -1) != eval_dir)
            ]
            jpy_mutex_veto = (raw_sig != 0) and bool(jpy_opp)

            if sym in open_syms:
                status_str = "IN_POSITION"
            elif carry_veto:
                status_str = "CARRY_VETO"
            elif macro_veto:
                status_str = "MACRO_VETO"
            elif jpy_mutex_veto:
                status_str = "JPY_MUTEX"
            elif raw_sig != 0 and sess_ok:
                status_str = "SIGNAL"
            elif not sess_ok:
                status_str = "OFF_SESSION"
            else:
                status_str = "SCANNING"

            detail_str = sess_reason if not sess_ok else (
                f"Blocked negative swap ({sw_dir_r:+.3f}R/n)" if carry_veto else (
                    f"Blocked JPY Yield/Credit headwind (Macro={macro_sc:+.2f} < {jpy_macro_min:+.2f})" if macro_veto else (
                        f"Blocked opposing JPY leg ({', '.join(jpy_opp)})" if jpy_mutex_veto else f"Bar {last_bar_t[:16]}"
                    )
                )
            )
            radar.append({
                "symbol": sym,
                "group": acfg.get("group"),
                "class": acfg.get("class"),
                "last_close": round(last_c, int(info.digits)),
                "donch_hi": round(hi_ch, int(info.digits)),
                "donch_lo": round(lo_ch, int(info.digits)),
                "trend_z": round(m_tr, 3),
                "macro_score": round(macro_sc, 3) if is_jpy else None,
                "req_z": z_thr,
                "atr": round(atr_v, int(info.digits)),
                "swap_long_pts": round(float(info.swap_long), 2),
                "swap_short_pts": round(float(info.swap_short), 2),
                "swap_r_night": round(sw_dir_r, 4),
                "status": status_str,
                "detail": detail_str,
            })

            if raw_sig != 0 and sess_ok and (not carry_veto) and (not macro_veto) and (not jpy_mutex_veto) and (sym not in open_syms):
                if self.last_eval_bar.get(sym) != last_bar_t:
                    prio = abs(m_tr) + carry_w * sw_dir_r + (macro_prio_w * macro_sc if is_jpy else 0.0)
                    candidates.append((prio, sym, acfg, raw_sig, sl_dist, atr_v, last_bar_t, m_tr, sw_dir_r, "80H"))

        # Execute ranked breakout & FOMC drift candidates respecting group caps (across ALL live engines), total open cap, and total margin cap
        candidates.sort(key=lambda x: x[0], reverse=True)
        for prio, sym, acfg, sig, sl_dist, atr_v, bar_t, m_tr, sw_r, sig_tag in candidates:
            if len(open_80h) >= max_open:
                break
            grp = acfg.get("group", "OTHER")
            grp_max = int(acfg.get("group_max", 2))
            grp_open = sum(
                1 for p in all_live_pos
                if assets_cfg.get(p.symbol, {}).get("group") == grp
                or (grp == "METAL" and ("XAU" in str(p.symbol) or "GOLD" in str(p.symbol)))
                or (grp == "JPY" and str(p.symbol).endswith("JPY"))
            )
            if grp_open >= grp_max:
                log(f"[GROUP CAP] Skipped {sym} ({grp} {grp_open}/{grp_max} full across live desk)")
                self.last_eval_bar[sym] = bar_t
                continue

            tick = mt5.symbol_info_tick(sym)
            info = mt5.symbol_info(sym)
            if not tick or not info:
                continue

            is_buy = (sig == 1)
            entry_px = float(tick.ask if is_buy else tick.bid)
            lot, est_mrg, act_risk_pct = self.calculate_lot_and_margin(sym, acfg, sl_dist, entry_px, sig, equity, gov_state=gov_state)
            if lot <= 0:
                continue
            if used_margin + est_mrg > max_margin_usd:
                log(f"[MARGIN GUARD] Skipped {sym}: est margin ${est_mrg:,.0f} + used ${used_margin:,.0f} > cap ${max_margin_usd:,.0f}")
                continue

            digits = int(info.digits)
            sl_px = round(entry_px - sl_dist if is_buy else entry_px + sl_dist, digits)
            side_str = "BUY" if is_buy else "SELL"

            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": sym,
                "volume": float(lot),
                "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
                "price": entry_px,
                "sl": float(sl_px),
                "tp": 0.0,
                "deviation": 20,
                "magic": self.magic,
                "comment": f"ANT {sig_tag} {sym} ({act_risk_pct:.2f}%)",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": get_filling_mode(sym),
            }
            res = send_guarded_entry_order(req, log)
            self.last_eval_bar[sym] = bar_t
            if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                pos_tr_m = 3.0 if sig_tag == "FOMC_DRIFT" else float(self.cfg.get("TRAIL_ATR_MULT", 4.0))
                log(
                    f"[{sig_tag} FILLED] {sym} {side_str} {lot}L @ {entry_px:.{digits}f} | SL={sl_px:.{digits}f} "
                    f"| Risk={act_risk_pct:.2f}% | Metric={m_tr:+.2f} | Swap={sw_r:+.4f}R/n | Ticket=#{res.order}"
                )
                self.state[str(res.order)] = {"sym": sym, "atr_entry": float(atr_v), "best_p": entry_px, "trail_mult": pos_tr_m, "tag": sig_tag}
                self.save_state()
                used_margin += est_mrg
                all_live_pos = list(mt5.positions_get() or [])
                open_80h = [p for p in all_live_pos if p.magic == self.magic]
                open_syms = {p.symbol for p in all_live_pos}
            else:
                err = getattr(res, "comment", mt5.last_error())
                log(f"[{sig_tag} ENTRY REJECTED] {sym} {side_str}: {err}")

        self.write_telemetry(now_utc, radar, "ACTIVE: Cross-Asset 80h + External Yield/Credit Macro + Vol/DD Governor", macro_snap, gov_state)

    def write_telemetry(self, now_utc: datetime, radar: list, status_msg: str, macro_snap: dict = None, gov_state: dict = None):
        open_pos = []
        for p in (mt5.positions_get() or []):
            if p.magic == self.magic:
                open_pos.append({
                    "ticket": int(p.ticket),
                    "symbol": p.symbol,
                    "side": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                    "volume": float(p.volume),
                    "price_open": float(p.price_open),
                    "price_current": float(p.price_current),
                    "sl": float(p.sl),
                    "profit": round(float(p.profit), 2),
                })
        payload = {
            "timestamp": now_utc.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "engine": "CROSS_ASSET_80H_DONCHIAN_TREND",
            "magic": self.magic,
            "status": status_msg,
            "breakeven_protection": "PERMANENTLY_DISABLED_BY_DESIGN",
            "active_positions_count": len(open_pos),
            "active_positions": open_pos,
            "macro_snapshot": macro_snap or {},
            "risk_governor": gov_state or {},
            "radar": radar,
        }
        try:
            tmp = TELEMETRY_PATH.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            tmp.replace(TELEMETRY_PATH)
        except Exception:
            pass

    def run(self):
        log("=======================================================================")
        log("CROSS-ASSET 80H DONCHIAN + 20/60D TREND + MT5 CARRY ENGINE LIVE")
        log("Universe: XAUUSD, XAUEUR, USDJPY, EURJPY, GBPJPY, AUDJPY, IVV, XLK, TLT, USO")
        log("Breakeven Protection: PERMANENTLY DISABLED BY DESIGN")
        log("=======================================================================")
        while True:
            try:
                if not initialize_mt5():
                    time.sleep(10)
                    continue
                self.scan_and_execute()
            except Exception as e:
                log(f"Cycle exception: {e}")
            time.sleep(20)


def main():
    if not initialize_mt5():
        print("[CRITICAL] Failed to connect to MetaTrader 5.")
        return
    engine = CrossAsset80hBreakoutEngine()
    try:
        engine.run()
    finally:
        shutdown_mt5()


if __name__ == "__main__":
    main()
