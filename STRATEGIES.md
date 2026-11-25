# `forex-mitra` — Live Production Strategies & Risk Architecture

This repository contains the live production trading engines, shared institutional macro/risk governors, MT5 execution adapters, and model/COT artifacts actively running in production. Legacy short-horizon M15 FX engines, paper-trading bots, and research scripts are excluded by design.

---

## 1. Production Portfolio Overview

Three independent sleeves operate inside two continuously running Python execution engines, supervised by `trading/master_watchdog.py` and protected by `trading/friday_auto_liquidate.py`.

| Sleeve | Bot / Config | Magic | Markets Traded | Core Style | Active Entry Window | Risk & Exit Summary |
| :--- | :--- | :---: | :--- | :--- | :--- | :--- |
| **1. Cross-Asset 80h Donchian + 20/60d Trend (+ FOMC Drift)** | `trading/cross_asset_80h_breakout_bot.py`<br>`trading/cross_asset_config.json` | `777880` | **JPY FX:** `USDJPY`, `EURJPY`, `AUDJPY`<br>**Metals:** `XAUUSD`, `XAUEUR`<br>**US ETFs:** `IVV`, `XLK`, `TLT`, `USO` | Structural H1 Donchian breakout + multi-horizon trend z-score + MT5 carry & external JPY macro gates | **24H FX/Metals:** Mon–Thu `06:00–16:00 UTC`<br>**US ETFs:** Mon–Thu `09:35–15:30 NY`<br>**FOMC:** `15:00–15:35 NY` on FOMC dates | `0.80%` base risk governed by 10% vol target & DD tiers; `2.5× ATR` initial SL (`2.0×` FOMC); uncapped `4.0× ATR` Chandelier trail (`3.0×` FOMC); **Fri `15:45 NY` flat** |
| **2. Pair-Specific `GBPJPY` Purged Walk-Forward MTF + Macro + COT (+ VIX Unwind)** | `trading/pair_specific_gbpjpy_eurusd_engine.py`<br>`trading/pair_specific_gj_eu_config.json` | `777490` | **FX:** `GBPJPY` only | Shallow regularized LightGBM trained on M15 microstructure, H1/H4/D1/W1 momentum, `GBPUSD`/`USDJPY` legs, sovereign yields, credit/VIX, oil & CFTC COT + VIX/Credit unwind short | Mon–Thu `06:00–16:00 UTC` | `1.0×` governed risk (`~0.80%` base); `2.2× ATR` SL / `2.8× ATR` TP (`2.0× / 3.5×` unwind); `24h` Long / `20h` Short max hold; **Fri `15:45 NY` flat** |
| **3. `BOXX` T-Bill Cash Sleeve** | `trading/boxx_cash_sleeve.py`<br>`trading/pair_specific_gj_eu_config.json` | `777899` | **US ETF:** `BOXX` | Idle cash accrual in 1–3 month Treasury box-spread ETF | Mon–Wed `09:40–15:30 NY` | Allocates up to `25%` of equity when alpha margin `<= 35%`; auto-liquidates during US RTH if alpha margin `>= 42%`; `SL=0, TP=0`; **Fri `15:45 NY` flat** |

### Explicitly Disabled / Excluded From Production
- **`EURUSD` Bond-Convergence H4 (`Magic 777390`):** Present in `trading/pair_specific_gbpjpy_eurusd_engine.py` with `"ENABLED": false` in `trading/pair_specific_gj_eu_config.json`. Kept off because 2024–2026 OOS was flat (`-1.4R` over 25 trades) and spot `EURUSD` carries negative swap on both directions.
- **8 Legacy M15 FX/Gold Engines & Paper Bot:** All `M1+M3`, `M15 Sniper`, `SM1 Swing`, and `EEI` sub-engines across `GBPUSD`, `EURUSD`, `USDJPY`, `XAUUSD`, `GBPJPY`, `AUDUSD`, `USDCAD`, and `EURJPY` plus `institutional_paper_bot.py` are disabled and excluded. Mean-reverting non-JPY G10 FX crosses (`EURUSD`, `GBPUSD`, `AUDUSD`, `USDCAD`) are not traded.

---

## 2. Permanent Risk & Execution Directives

All engines enforce four system-wide invariants:

