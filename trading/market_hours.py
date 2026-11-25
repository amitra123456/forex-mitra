"""
Institutional Market Hours & Schedule Intelligence Guard (ANT_FOREX)
====================================================================
Accurately tracks global Forex and Precious Metals trading sessions,
weekend closures, Sunday market openings, daylight saving adjustments,
and daily rollover pauses.

Forex Schedule (Currencies):
  - Closes: Friday at 17:00 New York Time (21:00 UTC in DST / 22:00 UTC in Standard Time)
  - Opens:  Sunday at 17:00 New York Time (21:00 UTC in DST / 22:00 UTC in Standard Time)
  - Trades continuously 24/5 from Sunday open through Friday close.

Precious Metals Schedule (XAUUSD / Gold):
  - Closes: Friday at 17:00 New York Time
  - Opens:  Sunday at 18:00 New York Time (22:00 UTC in DST / 23:00 UTC in Standard Time)
  - Daily Rollover Pause: Monday-Thursday 17:00-18:00 NY Time (21:00-22:00 UTC in DST)
"""

import sys
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional, Dict, Any
from zoneinfo import ZoneInfo
import MetaTrader5 as mt5

if hasattr(sys.stdout, "reconfigure") and sys.stdout is not None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

NY_TZ = ZoneInfo("America/New_York")


def is_us_daylight_saving(dt_utc: datetime) -> bool:
    """Returns True if New York is currently observing Daylight Saving Time (EDT)."""
    try:
        dt_ny = dt_utc.astimezone(NY_TZ)
        return bool(dt_ny.dst() and dt_ny.dst().total_seconds() > 0)
    except Exception:
        # Fallback heuristic: US DST runs from 2nd Sunday in March to 1st Sunday in November
        month = dt_utc.month
        return 3 < month < 11


def is_friday_pre_liquidation_cutoff(
    now_utc: Optional[datetime] = None,
    cutoff_hour_ny: int = 15,
    cutoff_minute_ny: int = 30,
) -> bool:
    """
    Returns True ONLY on Friday at or after 15:30 America/New_York (15 min before
    the mandatory 15:45 NY weekend liquidation). Per GEMINI.md, trading is
    permitted throughout Friday without premature midday UTC cutoffs.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    dt_ny = now_utc.astimezone(NY_TZ)
    if dt_ny.weekday() != 4:
        return False
    return (dt_ny.hour, dt_ny.minute) >= (cutoff_hour_ny, cutoff_minute_ny)


def get_weekend_hours(now_utc: datetime, is_metal: bool = False) -> Tuple[int, int]:
    """
    Returns (close_hour_utc, open_hour_utc) for the weekend.
    In US Summer (DST):
      Forex: Close Friday 21:00 UTC, Open Sunday 21:00 UTC
      Metals: Close Friday 21:00 UTC, Open Sunday 22:00 UTC
    In US Winter (Standard):
      Forex: Close Friday 22:00 UTC, Open Sunday 22:00 UTC
      Metals: Close Friday 22:00 UTC, Open Sunday 23:00 UTC
    """
    dst = is_us_daylight_saving(now_utc)
    base_hour = 21 if dst else 22
    close_hour = base_hour
    open_hour = (base_hour + 1) if is_metal else base_hour
    return close_hour, open_hour


def get_next_market_open(symbol: str = "EURUSD", now_utc: Optional[datetime] = None) -> datetime:
    """Calculate the exact next UTC datetime when the market will open."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    is_metal = "XAU" in symbol.upper() or "GOLD" in symbol.upper()
    close_hour, open_hour = get_weekend_hours(now_utc, is_metal=is_metal)
    weekday = now_utc.weekday()  # Monday is 0, Sunday is 6

    # Global Rule: All weekend closures open on Monday 00:00:00 UTC (Tokyo Interbank Open)
    # 1. Friday after 15:45 NY close
    if weekday == 4:
        days_ahead = 3
        target_date = now_utc.date() + timedelta(days=days_ahead)
        return datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=timezone.utc)

    # 2. Saturday
    elif weekday == 5:
        days_ahead = 2
        target_date = now_utc.date() + timedelta(days=days_ahead)
        return datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=timezone.utc)

    # 3. Sunday (Global Sunday Reopen Blackout: Opens Monday 00:00 UTC)
    elif weekday == 6:
        days_ahead = 1
        target_date = now_utc.date() + timedelta(days=days_ahead)
        return datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=timezone.utc)

    # 4. Universal Daily Interbank & Metals Rollover Blackout (Mon-Thu 16:45-19:00 America/New_York)
    dt_ny = now_utc.astimezone(NY_TZ)
    if dt_ny.weekday() in [0, 1, 2, 3] and ((dt_ny.hour == 16 and dt_ny.minute >= 45) or dt_ny.hour in [17, 18]):
        resume_ny = dt_ny.replace(hour=19, minute=0, second=0, microsecond=0)
        return resume_ny.astimezone(timezone.utc)

    # 5. Normal weekday when open: next open is next Monday 00:00 UTC
    days_to_monday = (7 - weekday) % 7
    if days_to_monday == 0:
        days_to_monday = 7
    target_date = now_utc.date() + timedelta(days=days_to_monday)
    return datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=timezone.utc)


