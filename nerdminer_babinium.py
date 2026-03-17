import socket
import json
import hashlib
import struct
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
        print("[!] Para ejecutar este minero en Windows, instala 'windows-curses':")
        print("\n    pip install windows-curses\n")
        sys.exit(1)
else:
    import curses

import collections
import math

# --- Configuration ---
POOL_URL = "solo.ckpool.org"
POOL_PORT = 3333
CONFIG_FILE = "config.txt"

# ============================================================
# --- Crypto Utils ---
# ============================================================
def sha256d(data: bytes) -> bytes:
    """Doble SHA256, como lo requiere Bitcoin."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

def target_from_nbits(nbits_hex: str) -> int:
    """
    Convierte el campo 'nbits' (4 bytes, hex) al target numérico de 256 bits.
    Formato: [exponente 1 byte][mantissa 3 bytes]
    Ejemplo: '1d00ffff' -> target del bloque genesis
    """
    nbits = bytes.fromhex(nbits_hex)
    exponent = nbits[0]
    mantissa = int.from_bytes(nbits[1:4], 'big')
    target = mantissa * (2 ** (8 * (exponent - 3)))
    return target

# ============================================================
# --- Stratum Client ---
# Corre en un Thread. Lee líneas del servidor, envía submissions.
# ============================================================
def stratum_client(wallet: str, update_queue, job_queue, submit_queue):
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(60)
            sock.connect((POOL_URL, POOL_PORT))
            f = sock.makefile('r', encoding='utf-8')

            # 1. SUBSCRIBE
            sock.sendall(json.dumps({
                "id": 1, "method": "mining.subscribe", "params": []
            }).encode() + b'\n')

            resp = json.loads(f.readline())
            # result[0] = [[sub_details], extranonce1, extranonce2_size]
            # o result = [sub_details, extranonce1, extranonce2_size]
            result = resp.get("result", [])
            if isinstance(result[0], list):
                extranonce1 = result[1]
                extranonce2_size = result[2]
            else:
                # Formato alternativo de algunas pools
                extranonce1 = result[1]
                extranonce2_size = result[2]

            # 2. AUTHORIZE
            sock.sendall(json.dumps({
                "id": 2, "method": "mining.authorize",
                "params": [wallet, "x"]
            }).encode() + b'\n')

            update_queue.put(("status", "Conectado"))

            extranonce2_counter = 0

            while True:
                # Enviar submissions pendientes
                try:
                    while True:
                        sub = submit_queue.get_nowait()
                        payload = {
                            "id": 4, "method": "mining.submit",
                            "params": [wallet, sub[0], sub[1], sub[2], sub[3]]
                        }
                        sock.sendall(json.dumps(payload).encode() + b'\n')
                        update_queue.put(("status", "¡SOLUCION ENVIADA!"))
                except Exception:
                    pass

                # Leer mensaje del servidor (bloqueante con timeout)
                try:
                    sock.settimeout(45)
                    line = f.readline()
                    if not line:
                        break
                    msg = json.loads(line)
                except socket.timeout:
                    # Enviar keep-alive si hay silencio
                    try:
                        sock.sendall(json.dumps({"id": 99, "method": "mining.extranonce.subscribe", "params": []}).encode() + b'\n')
                    except Exception:
                        break
                    continue
                except Exception:
                    break

                method = msg.get("method")
                if method == "mining.notify":
                    params = msg["params"]
                    # Adjuntar extranonce1 y extranonce2_size al job
                    job = params + [extranonce1, extranonce2_size, extranonce2_counter]
                    extranonce2_counter = (extranonce2_counter + 1) & 0xFFFFFFFF
                    job_queue.put(job)
                    update_queue.put(("block", params[0]))
                    update_queue.put(("status", "Minando"))

                elif method == "mining.set_difficulty":
                    pass  # La dificultad ya viene en nbits del job

        except Exception:
            update_queue.put(("status", "Reconectando..."))
        finally:
            if sock:
                try: sock.close()
                except Exception: pass
        time.sleep(5)

# ============================================================
# --- Hash Worker ---
# Corre en un Process separado por cada núcleo de CPU.
# ============================================================
def hash_worker(job_queue, update_queue, submit_queue, intensity):
    target_ratio = max(0.01, min(1.0, intensity / 100.0))
    current_job = None

    # Referencia Diff 1 para la barra de suerte
    DIFF1 = 0x00000000FFFF0000000000000000000000000000000000000000000000000000

    while True:
        try:
            # Tomar el trabajo más reciente (descartar viejos)
            latest = None
            try:
                while True:
                    latest = job_queue.get_nowait()
            except Exception:
                pass
            if latest is not None:
                current_job = latest

            if current_job is None:
                time.sleep(0.2)
                continue

            # Desempaquetar el job
            # [job_id, prevhash, coinb1, coinb2, merkle_branch,
            #  version, nbits, ntime, clean_jobs,
            #  extranonce1, extranonce2_size, extranonce2_counter]
            if len(current_job) < 12:
                time.sleep(0.1)
                continue

            (job_id, prevhash, coinb1, coinb2, merkle_branch,
             version, nbits, ntime, clean_jobs,
             enonce1, enonce2_size, enonce2_base) = current_job

            # Calcular el target real de la red
            try:
                target = target_from_nbits(nbits)
            except Exception:
                time.sleep(0.1)
                continue

            # Construir extranonce2 con el tamaño correcto
            enonce2 = format(enonce2_base, '0' + str(enonce2_size * 2) + 'x')

            # --- 1. Coinbase ---
            coinbase_bytes = bytes.fromhex(coinb1 + enonce1 + enonce2 + coinb2)
            coinbase_hash = sha256d(coinbase_bytes)

            # --- 2. Merkle Root ---
            merkle_root = coinbase_hash
            for branch in merkle_branch:
                merkle_root = sha256d(merkle_root + bytes.fromhex(branch))

            # --- 3. Block Header (80 bytes) ---
            # Stratum envía version, prevhash, ntime, nbits ya como little-endian hex,
            # pero prevhash lo envía con los bytes internos revertidos (display format).
            # Según el protocolo Stratum estándar:
            #   version: little-endian hex  -> usamos directamente
            #   prevhash: byte-swapped hex  -> usamos directamente
            #   merkle_root: calculado como bytes -> usamos directamente
            #   ntime: little-endian hex    -> usamos directamente
            #   nbits: little-endian hex    -> usamos directamente
            version_bytes  = bytes.fromhex(version)
            prevhash_bytes = bytes.fromhex(prevhash)
            ntime_bytes    = bytes.fromhex(ntime)
            nbits_bytes    = bytes.fromhex(nbits)

            # El header sin el nonce ocupa 76 bytes
            header_prefix = version_bytes + prevhash_bytes + merkle_root + ntime_bytes + nbits_bytes

            chunk_size = 2000
            start_work = time.time()
            start_nonce = int(start_work * 73856) & 0xFFFFFFFF
            max_chunk_diff = 1e-15

            for i in range(chunk_size):
                nonce = (start_nonce + i) & 0xFFFFFFFF
                header = header_prefix + struct.pack('<I', nonce)
                h = sha256d(header)

                # El hash resultante se interpreta como Little-Endian para comparar con target
                hash_int = int.from_bytes(h, 'little')

                if hash_int > 0:
                    diff = DIFF1 / hash_int
                    if diff > max_chunk_diff:
                        max_chunk_diff = diff

                    if hash_int < target:
                        # ¡SOLUCIÓN REAL ENCONTRADA!
                        submit_queue.put((
                            job_id,
                            enonce2,
                            ntime,
                            format(nonce, '08x')
                        ))
                        update_queue.put(("status", "¡¡SOLUCION ENCONTRADA!!"))

            update_queue.put(("hash", chunk_size))
            update_queue.put(("diff_score", max_chunk_diff))

            # Throttle: dormir proporcionalmente para respetar el % de CPU
            elapsed = time.time() - start_work
            if target_ratio < 1.0 and elapsed > 0:
                sleep_time = elapsed * (1.0 - target_ratio) / target_ratio
                time.sleep(min(sleep_time, 0.5))

        except Exception:
            time.sleep(0.1)

# ============================================================
# --- TUI ---
# ============================================================
def draw_ui(stdscr, update_queue, intensity):
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN,   -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_GREEN,  -1)
    curses.init_pair(4, curses.COLOR_WHITE,  -1)
    curses.init_pair(5, curses.COLOR_RED,    -1)

    start_time   = time.time()
    hashes       = 0
    best_diff    = 1e-15
    cur_diff     = 1e-15
    status       = "Iniciando..."
    block_id     = "-------"
    hr_history   = collections.deque(maxlen=10)
    diff_history = collections.deque(maxlen=5)
    hashes_since = 0
    last_update  = time.time()
    spin_idx     = 0
    spinner      = ["|", "/", "-", "\\"]

    REF_DIFF = 88_000_000_000_000.0  # Dificultad de referencia (≈ mainnet)

    while True:
        # Vaciar la cola de mensajes
        while True:
            try:
                msg_type, val = update_queue.get_nowait()
                if msg_type == "status":
                    status = str(val)
                elif msg_type == "block":
                    block_id = str(val)[:12]
                elif msg_type == "hash":
                    count = int(val)
                    hashes += count
                    hashes_since += count
                elif msg_type == "diff_score":
                    cur_diff = float(val)
                    if cur_diff > best_diff:
                        best_diff = cur_diff
            except Exception:
                break

        now = time.time()
        elapsed = now - last_update
        if elapsed >= 1.0:
            hr = hashes_since / elapsed
            hr_history.append(hr)
            diff_history.append(cur_diff)
            hashes_since = 0
            last_update  = now
            spin_idx     = (spin_idx + 1) % 4

        avg_hr    = sum(hr_history) / max(1, len(hr_history))
        avg_diff  = sum(diff_history) / max(1, len(diff_history))

        # Barra de Suerte
        luck_len  = 24
        log_best  = math.log10(max(1e-15, best_diff))
        log_min   = -15.0
        log_max   = math.log10(REF_DIFF)
        progress  = (log_best - log_min) / (log_max - log_min)
        fill      = int(max(0.0, min(1.0, progress ** 0.5)) * luck_len)

        pulse = 0
        if cur_diff > avg_diff * 1.05:
            pulse = 2
        elif cur_diff < avg_diff * 0.95:
            pulse = -2

        # Dibujar
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        box_w = max(20, min(w - 2, 50))

        # Borde
        try:
            stdscr.attron(curses.color_pair(1))
            stdscr.addstr(0, 0, "╔" + "═" * box_w + "╗")
            for row in range(1, 12):
                if row < h:
                    stdscr.addstr(row, 0, "║" + " " * box_w + "║")
            if 12 < h:
                stdscr.addstr(12, 0, "╚" + "═" * box_w + "╝")
            stdscr.attroff(curses.color_pair(1))
        except curses.error:
            pass

        # Título y salida
        try:
            stdscr.addstr(0, 2, f" NerdMiner [{spinner[spin_idx]}] ", curses.A_BOLD)
            if box_w > 25:
                stdscr.addstr(0, box_w - 9, " [q] Salir ", curses.A_BOLD)

            # Uptime
            uptime = int(now - start_time)
            hh, rem = divmod(uptime, 3600)
            mm, ss  = divmod(rem, 60)
            stdscr.addstr(2, 2, f"Tiempo:   {hh:02d}:{mm:02d}:{ss:02d}")

            # Job ID
            stdscr.addstr(3, 2, f"ID Tarea: {block_id}")

            # Hashrate
            stdscr.addstr(5, 2, "Hashrate: ", curses.color_pair(4))
            if avg_hr >= 1_000_000:
                stdscr.addstr(f"{avg_hr/1_000_000:.2f} MH/s", curses.color_pair(2))
            elif avg_hr >= 1_000:
                stdscr.addstr(f"{avg_hr/1_000:.2f} KH/s", curses.color_pair(2))
            else:
                stdscr.addstr(f"{avg_hr:.1f} H/s", curses.color_pair(2))

            # Barra de Suerte
            stdscr.addstr(7, 2, "Suerte: [")
            if pulse > 0:
                ext = min(pulse, luck_len - fill)
                stdscr.addstr("█" * fill)
                stdscr.addstr("█" * ext, curses.color_pair(3))
                stdscr.addstr("░" * (luck_len - fill - ext))
            elif pulse < 0:
                ret = min(abs(pulse), fill)
                stdscr.addstr("█" * (fill - ret))
                stdscr.addstr("█" * ret, curses.color_pair(5))
                stdscr.addstr("░" * (luck_len - fill))
            else:
                stdscr.addstr("█" * fill)
                stdscr.addstr("░" * (luck_len - fill))
            stdscr.addstr("]")

            # CPU
            stdscr.addstr(8, 2, f"CPU:    {intensity}%")

            # Mejor dificultad
            if best_diff >= 1:
                best_str = f"{best_diff:.2e}"
            else:
                best_str = f"{best_diff:.2e}"
            stdscr.addstr(9, 2, f"Mejor:  {best_str}")

            # Estado
            color = curses.color_pair(3) if "SOLUCION" in status else curses.color_pair(4)
            stdscr.addstr(11, 2, f"Estado: {status[:box_w-9]}", color)

        except curses.error:
            pass

        stdscr.refresh()

        key = stdscr.getch()
        if key == ord('q'):
            break

        time.sleep(0.1)

# ============================================================
# --- Main ---
# ============================================================
def main():
    # Leer config.txt
    wallet    = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"  # Dirección de prueba (¡cámbiala!)
    intensity = 50

    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line:
                        k, v = line.split("=", 1)  # maxsplit=1 por si la wallet tiene '='
                        k = k.strip()
                        v = v.strip()
                        if k == "wallet" and v:
                            wallet = v
                        elif k == "intensity":
                            try:
                                intensity = max(1, min(100, int(v)))
                            except ValueError:
                                pass
    except Exception:
        pass

    update_queue = multiprocessing.Queue()
    job_queue    = multiprocessing.Queue()
    submit_queue = multiprocessing.Queue()

    # Lanzar un worker por núcleo de CPU
    workers = []
    for _ in range(multiprocessing.cpu_count()):
        p = multiprocessing.Process(
            target=hash_worker,
            args=(job_queue, update_queue, submit_queue, intensity),
            daemon=True
        )
        p.start()
        workers.append(p)

    # Lanzar el cliente Stratum en un thread (comparte el proceso)
    t = threading.Thread(
        target=stratum_client,
        args=(wallet, update_queue, job_queue, submit_queue),
        daemon=True
    )
    t.start()

    try:
        curses.wrapper(draw_ui, update_queue, intensity)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Error en la interfaz: {e}")
    finally:
        for p in workers:
            try: p.terminate()
            except Exception: pass

if __name__ == "__main__":
    main()