1. **Breakeven Protection Permanently Disabled:** Neither initial entries nor trailing managers move stop-losses to `0.0R` entry price. Cross-Asset 80h uses a continuous `4.0× ATR_entry` ratcheting Chandelier trail (`3.0×` on FOMC Drift), `GBPJPY` uses fixed calibrated ATR brackets + time stops, and `BOXX` runs unstopped until margin release or Friday liquidation.
2. **Global Sunday Reopen Blackout (`< Monday 00:00:00 UTC`):** Both `CrossAsset80hBreakoutEngine` (`cross_asset_80h_breakout_bot.py`) and `PairSpecificGBPJPYEURUSDEngine` (`pair_specific_gbpjpy_eurusd_engine.py`) immediately return on Sunday (`weekday() == 6`), and `trading/market_hours.py` schedules weekend reopen strictly for Monday `00:00:00 UTC` (Tokyo Interbank Open) to avoid Sunday spread blowouts.
3. **Mandatory Friday `15:45 America/New_York` Auto-Liquidation:** Enforced redundantly in **three** places:
   - Inside `CrossAsset80hBreakoutEngine.enforce_friday_liquidation()`
   - Inside `PairSpecificGBPJPYEURUSDEngine.manage_positions_and_friday_flat()` and `manage_boxx_cash_sleeve()`
   - Independently by the dedicated daemon `trading/friday_auto_liquidate.py`, which closes all open MT5 market positions and removes all resting pending orders every Friday at `15:45 America/New_York` (`15:45 NY`) so the account enters every weekend 100% flat in cash with zero weekend gap risk.
   - Pre-liquidation entry cutoff on 24h markets is already gated by the Mon–Thu entry schedule (with `is_friday_pre_liquidation_cutoff` permitting intraday holds until `15:30 NY` when enabled).
4. **Central Mutex & Anti-Stacking Guards (`trading/bot_manager.py::send_guarded_entry_order`):**
   - **Market Hours & Rollover Shield:** Blocks entries when `is_market_open(symbol)` is closed, including the Mon–Thu `16:45–19:00 America/New_York` daily rollover blackout.
   - **Same-Symbol Stacking & Self-Hedge Veto:** Blocks duplicate same-direction entries across different engine families or duplicate magic numbers on the same symbol, and blocks opposing long + short positions on the same symbol.
   - **Cross-Pair JPY Factor Mutex:** Prevents holding conflicting JPY legs simultaneously (e.g., long `USDJPY` while short `EURJPY` or `AUDJPY` or `GBPJPY`, which would create a synthetic cross and burn two spreads/swaps).
   - **JPY Group Cap:** Hard-capped at **2 concurrent JPY positions across all live engines**.

---

## 3. Shared Infrastructure

### 3.1 Causal Portfolio Volatility-Target & Drawdown Risk Governor
Defined in `trading/portfolio_risk_governor.py` (configured via `RISK_GOVERNOR` in `trading/cross_asset_config.json`) and shared by **both** Sleeve 1 (`777880`) and Sleeve 2 (`777490`):

- **Persistent Daily NY-Close Equity Ledger:** Maintained at `data/cache/portfolio_equity_ledger.json` (last 90 NY trading days using a `17:00 NY` daily rollover boundary; auto-seeded from MT5 closed deal history via `mt5.history_deals_get` if no ledger exists).
- **Causal Volatility Calculation:** Uses **strictly completed** prior NY business days (`d < cur_ny_day`, reindexed across `pd.bdate_range` with forward-fill so flat non-trading days properly count as `0.0%` return days). Requires `>= 6` completed days (`>= 5` return observations); falls back to `target_vol_pct` (`10.0%`) until populated.
  $$\sigma_{20\text{d}} = \max\!\left(1.0\%,\; \text{std}(r_{[-20:]}) \times \sqrt{252} \times 100\%\right)$$
- **Vol-Capped Base Risk (`BASE_RISK_PCT = 0.80%`, `TARGET_ANNUAL_VOL_PCT = 10.0%`):**
  $$\text{vol\_capped\_risk} = \min\!\left(0.80\%,\; \text{clip}\!\left(\frac{10.0\%}{\sigma_{20\text{d}}},\; 0.35\%,\; 1.05\%\right)\right)$$