def is_market_open(symbol: str = "EURUSD", now_utc: Optional[datetime] = None) -> Tuple[bool, str, Optional[datetime]]:
    """
    Evaluates whether the trading market is currently open for the given symbol.
    Enforces:
    - Global Sunday Reopen Blackout: No trading on Sunday before Monday 00:00:00 UTC (Tokyo Open).
    - Mandatory Friday Auto-Liquidation: Market closed for new entries at 15:45 NY time (3:45 PM NY).
    Returns:
      (is_open: bool, reason: str, next_open_dt: Optional[datetime])
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    is_metal = "XAU" in symbol.upper() or "GOLD" in symbol.upper()
    close_hour, open_hour = get_weekend_hours(now_utc, is_metal=is_metal)
    weekday = now_utc.weekday()  # Monday=0, ..., Friday=4, Saturday=5, Sunday=6

    # 1. Saturday check (Market is 100% CLOSED all day)
    if weekday == 5:
        next_open = get_next_market_open(symbol, now_utc)
        diff = next_open - now_utc
        hrs, rem = divmod(int(diff.total_seconds()), 3600)
        mins = rem // 60
        reason = f"Market Closed (Weekend) · Opens Monday 00:00 UTC (in {hrs}h {mins:02d}m)"
        return False, reason, next_open

    # 2. Sunday check: Global Sunday Reopen Blackout (Trading locked until Monday 00:00 UTC Tokyo Open)
    if weekday == 6:
        next_open = get_next_market_open(symbol, now_utc)
        diff = next_open - now_utc
        hrs, rem = divmod(int(diff.total_seconds()), 3600)
        mins = rem // 60
        reason = f"Global Sunday Reopen Blackout · Opens Monday 00:00 UTC Tokyo Open (in {hrs}h {mins:02d}m)"
        return False, reason, next_open

    # 3. Friday check: Market closes for new entries at 15:45 NY (3:45 PM NY Auto-Liquidation)
    dt_ny = now_utc.astimezone(NY_TZ)
    if dt_ny.weekday() == 4 and (dt_ny.hour > 15 or (dt_ny.hour == 15 and dt_ny.minute >= 45)):
        next_open = get_next_market_open(symbol, now_utc)
        diff = next_open - now_utc
        hrs, rem = divmod(int(diff.total_seconds()), 3600)
        mins = rem // 60
        reason = f"Market Closed (Friday 3:45 PM NY Auto-Liquidation Complete · Opens Monday 00:00 UTC in {hrs}h {mins:02d}m)"
        return False, reason, next_open

    # 4. Universal Daily Interbank & Metals Rollover Blackout (Mon-Thu 16:45-19:00 America/New_York)
    if dt_ny.weekday() in [0, 1, 2, 3] and ((dt_ny.hour == 16 and dt_ny.minute >= 45) or dt_ny.hour in [17, 18]):
        next_open = get_next_market_open(symbol, now_utc)
        diff = next_open - now_utc
        hrs, rem = divmod(max(0, int(diff.total_seconds())), 3600)
        mins = rem // 60
        reason = f"Market Paused (Daily Rollover & Reopen Spread Shield 16:45-19:00 NY) · Resumes {next_open.strftime('%H:%M UTC')} (in {hrs}h {mins:02d}m)"
        return False, reason, next_open

    # 5. Check broker MT5 trade mode if connected
    try:
        info = mt5.symbol_info(symbol)
        if info is not None:
            if info.trade_mode == mt5.SYMBOL_TRADE_MODE_DISABLED:
                return False, f"Trading Disabled by Broker for {symbol}", None
    except Exception:
        pass

    return True, "Market Open", None


def get_market_status_digest(symbol: str = "EURUSD", now_utc: Optional[datetime] = None) -> Dict[str, Any]:
    """Provides a structured dictionary of current market status and schedule."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    is_open, reason, next_open = is_market_open(symbol, now_utc)
    is_metal = "XAU" in symbol.upper() or "GOLD" in symbol.upper()
    close_hour, open_hour = get_weekend_hours(now_utc, is_metal=is_metal)
    dst = is_us_daylight_saving(now_utc)
    tz_label = "EDT (UTC-4)" if dst else "EST (UTC-5)"

    time_left_str = ""
    if next_open:
        diff = next_open - now_utc
        hrs, rem = divmod(int(diff.total_seconds()), 3600)
        mins = rem // 60
        time_left_str = f"{hrs}h {mins:02d}m"

    return {
        "symbol": symbol,
        "is_open": is_open,
        "status_badge": "🟢 OPEN" if is_open else "🔴 CLOSED",
        "reason": reason,
        "next_open_dt": next_open,
        "next_open_str": next_open.strftime("%A %H:%M UTC") if next_open else "N/A",
        "time_until_open": time_left_str,
        "schedule": f"Opens Sun {open_hour:02d}:00 UTC · Closes Fri {close_hour:02d}:00 UTC ({tz_label})",
    }


