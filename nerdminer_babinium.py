import socket
import json
import hashlib
import time
import threading
import multiprocessing
import os
import sys

# --- Windows Compatibility ---
if os.name == 'nt':
    try:
        import curses
    except ImportError:
        print("\n[!] Error: El módulo 'curses' no está instalado.")
        print("[!] Para ejecutar este minero en Windows, por favor instala 'windows-curses' ejecutando:")
        print("\n    pip install windows-curses\n")
        sys.exit(1)
else:
    import curses

import collections
import math
from datetime import datetime

# --- Configuration ---
POOL_URL = "solo.ckpool.org"
POOL_PORT = 3333
CONFIG_FILE = "config.txt"

# --- Shared State ---
stats = {
    "uptime_start": time.time(),
    "hashrate": 0,
    "best_diff": 0,
    "valid_shares": 0,
    "status": "Iniciando...",
    "intensity": 0,
    "wallet": "",
    "hashes": 0,
    "jobs": collections.deque(maxlen=3),
}

# --- Stratum Client Logic ---
def stratum_client(wallet, update_queue, job_queue):
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect((POOL_URL, POOL_PORT))
            
            f = sock.makefile('r', encoding='utf-8')
            sock.sendall(json.dumps({"id": 1, "method": "mining.subscribe", "params": []}).encode() + b'\n')
            sock.sendall(json.dumps({"id": 2, "method": "mining.authorize", "params": [wallet, "x"]}).encode() + b'\n')

            update_queue.put(("status", "Conectado"))

            while True:
                line = f.readline()
                if not line: break
                msg = json.loads(line)
                
                if msg.get("method") == "mining.notify":
                    job_queue.put(msg["params"])
                    # Use Job ID (params[0]) instead of hash slice to show more 'active' data
                    update_queue.put(("block", msg["params"][0]))
                    update_queue.put(("status", "Minando Trabajo"))
                elif msg.get("method") == "mining.set_difficulty":
                    update_queue.put(("difficulty", msg["params"][0]))
                    
        except Exception as e:
            update_queue.put(("status", f"Reconectando..."))
        
        time.sleep(5)

# --- Hashing Worker ---
def hash_worker(job_queue, update_queue, intensity):
    target_ratio = intensity / 100.0
    # Reference difficulty for 100% (Bitcoin Network Target)
    REF_DIFF = 88000000000000.0 
    
    current_job = None
    while True:
        try:
            try:
                new_job = job_queue.get_nowait()
                current_job = new_job
            except Exception:
                pass
            
            if not current_job or target_ratio <= 0:
                time.sleep(0.1)
                continue

            prevhash = current_job[1]
            chunk_size = 500
            
            start_work = time.time()
            start_nonce = int(start_work * 1000) % 0xFFFFFFFF
            
            max_chunk_diff = 1e-15 # Very low starting floor
            
            # Standard Target for Diff 1
            t1 = 0x00000000FFFF0000000000000000000000000000000000000000000000000000
            
            for i in range(chunk_size):
                nonce = (start_nonce + i) % 0xFFFFFFFF
                header_sim = f"{prevhash}{nonce:08x}".encode()
                h = hashlib.sha256(hashlib.sha256(header_sim).digest()).digest()
                
                hash_int = int.from_bytes(h, 'big')
                if hash_int > 0:
                    diff = t1 / hash_int
                    if diff > max_chunk_diff: max_chunk_diff = diff

            # Update hashrate and best diff score
            update_queue.put(("hash", chunk_size))
            update_queue.put(("diff_score", max_chunk_diff))
            
            work_duration = time.time() - start_work
            if target_ratio < 1.0:
                sleep_duration = work_duration * (1.0 - target_ratio) / target_ratio
                if sleep_duration > 0:
                    time.sleep(min(sleep_duration, 0.5))
                
        except Exception:
            time.sleep(0.1)