- **Convex Two-Tier Drawdown Governor** (measured from persistent High-Water Mark `hwm_capital`):
  - Drawdown `< 7.0%` → `dd_mult = 1.00`
  - Drawdown `>= 7.0%` (`DD_TIER1_PCT`) → `dd_mult = 0.65` (`DD_TIER1_MULT`)
  - Drawdown `>= 12.0%` (`DD_TIER2_PCT`) → `dd_mult = 0.45` (`DD_TIER2_MULT`)
- **Effective Risk Per Trade:** `round(vol_capped_risk * dd_mult, 4)`.
- **Optional Split-Account / Portable-Alpha Mode:** When `REFERENCE_CAPITAL_USD` is `null` (current live setting), sizing uses `mt5.account_info().equity` directly (`MT5_EQUITY` mode). When set alongside `MT5_ACTIVE_DEPOSIT_USD`, risk sizes off `REFERENCE_CAPITAL_USD + (mt5_equity - MT5_ACTIVE_DEPOSIT_USD)` while actual MT5 margin guards continue binding to real MT5 equity.

### 3.2 External Macro & Scheduled FOMC Feed
Defined in `trading/external_macro_feed.py`, cached for **4 hours** (`14,400s`) at `data/cache/live_external_macro_snapshot.json`:

- **Primary Source (`yfinance` daily close, period `9mo`):** Tickers `^FVX` (5Y US Treasury yield), `^TNX` (10Y US Treasury yield), `^VIX`, `DX-Y.NYB` (`DXY`), `TIP`, `IEF`, `HYG`, `HG=F` (Copper), `GLD` (Gold). If NY local time is before `17:00`, the current incomplete NY day is dropped so partial intraday bars never pollute daily z-scores.
- **Automatic MT5 ETF Fallback:** If external network fetch fails, derives proxies from completed MT5 `D1` bars (`IEF` inverse log-return for yield, `TIP/IEF` for breakeven, `HYG/IEF` for credit risk appetite, `UUP` for `DXY`).
- **Standardized Z-Score Transforms (`60d` vol lookback):**
  - Diff z-score (`^FVX`, `^TNX`, `^VIX`): `clip((x[-1] - x[-1-L]) / (std(diff(x)[-60:]) * sqrt(L) + 1e-9), -4.0, +4.0)`
  - Log-return z-score (`TIP/IEF`, `HYG/IEF`, `DXY`, `HG=F/GLD`): `clip(ln(x[-1]/x[-1-L]) / (std(ln_ret[-60:]) * sqrt(L) + 1e-9), -4.0, +4.0)`
- **Computed Macro Drivers:**
  - `us_yield_z20 = 0.5 * z_diff(^FVX, 20) + 0.5 * z_diff(^TNX, 20)`
  - `breakeven_z20 = z_logret(TIP / IEF, 20)`
  - `real_yield_z20 = clip(us_yield_z20 - 0.7 * breakeven_z20, -4.0, +4.0)`
  - `vix_shock_z5 = z_diff(^VIX, 5)`
  - `credit_risk_on_z20 = clip(0.7 * z_logret(HYG / IEF, 20) - 0.3 * vix_shock_z5, -4.0, +4.0)`
  - `dxy_z20 = z_logret(DX-Y.NYB, 20)`
  - `cyclical_cu_au_z20 = z_logret(HG=F / GLD, 20)`
- **JPY Rate-Differential & Credit Alignment Score (`compute_jpy_macro_score`):**
  - **`USDJPY`:**
    $$\text{panic} = -0.50 \text{ if } (\text{VIX} > 25.0 \land \text{vix\_shock\_z5} > 1.0) \text{ else } 0.0$$
    $$\text{score} = \text{direction} \times \left(0.60 \cdot \text{us\_yield\_z20} + 0.25 \cdot \text{dxy\_z20} + 0.25 \cdot \text{credit\_risk\_on\_z20} + \text{panic}\right)$$
  - **`AUDJPY`, `GBPJPY`, `EURJPY`:**
    $$\text{panic} = -0.80 \text{ if } (\text{VIX} > 24.0 \land \text{vix\_shock\_z5} > 1.0) \text{ else } 0.0$$
    $$\text{score} = \text{direction} \times \left(0.55 \cdot \text{credit\_risk\_on\_z20} + 0.30 \cdot \text{us\_yield\_z20} + 0.25 \cdot \text{cyclical\_cu\_au\_z20} + \text{panic}\right)$$