def check_pair_blackout(
    symbol: str,
    engine_key: str = "M1_M3",
    side: Optional[str] = None,
    now_utc: Optional[datetime] = None,
    cfg: Optional[Dict[str, Any]] = None
) -> Tuple[bool, str, str]:
    """
    Evaluates whether the given symbol/engine is currently in a pair-specific blackout period,
    news shock pause, fixing noise shield, or deadzone.
    
    Returns:
        (is_blackout: bool, shield_id: str, reason: str)
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    if cfg is None:
        cfg = {}

    sym = symbol.upper()
    h = now_utc.hour
    m = now_utc.minute
    w = now_utc.weekday()  # Monday=0, ..., Friday=4, Saturday=5, Sunday=6

    # 1. Global Market Hours / Weekend Check
    is_open, mkt_reason, _ = is_market_open(sym, now_utc)
    if not is_open:
        return True, "MARKET_CLOSED", mkt_reason

    # 1B. Extended US Macro News & NY Cash Open Blackout (12:15 - 13:30 UTC / 8:15 - 9:30 AM NY)
    if cfg.get("ENABLE_US_MACRO_NY_OPEN_SHIELD", True):
        if (h == 12 and m >= 15) or (h == 13 and m <= 30):
            return True, "US_MACRO_NY_OPEN_BLACKOUT", "Extended US Macro News (8:30 AM NY) & NY Cash Open Blackout (12:15-13:30 UTC)"

    # 2. USDJPY Pair-Specific Shields
    if "USDJPY" in sym or (sym.endswith("JPY") and "USD" in sym):
        # A. London 4 PM Fix & NY Rebalancing Noise Shield (14:00 - 16:45 UTC)
        if cfg.get("ENABLE_LONDON_FIX_SHIELD", cfg.get("RULE_1_LONDON_FIX", True)):
            if 14 <= h < 16 or (h == 16 and m < 45):
                return True, "LONDON_FIX_SHIELD", "London Fix & NY Rebalancing Noise Shield (14:00-16:45 UTC)"

        # B. Tokyo Post-Fix Long Exhaustion Shield (01:00 - 02:30 UTC for BUY orders)
        if cfg.get("RULE_2_TOKYO_POSTFIX", cfg.get("ENABLE_TOKYO_POSTFIX_SHIELD", True)):
            if (h == 1 or (h == 2 and m < 30)) and (side in ["BUY", "LONG"] or side is None):
                return True, "TOKYO_POSTFIX_SHIELD", "Tokyo Post-Fix Exhaustion Trap (01:00-02:30 UTC - Long Blocked)"

        # C. Friday Pre-Liquidation Cutoff (15:30 NY — 15m prior to 15:45 NY Auto-Liquidation)
        if cfg.get("ENABLE_FRIDAY_DERISK", True):
            if is_friday_pre_liquidation_cutoff(now_utc):
                return True, "FRIDAY_PRE_LIQUIDATION", "Friday Pre-Liquidation Cutoff (>= 15:30 NY before 15:45 NY Exit)"

        # D. NY Close / Daily Rollover Shield (Mon-Thu 21:00 - 22:00 UTC)
        if cfg.get("ENABLE_NY_CLOSE_SHIELD", True):
            if w in [0, 1, 2, 3] and h == 21:
                return True, "NY_CLOSE_ROLLOVER", "NY Close & Daily Rollover Spread Spike (21:00-22:00 UTC)"

    # 3. USDCAD Pair-Specific Shields
    elif "USDCAD" in sym or "CAD" in sym:
        # A. Macro News Shock Window (14:00 UTC / 10:00 AM EDT EIA Crude/NFP/CPI/BOC)
        if cfg.get("FILTER_14UTC_NEWS_TRAP", True):
            if h == 14:
                return True, "MACRO_NEWS_TRAP", "US/Canada Macro News Shock Window (14:00 UTC / 10:00 AM EDT EIA/NFP/BOC)"

        # B. NY Floor Close Deadzone (17:00 - 19:59 UTC)
        if cfg.get("FILTER_18UTC_DEADZONE", True):
            if 17 <= h <= 19:
                return True, "NY_FLOOR_CLOSE", "NY Floor Close Deadzone (17:00-20:00 UTC / 1:00-4:00 PM EDT)"

        # C. NY Core Active Window: 12:00 - 17:00 UTC
        start_h = int(cfg.get("SESSION_START_UTC", 12))
        end_h = int(cfg.get("SESSION_END_UTC", 17))
        if not (start_h <= h < end_h):
            return True, "SESSION_WINDOW", f"Outside USDCAD NY Core Session ({start_h:02d}:00-{end_h:02d}:00 UTC)"

    # 4. AUDUSD Pair-Specific Shields
    elif "AUDUSD" in sym or "AUD" in sym:
        # A. US Macro News Shock Pause (14:00 - 15:59 UTC)
        if cfg.get("ENABLE_US_NEWS_PAUSE", True):
            pause_start = int(cfg.get("US_NEWS_PAUSE_START_UTC", 14))
            pause_end = int(cfg.get("US_NEWS_PAUSE_END_UTC", 15))
            if pause_start <= h <= pause_end:
                return True, "US_NEWS_PAUSE", f"US Morning Macro Data Release Pause ({pause_start:02d}:00-{pause_end+1:02d}:00 UTC)"

        # B. Asian & Liquid Session Active Window (00:00 - 18:00 UTC)
        start_h = int(cfg.get("SESSION_START_UTC", 0))
        end_h = int(cfg.get("SESSION_END_UTC", 18))
        if not (start_h <= h <= end_h):
            return True, "OFF_SESSION", f"Outside AUDUSD Active Trading Session ({start_h:02d}:00-{end_h:02d}:00 UTC)"

    # 5. GBPUSD Pair-Specific Shields
    elif "GBPUSD" in sym:
        if engine_key in ["M1_M3", "M1_M3_MOMENTUM"]:
            # London Cash Open Auction Whipsaw (08:00 - 08:59 UTC)
            if h == 8:
                return True, "LONDON_OPEN_CHURN", "London Cash Open Auction Whipsaw (08:00-09:00 UTC)"
            # NY Floor Close / Late Evening (19:00 - 22:59 UTC)
            if 19 <= h <= 22:
                return True, "NY_CLOSE_DEADZONE", "NY Floor Close & Rollover Deadzone (19:00-23:00 UTC)"
            start_h = int(cfg.get("SESSION_START_UTC", 0))
            end_h = int(cfg.get("SESSION_END_UTC", 19))
            if not ((start_h <= h < end_h) or h == 23):
                return True, "SESSION_WINDOW", "Outside GBPUSD Active Session (00:00-08:00, 09:00-19:00 UTC)"
        else:
            start_h = int(cfg.get("SESSION_START_UTC", 0))
            end_h = int(cfg.get("SESSION_END_UTC", 23))
            if not (start_h <= h <= end_h):
                return True, "SESSION_WINDOW", f"Outside GBPUSD Active Session ({start_h:02d}:00-{end_h:02d}:00 UTC)"

    # 6. GBPJPY Pair-Specific Shields
    elif "GBPJPY" in sym:
        # NY Floor Close & Rollover Deadzone (19:00, 21:00-22:59 UTC)
        if h in [19, 21, 22]:
            return True, "SESSION_WINDOW", "GBPJPY Off-Peak Rollover / Spread Widening (19:00, 21:00-22:00 UTC)"
        if engine_key == "SM1":
            start_h = int(cfg.get("SESSION_START_UTC", 7))
            end_h = int(cfg.get("SESSION_END_UTC", 12))
            if not (start_h <= h < end_h):
                return True, "SESSION_WINDOW", f"Outside GBPJPY Asian Breakout London Window ({start_h:02d}:00-{end_h:02d}:00 UTC)"
        else:
            # Active Tokyo, London & NY: 23:00 - 19:00 UTC
            start_h = int(cfg.get("SESSION_START_UTC", 0))
            end_h = int(cfg.get("SESSION_END_UTC", 24))

    # 7. EURJPY Pair-Specific Shields
    elif "EURJPY" in sym:
        if engine_key == "SM1":
            allowed_hours = cfg.get("ALLOWED_HOURS_UTC", [0, 2, 3])
            if h not in allowed_hours:
                return True, "SESSION_WINDOW", f"Outside EURJPY Tokyo Fix / European Open Window ({allowed_hours} UTC)"

    # 8. EURUSD Pair-Specific Shields
    elif "EURUSD" in sym:
        if engine_key in ["M1_M3", "M1_M3_MOMENTUM"]:
            # Asian Mid-Session Chop (01:00 - 04:59 UTC)
            if 1 <= h <= 4:
                return True, "ASIAN_CHOP", "Asian Mid-Session Chop (01:00-05:00 UTC)"
            # London Open Auction Whipsaw (08:00 - 08:59 UTC)
            if h == 8:
                return True, "LONDON_OPEN_CHURN", "London Open Churn / Liquidity Sweep (08:00-09:00 UTC)"
            # Active Windows: 00:00 - 01:00 (Tokyo Open) + 05:00 - 16:30 (European / NY)
            start_h = int(cfg.get("SESSION_START_UTC", 0))
            end_h = int(cfg.get("SESSION_END_UTC", 16))
            if not ((h == 0) or (5 <= h <= end_h)):
                return True, "SESSION_WINDOW", "Outside EURUSD Active Session (00:00-01:00, 05:00-16:30 UTC)"
        else:
            start_h = int(cfg.get("SESSION_START_UTC", 0))
            end_h = int(cfg.get("SESSION_END_UTC", 23))
            if not (start_h <= h <= end_h):
                return True, "SESSION_WINDOW", f"Outside EURUSD Active Session ({start_h:02d}:00-{end_h:02d}:00 UTC)"

    # 9. Generic Session Filter Fallback
    if "SESSION_START_UTC" in cfg and "SESSION_END_UTC" in cfg:
        start_h = int(cfg["SESSION_START_UTC"])
        end_h = int(cfg["SESSION_END_UTC"])
        if start_h != 0 or end_h != 24:
            if not (start_h <= h <= end_h):
                return True, "SESSION_WINDOW", f"Outside Configured Session Window ({start_h:02d}:00-{end_h:02d}:00 UTC)"

    return False, "NONE", "Clear (No Active Blackout)"


def get_all_pair_blackouts(now_utc: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """Returns a dictionary of blackout status and active shields for all monitored currency pairs."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    all_symbols = ["EURUSD", "USDJPY", "GBPUSD", "GBPJPY", "USDCAD", "AUDUSD", "EURJPY", "XAUUSD"]
    results = {}
    for s in all_symbols:
        is_blk, shield, reason = check_pair_blackout(s, engine_key="M1_M3", now_utc=now_utc)
        results[s] = {
            "symbol": s,
            "is_blackout": is_blk,
            "shield": shield,
            "reason": reason
        }
    return results