# --- TUI Logic ---
def draw_ui(stdscr, update_queue, intensity):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.init_pair(1, curses.COLOR_CYAN, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLACK)

    start_time = time.time()
    hashes = 0
    best_diff = 1e-12 
    current_luck_val = 1e-12
    status = "Iniciando..."
    block_id = "-------"
    hr_history = collections.deque(maxlen=10)
    luck_avg_history = collections.deque(maxlen=10) # For pulse comparison

    last_hr_update = time.time()
    hashes_since_update = 0
    spinner = ["|", "/", "-", "\\"]
    spin_idx = 0
    
    # 100% Luck Bar Reference (Bitcoin Network Diff)
    REF_DIFF = 88000000000000.0

    while True:
        while True:
            try:
                msg = update_queue.get_nowait()
                msg_type, val = msg
                if msg_type == "status": 
                    # Simplify mining status
                    new_stat = str(val)
                    if "Trabajo" in new_stat or "Mining" in new_stat:
                        status = "Minando"
                    else:
                        status = new_stat
                elif msg_type == "block": block_id = str(val)
                elif msg_type == "hash": 
                    hashes += int(val)
                    hashes_since_update += int(val)
                elif msg_type == "diff_score":
                    d = float(val)
                    current_luck_val = d # Real-time pulse value
                    if d > best_diff: best_diff = d
            except Exception:
                break

        now = time.time()
        if now - last_hr_update >= 0.1: # Faster UI update for pulse
            if now - last_hr_update >= 1.0:
                hr = hashes_since_update / (now - last_hr_update)
                hr_history.append(hr)
                hashes_since_update = 0
                spin_idx = (spin_idx + 1) % 4
                # Maintain a short history of recent bests for the 'pulse' average
                luck_avg_history.append(current_luck_val)
                last_hr_update = now

        avg_hr = sum(hr_history) / max(1, len(hr_history))
        avg_recent_luck = sum(luck_avg_history) / max(1, len(luck_avg_history))
        
        # Luck Bar: Fixed width (24 chars) + Dynamic Pulse
        luck_len = 24
        min_log = -12.0
        max_log = math.log10(REF_DIFF)
        
        log_best = math.log10(max(1e-12, float(best_diff)))
        progress_best = (log_best - min_log) / (max_log - min_log)
        luck_base_pct = (max(0, progress_best) ** 0.5) * 100.0
        
        # Visual pulse: sensitive to movement relative to avg
        pulse_movement = 0
        if current_luck_val > avg_recent_luck:
            pulse_movement = 2 # Green boost
        elif current_luck_val < avg_recent_luck:
            pulse_movement = -2 # Red retreat

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        box_w = min(w - 2, 48)
        if box_w < 20: box_w = 20
        
        stdscr.attron(curses.color_pair(1))
        stdscr.addstr(0, 0, "╔" + "═" * box_w + "╗")
        box_h = 11 
        for i in range(1, box_h):
            if i < h:
                stdscr.addstr(i, 0, f"║{' '*(box_w)}║")
        if box_h < h:
            stdscr.addstr(box_h, 0, "╚" + "═" * box_w + "╝")
        stdscr.attroff(curses.color_pair(1))

        # Header
        stdscr.addstr(0, 2, f" NerdMiner Babinium [{spinner[spin_idx]}] ", curses.A_BOLD)
        if box_w > 35:
            stdscr.addstr(0, box_w-10, " [q] Salir ", curses.A_BOLD)
        
        uptime = int(now - start_time)
        hours, rem = divmod(uptime, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        try:
            if h > 2: stdscr.addstr(2, 2, f"Tiempo:   {uptime_str}")
            if h > 3: stdscr.addstr(3, 2, f"ID Tarea: {block_id}")
            
            if h > 5:
                stdscr.addstr(5, 2, "Hashrate: ", curses.color_pair(4))
                if avg_hr > 1000:
                    stdscr.addstr(f"{avg_hr/1000:.2f} KH/s", curses.color_pair(2))
                else:
                    stdscr.addstr(f"{avg_hr:.2f} H/s", curses.color_pair(2))

            if h > 7:
                stdscr.addstr(7, 2, "Suerte: [")
                fill_base = int((luck_base_pct / 100.0) * luck_len)
                
                # Calculate color segments
                # Base is white (pair 0 or 4), Pulse is Green (3) or Red (2)
                # We always draw 24 characters total.
                
                if pulse_movement > 0:
                    # Case: Récord + Extensión Verde
                    ext = min(pulse_movement, luck_len - fill_base)
                    stdscr.addstr("█" * fill_base)
                    stdscr.addstr("█" * ext, curses.color_pair(3))
                    stdscr.addstr("░" * (luck_len - fill_base - ext))
                elif pulse_movement < 0:
                    # Case: Récord con punta Roja (retroceso)
                    ret = min(abs(pulse_movement), fill_base)
                    stdscr.addstr("█" * (fill_base - ret))
                    stdscr.addstr("█" * ret, curses.color_pair(2))
                    stdscr.addstr("░" * (luck_len - fill_base))
                else:
                    # Case: Solo Récord
                    stdscr.addstr("█" * fill_base)
                    stdscr.addstr("░" * (luck_len - fill_base))
                
                stdscr.addstr("]")

            if h > 8: stdscr.addstr(8, 2, f"CPU: {intensity}%")
            if h > 10: 
                # Display current status, clean only if it's mining
                display_stat = status
                if "Minando" in display_stat: display_stat = "Minando"
                stdscr.addstr(10, 2, f"Estado: {display_stat[:box_w-9]}")
        except curses.error:
            pass

        stdscr.refresh()
        if stdscr.getch() == ord('q'):
            break
        time.sleep(0.1)

def main():
    wallet = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    intensity = 50
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=")
                        if k == "wallet": wallet = v
                        if k == "intensity": intensity = int(v)
    except Exception:
        pass

    update_queue = multiprocessing.Queue()
    job_queue = multiprocessing.Queue()
    
    num_workers = multiprocessing.cpu_count()
    workers = []
    for _ in range(num_workers):
        p = multiprocessing.Process(target=hash_worker, args=(job_queue, update_queue, intensity), daemon=True)
        p.start()
        workers.append(p)

    st = threading.Thread(target=stratum_client, args=(wallet, update_queue, job_queue), daemon=True)
    st.start()

    try:
        curses.wrapper(draw_ui, update_queue, intensity)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error: {e}")
    finally:
        for p in workers:
            try: p.terminate()
            except: pass

if __name__ == "__main__":
    main()