- **Why Gold Has No Real-Yield Veto:** `XAUUSD` and `XAUEUR` intentionally omit a real-yield veto because sovereign central-bank reserve accumulation decoupled Gold from rising US real yields across `2022–2026`.

### 3.3 MT5 Feed, Broker Timezone Normalization & Execution Helpers
- **`trading/mt5_feed.py`:** Deterministically maps MT5 broker server timestamps (`EET/EEST = America/New_York + 7h`, where `17:00 NY = 00:00 Broker Time` year-round across EST and EDT) into timezone-aware UTC via `mt5_server_ts_to_utc_series` and `mt5_ts_to_utc_epoch`. All strategy signals fetch `completed_only=True` (`start_pos=1`), ensuring zero intra-bar repainting.
- **`trading/bot_manager.py`:** Provides `get_filling_mode(symbol)` (FOK -> IOC -> RETURN bitmask auto-detection), `close_mt5_position()`, and `send_guarded_entry_order()`.
- **`trading/market_hours.py`:** DST-aware weekend schedule (normalized to `Mon 00:00 UTC` reopen), Mon–Thu `16:45–19:00 NY` rollover blackout, and asset session checks.

---

## 4. Strategy 1 — Cross-Asset 80h Donchian Breakout + 20d/60d Trend + Carry & Macro Gate (`Magic 777880`)

Implemented in `trading/cross_asset_80h_breakout_bot.py` and configured in `trading/cross_asset_config.json` (polling every `20s` on completed `H1` candles).

### 4.1 Active 9-Asset Universe & Structural Parameters

| Symbol | Class | Group | Group Cap | Donchian `ch_n` | ATR `atr_n` | Fast Trend `m20_n` | Slow Trend `m60_n` | Active Entry Session |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |
| `XAUUSD` | `24H` | `METAL` | `1` | `80` bars | `20` bars | `480` bars (20d) | `1440` bars (60d) | Mon–Thu `06:00–16:00 UTC` |
| `XAUEUR` | `24H` | `METAL` | `1` | `80` bars | `20` bars | `480` bars (20d) | `1440` bars (60d) | Mon–Thu `06:00–16:00 UTC` |
| `USDJPY` | `24H` | `JPY` | `2` | `80` bars | `20` bars | `480` bars (20d) | `1440` bars (60d) | Mon–Thu `06:00–16:00 UTC` |
| `EURJPY` | `24H` | `JPY` | `2` | `80` bars | `20` bars | `480` bars (20d) | `1440` bars (60d) | Mon–Thu `06:00–16:00 UTC` |
| `AUDJPY` | `24H` | `JPY` | `2` | `80` bars | `20` bars | `480` bars (20d) | `1440` bars (60d) | Mon–Thu `06:00–16:00 UTC` |
| `IVV` | `ETF` | `EQUITY` | `2` | `24` bars | `14` bars | `140` bars (20d RTH) | `420` bars (60d RTH) | Mon–Thu `09:35–15:30 NY` |
| `XLK` | `ETF` | `EQUITY` | `2` | `24` bars | `14` bars | `140` bars (20d RTH) | `420` bars (60d RTH) | Mon–Thu `09:35–15:30 NY` |
| `TLT` | `ETF` | `RATES` | `1` | `24` bars | `14` bars | `140` bars (20d RTH) | `420` bars (60d RTH) | Mon–Thu `09:35–15:30 NY` |
| `USO` | `ETF` | `COMMOD` | `1` | `24` bars | `14` bars | `140` bars (20d RTH) | `420` bars (60d RTH) | Mon–Thu `09:35–15:30 NY` |

- `GBPJPY` is disabled in `cross_asset_config.json` and handed off exclusively to Strategy 2 (`Magic 777490`).
- Global portfolio caps: `MAX_CONCURRENT_POSITIONS = 5` (for Magic `777880`), `MAX_TOTAL_MARGIN_FRAC = 0.60` (live non-`BOXX` margin capped at `60%` of equity).

### 4.2 Signal Logic & Filters

On each asset using completed `H1` bars (`count = m60_n + 200`):

1. **ATR & Realized Volatility:**
   - `TR_t = max(High - Low, |High - Close_{t-1}|, |Low - Close_{t-1}|)`
   - `ATR = SMA(TR, atr_n)[-1]`
   - `r_vol = std(ln(Close / Close_{t-1}), window=5*atr_n, min_periods=2*atr_n)[-1]`