PAIR_BLACKOUT_CONFIG = {
    "USDJPY": {
        "name": "USD/JPY",
        "flag": "🇯🇵",
        "shields": [
            ("London Fix & NY Rebalance Shield", "14:00 - 16:45 UTC", "Blocks noise & spread spikes during London 4 PM WMR Fix"),
            ("Tokyo Post-Fix Long Shield", "01:00 - 02:30 UTC", "Blocks BUY orders during post-Tokyo Fix exhaustion trap"),
            ("Friday De-Risk Shield", "Friday >= 12:00 UTC", "Prevents holding new exposure into weekend illiquidity"),
            ("NY Close Rollover Shield", "Mon-Thu 21:00 - 22:00 UTC", "Blocks daily bank settlement rollover spread spikes"),
        ],
        "default_session": "Continuous liquid sessions (subject to shields)"
    },
    "USDCAD": {
        "name": "USD/CAD",
        "flag": "🇨🇦",
        "shields": [
            ("Macro News Shock Trap", "14:00 - 15:00 UTC", "10:00 AM EDT EIA Oil Inventories, NFP, CPI, BOC Rate Decisions"),
            ("NY Floor Close Deadzone", "18:00 - 19:00 UTC", "2:00 PM EDT pit close liquidity drain & reversal risk"),
            ("NA Liquid Session Window", "12:00 - 18:00 UTC", "Only trades high-liquidity North American morning overlap"),
        ],
        "default_session": "12:00 - 18:00 UTC (North American Session)"
    },
    "AUDUSD": {
        "name": "AUD/USD",
        "flag": "🇦🇺",
        "shields": [
            ("US Macro News Pause", "14:00 - 16:00 UTC", "Pauses during high-impact US morning economic data releases"),
            ("Asian & Liquid Active Window", "00:00 - 18:00 UTC", "Captures high-alpha Asian trend expansion & London/NY flows"),
        ],
        "default_session": "00:00 - 18:00 UTC (Asian & London/NY Active)"
    },
    "EURUSD": {
        "name": "EUR/USD",
        "flag": "🇪🇺",
        "shields": [
            ("M1/M3 Core London/NY Window", "08:00 - 16:00 UTC", "High-velocity momentum confined to London Core + NY Overlap"),
            ("Sniper & SM1 Active Window", "06:00 - 18:00 UTC", "Pullback & swing strategies trade broader European day"),
        ],
        "default_session": "08:00 - 16:00 UTC (M1) / 06:00 - 18:00 UTC (Sniper/SM1)"
    },
    "GBPUSD": {
        "name": "GBP/USD",
        "flag": "🇬🇧",
        "shields": [
            ("M1/M3 Core London Window", "09:00 - 15:00 UTC", "Core momentum confined to London peak liquidity hours"),
            ("Sniper & SM1 Active Window", "06:00 - 18:00 UTC", "Extended active session for pullback and structural swings"),
        ],
        "default_session": "09:00 - 15:00 UTC (M1) / 06:00 - 18:00 UTC (Sniper/SM1)"
    },
    "GBPJPY": {
        "name": "GBP/JPY",
        "flag": "🇬🇧🇯🇵",
        "shields": [
            ("Asian Breakout London Window (SM1)", "07:00 - 12:00 UTC", "Captures London open breakout expansion from Asian range"),
            ("London & NY Session (M1/Sniper)", "07:00 - 17:00 UTC", "Operates during high dragon cross volatility window"),
        ],
        "default_session": "07:00 - 17:00 UTC (London & NY Sessions)"
    },
    "EURJPY": {
        "name": "EUR/JPY",
        "flag": "🇪🇺🇯🇵",
        "shields": [
            ("Tokyo Fix / Euro Open Window (SM1)", "00:00, 02:00, 03:00 UTC", "Calibrated European early session liquidity impulses"),
            ("Active Trading Window (M1/Sniper)", "06:00 - 18:00 UTC", "Liquid non-Asia hours to avoid off-session chop"),
        ],
        "default_session": "06:00 - 18:00 UTC (Non-Asia Liquid Hours)"
    },
    "XAUUSD": {
        "name": "XAU/USD",
        "flag": "🥇",
        "shields": [
            ("Daily Metals Rollover Pause", "Mon-Thu 21:00 - 22:00 UTC", "CME gold trading pause during daily physical settlement"),
            ("Weekend Closure (Extended)", "Fri 21:00 - Sun 22:00 UTC", "Gold opens 1 hour later than forex currencies on Sunday"),
            ("Spread Cost Ratio Ceiling", "Live Spread > 25% Target ATR", "Vetoes execution if broker bid-ask spread consumes >25% target"),
        ],
        "default_session": "22:00 Sun - 21:00 Fri UTC (Excludes 21-22 Mon-Thu)"
    }
}


