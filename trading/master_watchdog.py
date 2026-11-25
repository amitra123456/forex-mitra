import subprocess
import time
import sys
import os
from datetime import datetime

# Paths to the live trading engines
# Legacy pair bots & paper bot are disabled in trading/dual_config.json and pruned to avoid unnecessary CPU/RAM load.
BOTS = [
    # {"name": "GBPUSD_Engine", "script": "trading/gbpusd_bot.py", "process": None},
    # {"name": "EURUSD_Engine", "script": "trading/eurusd_bot.py", "process": None},
    # {"name": "USDJPY_Engine", "script": "trading/usdjpy_bot.py", "process": None},
    # {"name": "XAUUSD_Engine", "script": "trading/xauusd_bot.py", "process": None},
    # {"name": "GBPJPY_Engine", "script": "trading/gbpjpy_bot.py", "process": None},
    # {"name": "AUDUSD_Engine", "script": "trading/audusd_bot.py", "process": None},
    # {"name": "USDCAD_Engine", "script": "trading/usdcad_bot.py", "process": None},
    # {"name": "EURJPY_Engine", "script": "trading/eurjpy_bot.py", "process": None},
    # {"name": "Institutional_Paper_Bot", "script": "trading/institutional_paper_bot.py", "process": None},
    {"name": "Friday_Liquidator", "script": "trading/friday_auto_liquidate.py", "process": None},
    # All 3 active sleeves (777880, 777490, 777899) are self-managed; legacy trailing manager is omitted from this repo.
    # {"name": "Production_Trailing_Manager", "script": "trading/production_trailing_manager.py", "process": None},
    {"name": "CrossAsset_80h_Breakout_Engine", "script": "trading/cross_asset_80h_breakout_bot.py", "process": None},
    {"name": "PairSpecific_GBPJPY_EURUSD_Engine", "script": "trading/pair_specific_gbpjpy_eurusd_engine.py", "process": None},
]

WATCHDOG_LOG_PATH = os.path.join(
    os.path.abspath(os.path.dirname(os.path.dirname(__file__))),
    "logs",
    "watchdog.log",
)

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [WATCHDOG] {msg}"
    try:
        os.makedirs(os.path.dirname(WATCHDOG_LOG_PATH), exist_ok=True)
        with open(WATCHDOG_LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:
        pass

def find_running_script_proc(script_name):
    try:
        import psutil
        script_base = os.path.basename(script_name if isinstance(script_name, str) else script_name[0])
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['name'] and 'python' in proc.info['name'].lower():
                    cmdline = ' '.join(proc.info['cmdline'] or [])
                    if script_base in cmdline and proc.pid != os.getpid():
                        return proc
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass
    return None

def start_bot(bot_config):
    existing = find_running_script_proc(bot_config['script'])
    if existing:
        log(f"{bot_config['name']} is already running (PID {existing.pid}). Adopting process...")
        bot_config['process'] = existing
        return existing

    log(f"Starting {bot_config['name']} ({bot_config['script']})...")
    
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    log_file = open('trading/live_engine.log', 'a', encoding='utf-8', errors='replace')
    
    creationflags = 0x08000000 | 0x00000008 | 0x00000200
    py_exe = os.path.join(os.path.dirname(sys.executable), "python.exe")
    exe_path = py_exe if os.path.exists(py_exe) else sys.executable
    process = subprocess.Popen(
        [exe_path] + (bot_config['script'] if isinstance(bot_config['script'], list) else [bot_config['script']]),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=os.path.abspath(os.path.dirname(os.path.dirname(__file__))),
        env=env,
        creationflags=creationflags
    )
    bot_config['process'] = process
    log(f"{bot_config['name']} started with PID {process.pid}.")
    return process

def is_process_dead(process):
    if process is None:
        return True
    if hasattr(process, 'poll'):
        return process.poll() is not None
    try:
        import psutil
        return not process.is_running() or process.status() in [psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD]
    except Exception:
        return True

MT5_EXE_PATH = r"C:\Program Files\MetaTrader 5\terminal64.exe"
_LAST_MT5_LAUNCH_TS = 0.0


def ensure_mt5_terminal_running():
    """Supervises MetaTrader 5 (terminal64.exe) and relaunches if offline."""
    global _LAST_MT5_LAUNCH_TS
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() == "terminal64.exe":
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        return

    now_ts = time.time()
    if now_ts - _LAST_MT5_LAUNCH_TS < 45.0:
        return
    if not os.path.exists(MT5_EXE_PATH):
        log(f"WARNING: terminal64.exe offline and executable missing at {MT5_EXE_PATH}")
        _LAST_MT5_LAUNCH_TS = now_ts
        return

    log("CRITICAL WARNING: MetaTrader 5 (terminal64.exe) is OFFLINE! Relaunching MT5...")
    _LAST_MT5_LAUNCH_TS = now_ts
    try:
        creationflags = 0x00000008 | 0x00000200
        subprocess.Popen([MT5_EXE_PATH], creationflags=creationflags, close_fds=True)
        log("MetaTrader 5 terminal64.exe relaunched by Watchdog.")
    except Exception as exc:
        log(f"ERROR relaunching MetaTrader 5: {exc}")


def main():
    existing_wd = find_running_script_proc("master_watchdog.py")
    if existing_wd:
        log(f"Master Watchdog already running (PID {existing_wd.pid}). Exiting duplicate instance.")
        return

    log("=== ANTIGRAVITY MASTER WATCHDOG INITIALIZED ===")
    ensure_mt5_terminal_running()
    
    # Initial startup of all bots
    for bot in BOTS:
        start_bot(bot)
        time.sleep(2) # Brief stagger to prevent MT5 connection race conditions

    log("All engines are live. Watchdog is now monitoring processes.")

    try:
        while True:
            ensure_mt5_terminal_running()
            for bot in BOTS:
                process = bot['process']
                
                # Check if process has terminated
                if is_process_dead(process):
                    exit_code = getattr(process, 'returncode', 'TERMINATED')
                    log(f"CRITICAL WARNING: {bot['name']} unexpectedly crashed or stopped (status/code: {exit_code})!")
                    log(f"Respawning {bot['name']} in 10 seconds...")
                    
                    time.sleep(10) # Wait a moment before restarting to avoid infinite rapid crash loops
                    start_bot(bot)
                    
            # Heartbeat check every 5 seconds
            time.sleep(5)
            
    except KeyboardInterrupt:
        log("Shutting down the Master System...")
        # Gracefully terminate all child processes
        for bot in BOTS:
            if bot['process'] and bot['process'].poll() is None:
                log(f"Terminating {bot['name']}...")
                bot['process'].terminate()
                
        log("All engines stopped. Watchdog offline.")

if __name__ == "__main__":
    main()