2. **Multi-Horizon Trend Z-Score (`TREND_Z_THRESH = 0.30`):**
   $$m_{20,z} = \frac{\ln(C_{-1} / C_{-1-m_{20}})}{r_{\text{vol}}\sqrt{m_{20}} + 10^{-9}}, \quad m_{60,z} = \frac{\ln(C_{-1} / C_{-1-m_{60}})}{r_{\text{vol}}\sqrt{m_{60}} + 10^{-9}}$$
   $$m_{\text{tr}} = 0.5\,m_{20,z} + 0.5\,m_{60,z}$$
3. **Strict Prior-Channel Breakout:** Using the `ch_n` completed bars immediately *prior* to the signal bar (`bars.iloc[-1 - ch_n : -1]`):
   - **BUY (`+1`):** `last_c > hi_ch` AND `m_tr > 0.30`
   - **SELL (`-1`):** `last_c < lo_ch` AND `m_tr < -0.30`
4. **Live MT5 Swap/Carry Gate:** Queries live broker `swap_long` / `swap_short` (`SWAP_MODE_POINTS` normalized by initial `sl_dist = 2.5 * atr_v`). Vetoes entry if:
   $$\text{sw\_dir\_r} < -0.020\text{ R/night} \quad \text{AND} \quad |m_{\text{tr}}| < 1.25$$
5. **External JPY Macro Gate (`ENABLE_EXTERNAL_JPY_MACRO_GATE = true`):** For `JPY` assets, computes `compute_jpy_macro_score(sym, direction, macro_snap)` and vetoes if `macro_sc < -0.20`.
6. **Cross-Engine JPY Direction Mutex:** Vetoes if any live MT5 position on another `*JPY` symbol holds the opposite direction.
7. **Candidate Ranking:** Unblocked simultaneous breakouts are sorted descending by:
   $$\text{priority} = |m_{\text{tr}}| + 15.0 \cdot \text{sw\_dir\_r} + \left(0.75 \cdot \text{macro\_sc} \text{ if JPY else } 0\right)$$
   Evaluated strictly once per completed `H1` bar (`last_eval_bar` guard) while respecting per-group caps across **all** live engines on the MT5 account.

### 4.3 Sub-Strategy: Scheduled FOMC Bond-Confirmed Drift
Implemented in `trading/external_macro_feed.py::evaluate_fomc_bond_confirmed_drift()` (`ENABLE_FOMC_BOND_DRIFT = true`, priority `5.0`):

- **Trigger Window:** Only on official Federal Reserve FOMC decision dates (`2025–2027` calendar in `FOMC_DECISION_DATES`) between **`15:00` and `15:35 America/New_York`**, immediately after the `14:00 NY` H1 announcement bar has closed.
- **Bond Verification:** Fetches live completed `H1` `IEF` (7–10Y US Treasury ETF) and `UUP` (US Dollar ETF), confirms the latest completed `IEF` bar is today's `14:00 NY` bar, and requires:
  - `|ief_r1| >= 0.0018` (`>= 0.18%` 1-hour move; `> +0.18%` = dovish yield drop -> **BUY `XAUUSD` / SELL `USDJPY`**; `< -0.18%` = hawkish jump -> **SELL `XAUUSD` / BUY `USDJPY`**).
  - **`UUP` Non-Contradiction:** Must not contradict `IEF` (`dovish` blocked if `uup_r1 > +0.15%`; `hawkish` blocked if `uup_r1 < -0.15%`).
  - **Target Candle Confirmation:** Target asset's `14:00 NY` H1 bar body `(Close - Open) * ev_dir >= 0.60 * ATR(20)`.
- **Geometry:** `sl_dist = 2.0 * atr_v`, `trail_mult = 3.0` (`tag = "FOMC_DRIFT"`).

### 4.4 Position Sizing, Broker Quirks & Chandelier Exit Management
- **Dynamic Lot Sizing (`calculate_lot_and_margin`):**
  - `target_risk_usd = sizing_capital * (effective_risk_pct / 100.0)`
  - `XAUUSD` explicitly overrides broker `trade_tick_value` to `100.0` USD/price-unit/lot (`100 oz` contract where MT5 reports `0.10` per `0.01` tick).
  - **1:1 Cash-Margin ETF Notional Cap (`class == "ETF"`):** Caps notional at `min(sizing_cap * 0.18, equity * 0.25)` so cash-margined ETFs (`IVV`, `XLK`, `TLT`, `USO`) never monopolize margin.
  - Pre-flight margin verification via `mt5.order_check` enforces `used_margin + est_margin <= 0.60 * equity` (excluding `BOXX` sleeve margin).