def get_pair_schedule_and_blackout_matrix(now_utc: Optional[datetime] = None) -> list:
    """
    Returns a structured list of blackout status, active shield, and configured shield rules
    for all monitored pairs.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    matrix = []
    for sym, meta in PAIR_BLACKOUT_CONFIG.items():
        is_open, mkt_reason, next_open = is_market_open(sym, now_utc)
        is_blk, shield_id, blk_reason = check_pair_blackout(sym, engine_key="M1_M3", now_utc=now_utc)
        
        if not is_open:
            status_badge = "🔴 CLOSED"
            current_detail = mkt_reason
        elif is_blk:
            status_badge = f"🔴 BLOCKED"
            current_detail = blk_reason
        else:
            status_badge = "🟢 CLEAR"
            current_detail = "Active session · No active blackout shield (Trading Permitted)"

        matrix.append({
            "symbol": sym,
            "name": meta["name"],
            "flag": meta["flag"],
            "is_open": is_open,
            "is_blackout": is_blk,
            "shield_id": shield_id,
            "status_badge": status_badge,
            "current_detail": current_detail,
            "configured_shields": meta["shields"],
            "default_session": meta["default_session"],
            "next_open_str": next_open.strftime("%A %H:%M UTC") if next_open else "N/A"
        })
    return matrix


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    print("Testing Market Hours & Blackout Intelligence...")
    print(f"Current UTC: {now} (Weekday: {now.strftime('%A')})")
    
    for test_sym in ["EURUSD", "USDJPY", "XAUUSD", "USDCAD", "AUDUSD"]:
        status = get_market_status_digest(test_sym, now)
        is_blk, shield, reason = check_pair_blackout(test_sym, now_utc=now)
        print(f"\n[{test_sym}] Status: {status['status_badge']} | Reason: {status['reason']}")
        print(f"Blackout Shield: {'🔴 ACTIVE' if is_blk else '🟢 CLEAR'} ({shield}) -> {reason}")

