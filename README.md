# forex-mitra

Production MetaTrader 5 systematic FX, precious metals, ETF, and cash-management strategies actively running in live trading.

> For full signal equations, model architecture, 24-feature definitions, risk-governor formulas, carry/macro gates, and exit rules, see **[STRATEGIES.md](STRATEGIES.md)**.

## Active Production Sleeves

1. **Cross-Asset 80h Donchian Breakout + 20d/60d Trend (+ Scheduled FOMC Drift)** — `Magic 777880`
   - **Instruments:** `USDJPY`, `EURJPY`, `AUDJPY` (`JPY` FX), `XAUUSD`, `XAUEUR` (Metals), `IVV`, `XLK`, `TLT`, `USO` (US ETFs)
   - **Guards:** Live MT5 carry/swap gate, external US yield/credit/DXY JPY macro gate, JPY direction mutex, 10% vol-target & 2-tier drawdown risk governor
   - **Exits:** `2.5× ATR` initial SL (`2.0×` FOMC Drift), continuous `4.0× ATR` Chandelier trailing stop (`3.0×` FOMC), Friday `15:45 NY` auto-flat
2. **Pair-Specific `GBPJPY` Purged Walk-Forward MTF + Macro + COT (+ VIX/Credit Unwind)** — `Magic 777490`
   - **Instruments:** `GBPJPY` (`EURUSD` Bond-Convergence `Magic 777390` included in code, disabled by design)
   - **Model:** 24-feature standardized shallow `LGBMRegressor` (`2026 H2`, 5-day purged embargo) + asymmetric carry thresholds (`PredZ >= +1.30` Long / `<= -1.60` Short) + VIX/Credit crash unwind short (gated by `PredZ <= 0.00`) + hard Risk-Off long veto
   - **Exits:** `2.2× ATR` SL / `2.8× ATR` TP (`2.0× / 3.5×` unwind), `24h` Long / `20h` Short max hold, Friday `15:45 NY` auto-flat
3. **`BOXX` T-Bill Box Cash Sleeve** — `Magic 777899`
   - **Instruments:** `BOXX`
   - **Allocation:** Parks `25%` idle cash during Mon–Wed `09:40–15:30 NY` when alpha margin `<= 35%`; auto-liquidates if alpha margin rises `>= 42%`; flattens Friday `15:45 NY`

## Core Safety Invariants

- **Breakeven protection is permanently disabled by design** across all strategies.
- **Global Sunday Reopen Blackout:** No entries prior to Monday `00:00:00 UTC`.
- **Mandatory Friday `15:45 America/New_York` Auto-Liquidation:** Enforced both inside every strategy engine and by the independent `trading/friday_auto_liquidate.py` daemon.

## Repository Structure

- `STRATEGIES.md` — Detailed technical strategy & risk specification
- `trading/` — Watchdog, execution engines, config JSONs, MT5 feed/order guards, macro & risk governors
- `models/gbpjpy_purged_wf_lgbm_2026h2.joblib` — Purged walk-forward LightGBM model bundle (`2026 H2`)
- `data/goal_eurusd_gbpjpy/cftc_cot_fx_2014_2026.parquet` — CFTC COT FX positioning dataset

## Quickstart (Windows + MetaTrader 5)

```powershell
# 1. Install runtime dependencies
pip install -r requirements.txt

# 2. Ensure MetaTrader 5 terminal (terminal64.exe) is open, logged in, and Algo Trading is enabled (Ctrl+E)

# 3. Launch the Master Watchdog (runs Cross-Asset 80h, GBPJPY/BOXX, and Friday Auto-Liquidator)
python -u trading/master_watchdog.py
# Or run silently in background:
pythonw -u trading/master_watchdog.py
```