- **Exit Rules & Continuous Ratcheting Trail:**
  - **Initial Order:** Market entry (`deviation=20`), `SL = entry -/+ 2.5 * ATR` (`2.0 * ATR` for FOMC Drift), `TP = 0.0` (uncapped runner).
  - **Ratcheting Chandelier Stop (`manage_trailing_stops`):** Checked every `20s` using live tick `bid` (Long) / `ask` (Short) whenever market/ETF RTH (`09:31–15:58 NY`) is open. Updates `best_p` and moves SL strictly in the favorable direction whenever it improves current SL by `>= 3 * point` and stays `>= 5 * point` from live quote:
    - **BUY:** `new_sl = round(best_p - trail_mult * atr_entry, digits)`
    - **SELL:** `new_sl = round(best_p + trail_mult * atr_entry, digits)`
  - **Restart Persistence:** State is persisted atomically to `trading/cross_asset_80h_state.json`. If a position exists without local state after a restart/recovery, `manage_trailing_stops()` reconstructs `atr_entry` and `best_p` from `H1` bars since `p.time`.
  - **Weekend Flat:** Closed every Friday at `15:45 NY`.

---

## 5. Strategy 2 — Pair-Specific `GBPJPY` Purged Walk-Forward MTF + Macro + COT (`Magic 777490`)

Implemented in `trading/pair_specific_gbpjpy_eurusd_engine.py`, configured in `trading/pair_specific_gj_eu_config.json`, running on a `30s` polling loop (strictly evaluating completed `H1` and `M15` bars).

### 5.1 Purged Walk-Forward Model Specification
- **Artifact:** `models/gbpjpy_purged_wf_lgbm_2026h2.joblib` (`60,842` bytes).
- **Estimator:** `LGBMRegressor` with heavy shallow-tree regularization:
  - `n_estimators = 80`, `learning_rate = 0.03`, `max_depth = 3`, `num_leaves = 7`
  - `min_child_samples = 250`, `subsample = 0.7`, `colsample_bytree = 0.7`, `reg_lambda = 30.0`
- **Training Discipline (`2022-07-01` to `2026-06-25`):** 4-year trailing window with a mandatory **5-day (120-hour) purge/embargo gap** before fold start so 24h forward target labels never cross fold boundaries. Designed for semi-annual walk-forward retraining (`YYYY-01-01` / `YYYY-07-01`).
- **Prediction Standardization:**
  $$x_{\text{std}} = \frac{x - \mu_{\text{train}}}{\sigma_{\text{train}}}, \quad \text{PredZ} = \frac{\hat{y}(x_{\text{std}}) - \hat{\mu}_{\text{train}}}{\hat{\sigma}_{\text{train}}}$$
  (`pred_train_mean = 0.049015`, `pred_train_std = 0.120651`).

### 5.2 Exact 24-Feature Input Vector

| Category | Features | Computation Window & Normalization |
| :--- | :--- | :--- |
| **Multi-Timeframe Return Z-Scores (7)** | `ret_4h_z`, `ret_12h_z`, `ret_1d_z`, `ret_5d_z`, `ret_20d_z`, `ret_60d_z`, `ret_12w_z` | Log returns over `4, 12, 24, 120, 480, 1440, 2880` H1 bars normalized by `std(r1[-120:]) * sqrt(k)`, clipped `[-4, +4]` |
| **H1 Regime & Oscillators (4)** | `live_vr_12h`, `rsi2_h1`, `rsi14_h1`, `bb_z24` | 12h variance ratio over 480 bars clipped `[0.4, 2.0]`; Wilder EMA RSI(2) & RSI(14) centered as `(RSI - 50)/25`; 24h Bollinger z-score clipped `[-4, +4]` |
| **M15 Microstructure (3)** | `m15_wick_norm`, `m15_rv_ratio`, `m15_up_centered` | Last 4 completed M15 bars: net lower-vs-upper wick imbalance `/ ATR20` (`[-3, +3]`), `sqrt(sum(body^2)) / ATR20` (`[0, 4]`), fraction of up M15 bars `- 0.5` |
| **Dual-Leg FX Factorization (4)** | `gu_z24`, `uj_z24`, `gu_z4`, `uj_z4` | Completed H1 log-return z-scores (`24h` and `4h`) of underlying component legs `GBPUSD` and `USDJPY` |
| **Sovereign Yields, Credit, VIX & Oil (5)** | `uk_gilt_yield_z20`, `us_yield_5_10_z20`, `credit_hyg_ief_z20`, `vix_z5`, `oil_z20` | Completed prior NY daily closes via `yfinance` (cached `4h`): `-z_logret(IGLT.L, 20)` (UK Gilt yield proxy), `0.5*z_diff(^FVX, 20) + 0.5*z_diff(^TNX, 20)`, `z_logret(HYG/IEF, 20)`, `z_diff(^VIX, 5)`, `z_logret(CL=F, 20)` |
| **Official CFTC COT Positioning (1)** | `cot_gbpjpy_comm_z_spread` | Latest causally available Friday release (`avail_time <= now_utc`) from `data/goal_eurusd_gbpjpy/cftc_cot_fx_2014_2026.parquet` (`GBP - JPY` commercial hedger net % OI z-spread) |

### 5.3 Entry Paths, Carry Asymmetry & Risk-Off Vetoes
Evaluated Mon–Thu `06:00–16:00 UTC` (once per completed H1 bar via `last_eval_bar` guard, provided zero live `GBPJPY` positions exist across **all** engines):

1. **Path A — `VIX / Credit` Crash Unwind Short (Checked First, Tag `GJ_VIX_UNWIND`):**
   - Condition: `(vix_z5 > +1.0 OR credit_hyg_ief_z5 < -1.0)` **AND** `PredZ <= 0.00` (`VIX_UNWIND_MAX_PRED_Z = 0.0`, preventing unwind shorts against a bullish ML signal) **AND** `Close < min(Low[-25:-1])` (24h low break) **AND** `gu_z4 < 0` **AND** `uj_z4 < 0`.
   - Fires `SELL` with tighter crisis brackets: **`SL = 2.0 * ATR20`**, **`TP = 3.5 * ATR20`**.
2. **Path B — Purged Walk-Forward ML Signal (Tag `GJ_WF`):**
   - **Asymmetric Long Threshold:** `PredZ >= +1.30` (collects positive MT5 overnight swap on `GBPJPY` longs).
   - **Asymmetric Short Threshold:** `PredZ <= -1.60` (demands higher conviction to overcome negative short swap).
   - Brackets: **`SL = 2.2 * ATR20`**, **`TP = 2.8 * ATR20`**.
3. **Hard Risk-Off Long Veto (`ENABLE_RISK_OFF_LONG_VETO = true`):** Blocks any `GBPJPY BUY` if **any** of the following triggers:
   - `vix_z5 > +1.0`
   - `credit_hyg_ief_z5 < -1.0`
   - `compute_jpy_macro_score("GBPJPY", +1, external_macro_snapshot) < -0.20`
4. **Portfolio & JPY Guards:** Checks opposing active `*JPY` exposure (`JPY_MUTEX`), enforces max `2` concurrent non-`GBPJPY` JPY positions (`JPY_GROUP_CAP`), sizes using `get_governed_risk_state() * 1.00`, and submits through `send_guarded_entry_order`.

### 5.4 Exits
- Initial broker `SL` and `TP` set on entry (`round(..., 3)`).
- **Max-Hold Time Expiry:** Closed automatically by `manage_positions_and_friday_flat()` after **`24 hours`** (Long) or **`20 hours`** (Short) using true UTC elapsed time.
- **No Breakeven Protection** and **Mandatory Friday `15:45 NY` Auto-Liquidation**.

---

## 6. Strategy 3 — `BOXX` T-Bill Cash Sleeve (`Magic 777899`)

Implemented in `trading/boxx_cash_sleeve.py` and executed every `30s` inside `PairSpecificGBPJPYEURUSDEngine.run_cycle()`:

- **Purpose:** Harvests T-bill box-spread yield (`~0.8 bps` live spread, `0.0` overnight swap) on unencumbered cash while preserving full margin priority for alpha engines.
- **Allocation Rule (`TARGET_EQUITY_FRAC = 0.25`):** Targets `min(0.25 * equity, max(0, 0.65 * equity - alpha_margin))`, floored to integer shares.
- **Entry Gate (Mon–Wed `09:40–15:30 America/New_York` ONLY):** Never enters on Thursday or Friday so weekly Friday liquidation never pays entry/exit spread for `< 2` days of accrual.
  - Requires active non-`BOXX` margin `alpha_margin / equity <= 35%` (`MAX_ALPHA_MARGIN_TO_ENTER_FRAC`).
  - Requires live `BOXX` spread `ask - bid <= USD 0.03` (`MAX_SPREAD_USD`).
- **Alpha-Priority Margin Release:** While holding `BOXX` during US RTH (`Mon–Fri 09:35–15:55 NY`), if active alpha margin rises to `>= 42%` of equity (`RELEASE_ALPHA_MARGIN_FRAC`), immediately closes `BOXX` at market to free cash for alpha engines.
- **Stops & Weekend Flat:** `SL = 0.0`, `TP = 0.0` (breakeven permanently disabled); liquidated every **Friday at `15:45 NY`**.

---

## 7. Operations, Supervision & Running Locally

### Repository File Map

```text
forex-mitra/
├── STRATEGIES.md                                  # Full technical specification (this document)
├── README.md                                      # Overview & quickstart
├── requirements.txt                               # Runtime Python dependencies
├── trading/
│   ├── master_watchdog.py                         # Process & MT5 supervisor (restarts dropped engines)
│   ├── cross_asset_80h_breakout_bot.py            # Sleeve 1: Cross-Asset 80h Donchian + FOMC Drift
│   ├── cross_asset_config.json                    # Universe, thresholds, carry/macro gates, Risk Governor
│   ├── pair_specific_gbpjpy_eurusd_engine.py      # Sleeve 2 (+ disabled EU Bond-Conv) & Sleeve 3 loop
│   ├── pair_specific_gj_eu_config.json            # GBPJPY WF, risk-off veto, EURUSD disabled, BOXX cfg
│   ├── boxx_cash_sleeve.py                        # Sleeve 3: BOXX 25% idle-cash allocation logic
│   ├── friday_auto_liquidate.py                   # Independent Friday 15:45 NY full liquidation daemon
│   ├── portfolio_risk_governor.py                 # Causal 10% vol-target + 2-tier DD risk governor
│   ├── external_macro_feed.py                     # yfinance macro + MT5 ETF fallback + FOMC H1 trigger
│   ├── bot_manager.py                             # MT5 filling modes, close helper & guarded mutex router
│   ├── mt5_feed.py                                # EET/EEST->UTC timestamps & completed-bar loader
│   ├── market_hours.py                            # Sunday blackout, rollover shield & DST session clock
│   └── cooldown_manager.py                        # Disk + MT5 deal-history cooldown persistence
├── models/
│   └── gbpjpy_purged_wf_lgbm_2026h2.joblib        # Purged 5d-embargo LightGBM bundle (2026 H2)
└── data/
    └── goal_eurusd_gbpjpy/
        └── cftc_cot_fx_2014_2026.parquet          # Historical & weekly CFTC COT FX positioning
```

### Runtime Logs & State Files (Generated at Runtime, Git-Ignored)
- Logs: `logs/watchdog.log`, `logs/cross_asset_80h_bot.log`, `logs/pair_specific_gj_eu.log`, `logs/friday_close.log`, `trading/live_engine.log`
- State & Telemetry: `trading/cross_asset_80h_state.json`, `trading/telemetry_CROSS_ASSET_80H.json`, `trading/telemetry_PAIR_SPECIFIC_GJ_EU.json`, `data/cache/live_external_macro_snapshot.json`, `data/cache/live_gj_eu_sovereign_cot.json`, `data/cache/portfolio_equity_ledger.json`

### Running on Windows + MetaTrader 5
```powershell
pip install -r requirements.txt
# Ensure MetaTrader 5 terminal (terminal64.exe) is open, logged in, with Algo Trading enabled (Ctrl+E)
pythonw -u trading/master_watchdog.py
```
On Windows, running `trading/master_watchdog.py` under a persistent Windows Scheduled Task (`pythonw.exe -u trading/master_watchdog.py` with `ExecutionTimeLimit = 0`, at logon) keeps the watchdog, MT5 monitor, both alpha/cash engines, and `friday_auto_liquidate.py` alive across terminal/IDE restarts.
