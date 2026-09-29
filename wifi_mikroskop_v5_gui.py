import socket
import cv2
import numpy as np
import time
import threading
import signal
import csv
import subprocess
import re
import json
import multiprocessing as mp
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
from datetime import datetime


# ============================================================
# CONFIG
# ============================================================

MIC_IP = "192.168.29.1"

PORT_CMD = 20000
PORT_RTV = 10900

# UDP port, z ktorého podľa logu prichádza video
MIC_VIDEO_PORT = 10850

# UDP receive buffer
SOCKET_BUFFER_SIZE = 4 * 1024 * 1024

# Recording
RECORDING_FPS = 20.0

# Statistics
STATS_INTERVAL = 1.0

# WiFi monitoring
WIFI_CHECK_INTERVAL = 2.0
WIFI_RECONNECT_INTERVAL = 15.0
WIFI_RECONNECT_ATTEMPTS = 3

# Stream watchdog
STREAM_SLOW_MS = 500.0
STREAM_LOST_MS = 3000.0

# Po obnovení WiFi chvíľu počkáme,
# kým pošleme inicializáciu mikroskopu.
WIFI_RESTORE_SETTLE_TIME = 1.5

# Koľko sekúnd musí byť WiFi connected,
# aby sa považovala za stabilne obnovenú.
WIFI_STABLE_TIME = 2.0

# Detailný packet log
DETAILED_PACKET_LOG = False

# WiFi SSID, ktorý očakávame.
EXPECTED_WIFI_SSID = "Max-See_dc7a"

# Ak je None, netsh použije aktuálne/default rozhranie.
WIFI_INTERFACE = None

# Logs
TEXT_LOG = "microscope_v5.log"
CSV_LOG = "microscope_v5_stats.csv"


# ============================================================
# ORIGINAL MICROSCOPE COMMANDS
# ============================================================

CMD_INIT_1 = b"\x4A\x48\x43\x4D\x44\xD0\x01"
CMD_INIT_2 = b"\x4A\x48\x43\x4D\x44\x20\x00\x00\x00\x00\x00"
CMD_STOP = b"\x4A\x48\x43\x4D\x44\xD0\x02"


# ============================================================
# SHARED STATE
# ============================================================

class StreamState:

    def __init__(self):

        self.lock = threading.RLock()

        # ----------------------------------------------------
        # Latest VALID decoded image
        # ----------------------------------------------------

        self.latest_frame = None
        self.latest_frame_id = -1
        self.latest_frame_time = 0.0

        # ----------------------------------------------------
        # Frame / display
        # ----------------------------------------------------

        self.received_frame_id = -1
        self.displayed_frames = 0
        self.replaced_frames = 0

        # ----------------------------------------------------
        # Packet statistics
        # ----------------------------------------------------

        self.packet_count = 0
        self.total_bytes = 0

        self.complete_frames = 0
        self.incomplete_frames = 0

        # NEW V5:
        # Frame assembly obsahoval missing/duplicate/OOO fragment.
        self.corrupt_frames = 0

        self.lost_fragments = 0
        self.duplicates = 0
        self.out_of_order = 0
        self.frame_gaps = 0

        self.jpeg_ok = 0
        self.jpeg_failed = 0

        # ----------------------------------------------------
        # Current second counters
        # ----------------------------------------------------

        self.sec_packets = 0
        self.sec_bytes = 0
        self.sec_frames = 0

        # ----------------------------------------------------
        # Frame assembly
        # ----------------------------------------------------

        self.current_frame_id = None
        self.expected_fragment = None
        self.frame_corrupt = False
        self.frame_packet_count = 0

        # ----------------------------------------------------
        # Last completed VALID source frame
        # ----------------------------------------------------

        self.last_completed_frame_id = -1

        # ----------------------------------------------------
        # Runtime
        # ----------------------------------------------------

        self.running = True
        self.start_time = time.time()

        # ----------------------------------------------------
        # WiFi
        # ----------------------------------------------------

        self.wifi_connected = False
        self.wifi_ssid = ""
        self.wifi_profile = ""
        self.wifi_interface = ""
        self.wifi_category = "Unknown"
        self.wifi_ipv4 = ""
        self.wifi_signal = ""

        self.wifi_last_change = time.time()

        self.wifi_reconnect_in_progress = False
        self.wifi_reconnect_attempt = 0

        self.wifi_restore_pending = False
        self.wifi_restore_time = 0.0

        # ----------------------------------------------------
        # Stream state
        # ----------------------------------------------------

        self.stream_status = "WAITING"

        # ----------------------------------------------------
        # Recovery
        # ----------------------------------------------------

        self.microscope_reinit_count = 0

        # ----------------------------------------------------
        # Logging
        # ----------------------------------------------------

        self.last_event = ""


# ============================================================
# GLOBAL
# ============================================================

STATE = None

# ============================================================
# GUI IPC
# ============================================================
# The UDP receiver never calls the GUI directly.
# Frames/statistics are exported through maxsize=1 queues.
# If the GUI is slower than the stream, an older GUI frame is
# discarded instead of blocking the UDP receiver.
GUI_FRAME_QUEUE = None
GUI_STATS_QUEUE = None
GUI_COMMAND_QUEUE = None


# ============================================================
# SIGNAL HANDLER
# ============================================================

def stop_handler(signum, frame):

    global STATE

    print("\n[!] Ukončovanie programu...")

    if STATE is not None:
        STATE.running = False


# ============================================================
# EVENT LOGGING
# ============================================================

def event_log(state, message, log_file=None):

    timestamp = datetime.now().isoformat(
        timespec="milliseconds"
    )

    line = f"{timestamp} {message}"

    print(line)

    with state.lock:
        state.last_event = message

    if log_file is not None:

        try:
            log_file.write(line + "\n")
            log_file.flush()
        except Exception:
            pass


# ============================================================
# COMMAND SOCKET SEND
# ============================================================

def send_command(sock_cmd, command):

    try:

        sock_cmd.sendto(
            command,
            (MIC_IP, PORT_CMD)
        )

        return True

    except Exception as e:

        print(
            f"[!] Command send error: {e}"
        )

        return False


# ============================================================
# MICROSCOPE INITIALIZATION
# ============================================================

def initialize_microscope(sock_cmd, state=None, log_file=None):

    if state is not None:
        event_log(
            state,
            "[EVENT] MICROSCOPE INIT",
            log_file
        )
    else:
        print("[*] Inicializujem mikroskop...")

    try:

        # PRESNE pôvodná inicializácia

        sock_cmd.sendto(
            CMD_INIT_1,
            (MIC_IP, PORT_CMD)
        )

        time.sleep(0.05)

        sock_cmd.sendto(
            CMD_INIT_2,
            (MIC_IP, PORT_CMD)
        )

        time.sleep(0.05)

        sock_cmd.sendto(
            CMD_INIT_1,
            (MIC_IP, PORT_CMD)
        )

        if state is not None:

            with state.lock:
                state.microscope_reinit_count += 1

        print("[+] Inicializácia odoslaná")

        return True

    except Exception as e:

        print(
            f"[!] Inicializácia zlyhala: {e}"
        )

        return False


# ============================================================
# FRAME PROCESSING
# ============================================================

def process_frame(
    state,
    frame_id,
    frame_buffer,
    frame_corrupt
):

    if not frame_buffer:
        return

    # ========================================================
    # V5 STRICT FRAME VALIDATION
    #
    # Ak chýbal čo i len jeden fragment alebo prišiel
    # duplicate/out-of-order fragment, celý frame zahodíme.
    #
    # Taký frame sa NESMIE dostať do latest_frame.
    # ========================================================

    if frame_corrupt:

        with state.lock:

            state.corrupt_frames += 1
            state.incomplete_frames += 1

        return

    # --------------------------------------------------------
    # JPEG SOI
    # --------------------------------------------------------

    sof = frame_buffer.find(b"\xFF\xD8")

    # --------------------------------------------------------
    # JPEG EOI
    # --------------------------------------------------------

    eof = frame_buffer.rfind(b"\xFF\xD9")

    if sof == -1 or eof == -1 or eof <= sof:

        with state.lock:

            state.incomplete_frames += 1

        return

    jpg_data = frame_buffer[sof:eof + 2]

    img_np = np.frombuffer(
        jpg_data,
        dtype=np.uint8
    )

    # --------------------------------------------------------
    # OpenCV decode
    # --------------------------------------------------------

    try:

        img = cv2.imdecode(
            img_np,
            cv2.IMREAD_COLOR
        )

    except Exception:

        img = None

    if img is None:

        with state.lock:

            state.jpeg_failed += 1

        return

    # --------------------------------------------------------
    # VALID FRAME
    # --------------------------------------------------------

    now = time.time()

    with state.lock:

        state.jpeg_ok += 1
        state.complete_frames += 1

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Iba VALID frame môže nahradiť latest_frame.
        # ----------------------------------------------------

        if state.latest_frame is not None:
            state.replaced_frames += 1

        state.latest_frame = img
        state.latest_frame_id = frame_id
        state.received_frame_id = frame_id
        state.latest_frame_time = now

        state.sec_frames += 1

        state.last_completed_frame_id = frame_id

    # --------------------------------------------------------
    # GUI IPC: latest frame only, NEVER block receiver.
    # The GUI process can therefore lag without backing up UDP.
    # --------------------------------------------------------
    publish_gui_frame(frame_id, now, img)



# ============================================================
# GUI IPC HELPERS
# ============================================================

def _queue_put_latest(q, item):
    """Put only the newest item into a maxsize=1 queue."""
    if q is None:
        return

    try:
        q.put_nowait(item)
        return
    except queue.Full:
        pass
    except Exception:
        return

    # Remove stale GUI item, then try once more.
    try:
        q.get_nowait()
    except Exception:
        pass

    try:
        q.put_nowait(item)
    except Exception:
        # Never allow GUI IPC to block the receiver.
        pass


def publish_gui_frame(frame_id, frame_time, frame):
    if GUI_FRAME_QUEUE is None:
        return

    _queue_put_latest(
        GUI_FRAME_QUEUE,
        (
            int(frame_id),
            float(frame_time),
            frame
        )
    )


def gui_stats_thread(state):
    """Publishes lightweight state snapshots to the GUI process."""
    last_t = time.time()
    last_packets = 0
    last_bytes = 0
    last_frames = 0

    while state.running:
        time.sleep(0.20)

        now = time.time()
        dt = max(now - last_t, 0.001)

        with state.lock:
            packet_count = state.packet_count
            total_bytes = state.total_bytes
            complete_frames = state.complete_frames
            latest_frame_id = state.latest_frame_id
            latest_frame_time = state.latest_frame_time

            snapshot = {
                "timestamp": now,
                "mic_ip": MIC_IP,
                "wifi_connected": state.wifi_connected,
                "wifi_ssid": state.wifi_ssid,
                "wifi_signal": state.wifi_signal,
                "wifi_category": state.wifi_category,
                "wifi_ipv4": state.wifi_ipv4,
                "wifi_interface": state.wifi_interface,
                "stream_status": state.stream_status,
                "packet_count": packet_count,
                "total_bytes": total_bytes,
                "complete_frames": complete_frames,
                "latest_frame_id": latest_frame_id,
                "replaced_frames": state.replaced_frames,
                "corrupt_frames": state.corrupt_frames,
                "lost_fragments": state.lost_fragments,
                "duplicates": state.duplicates,
                "out_of_order": state.out_of_order,
                "frame_gaps": state.frame_gaps,
                "jpeg_ok": state.jpeg_ok,
                "jpeg_failed": state.jpeg_failed,
                "reinit_count": state.microscope_reinit_count,
                "last_event": state.last_event,
            }

        snapshot["packet_rate"] = (
            packet_count - last_packets
        ) / dt

        snapshot["mbit_s"] = (
            (total_bytes - last_bytes) * 8.0
            / dt
            / 1_000_000.0
        )

        snapshot["fps"] = (
            complete_frames - last_frames
        ) / dt

        if latest_frame_time > 0:
            snapshot["latency_ms"] = max(
                0.0,
                (now - latest_frame_time) * 1000.0
            )
        else:
            snapshot["latency_ms"] = 0.0

        denom = packet_count + state.lost_fragments
        snapshot["loss_pct"] = (
            state.lost_fragments / denom * 100.0
            if denom > 0
            else 0.0
        )

        _queue_put_latest(GUI_STATS_QUEUE, snapshot)

        last_t = now
        last_packets = packet_count
        last_bytes = total_bytes
        last_frames = complete_frames


def receiver_command_thread(state, sock_cmd, log_file):
    """
    Receives commands from GUI.

    IMPORTANT:
    This thread performs GUI commands. The UDP receiver thread is
    completely independent and never waits for this queue.
    """
    global MIC_IP

    while state.running:
        try:
            command = GUI_COMMAND_QUEUE.get(timeout=0.10)
        except queue.Empty:
            continue
        except Exception:
            break

        try:
            action = command.get("action")

            if action == "set_ip":
                new_ip = str(command.get("ip", "")).strip()

                # Basic IPv4 validation without doing DNS/network work.
                try:
                    socket.inet_aton(new_ip)
                    if new_ip.count(".") != 3:
                        raise ValueError
                except Exception:
                    event_log(
                        state,
                        f"[GUI] Neplatná IP adresa: {new_ip}",
                        log_file
                    )
                    continue

                old_ip = MIC_IP
                MIC_IP = new_ip

                event_log(
                    state,
                    f"[GUI] Microscope IP: {old_ip} -> {MIC_IP}",
                    log_file
                )

            elif action == "init":
                initialize_microscope(
                    sock_cmd,
                    state,
                    log_file
                )

            elif action == "connect":
                new_ip = str(command.get("ip", "")).strip()

                try:
                    socket.inet_aton(new_ip)
                    if new_ip.count(".") != 3:
                        raise ValueError
                except Exception:
                    event_log(
                        state,
                        f"[GUI] Neplatná IP adresa: {new_ip}",
                        log_file
                    )
                    continue

                MIC_IP = new_ip

                event_log(
                    state,
                    f"[GUI] Connect -> {MIC_IP}",
                    log_file
                )

                initialize_microscope(
                    sock_cmd,
                    state,
                    log_file
                )

            elif action == "wifi_reconnect":
                threading.Thread(
                    target=wifi_reconnect,
                    args=(state, log_file),
                    daemon=True,
                    name="GUI-WiFiReconnect"
                ).start()

            elif action == "detailed":
                global DETAILED_PACKET_LOG
                DETAILED_PACKET_LOG = bool(
                    command.get("enabled", False)
                )

                event_log(
                    state,
                    "[GUI] Detailed packet logging: "
                    f"{DETAILED_PACKET_LOG}",
                    log_file
                )

            elif action == "stop":
                state.running = False

        except Exception as exc:
            event_log(
                state,
                f"[GUI] Command error: {exc}",
                log_file
            )


# ============================================================
# UDP RECEIVER THREAD
# ============================================================

def receiver_thread(
    state,
    sock_rtv,
    log_file
):

    print("[*] UDP receiver thread spustený")

    current_frame_id = None
    frame_buffer = bytearray()

    expected_fragment = None
    frame_corrupt = False

    packet_number = 0

    while state.running:

        try:

            data, addr = sock_rtv.recvfrom(65535)

        except socket.timeout:

            continue

        except OSError:

            if not state.running:
                break

            continue

        except Exception as e:

            print(
                f"[!] UDP receive error: {e}"
            )

            continue

        if not state.running:
            break

        # ----------------------------------------------------
        # Filter
        # ----------------------------------------------------

        if addr[0] != MIC_IP:
            continue

        if len(data) < 8:
            continue

        # ----------------------------------------------------
        # Header
        # ----------------------------------------------------

        frame_id = (
            data[0]
            | (data[1] << 8)
        )

        fragment_id = data[3]

        payload = data[8:]

        packet_number += 1

        # ----------------------------------------------------
        # Statistics
        # ----------------------------------------------------

        with state.lock:

            state.packet_count += 1
            state.total_bytes += len(data)

            state.sec_packets += 1
            state.sec_bytes += len(data)

        # ----------------------------------------------------
        # Detailed packet logging
        # ----------------------------------------------------

        if (
            DETAILED_PACKET_LOG
            and log_file is not None
        ):

            timestamp = datetime.now().isoformat(
                timespec="milliseconds"
            )

            header_hex = data[:8].hex(" ")

            line = (
                f"{timestamp}"
                f" | packet={packet_number:08d}"
                f" | from={addr[0]}:{addr[1]}"
                f" | size={len(data):5d}"
                f" | frame={frame_id:5d}"
                f" | fragment={fragment_id:3d}"
                f" | header={header_hex}\n"
            )

            try:
                log_file.write(line)
            except Exception:
                pass

        # ====================================================
        # FIRST FRAME
        # ====================================================

        if current_frame_id is None:

            current_frame_id = frame_id
            expected_fragment = fragment_id

            frame_buffer = bytearray(payload)

            frame_corrupt = False

            continue

        # ====================================================
        # NEW FRAME
        # ====================================================

        if frame_id != current_frame_id:

            # ------------------------------------------------
            # Finalize previous frame
            # ------------------------------------------------

            process_frame(
                state,
                current_frame_id,
                frame_buffer,
                frame_corrupt
            )

            # ------------------------------------------------
            # Frame gap detection
            # ------------------------------------------------

            expected_frame = (
                (current_frame_id + 1)
                & 0xFFFF
            )

            if frame_id != expected_frame:

                diff = (
                    frame_id - expected_frame
                ) & 0xFFFF

                if diff < 1000:

                    with state.lock:
                        state.frame_gaps += diff

            # ------------------------------------------------
            # Start NEW clean frame
            # ------------------------------------------------

            current_frame_id = frame_id

            frame_buffer = bytearray(payload)

            # NEW frame = nový čistý assembly
            frame_corrupt = False
            
            expected_fragment = (
                fragment_id + 1
            ) & 0xFF



            continue

        # ====================================================
        # SAME FRAME
        # ====================================================

        # ----------------------------------------------------
        # First fragment
        # ----------------------------------------------------

        if expected_fragment is None:

            expected_fragment = fragment_id

            frame_buffer.extend(payload)

            continue

        # ----------------------------------------------------
        # Normal sequence
        # ----------------------------------------------------

        if fragment_id == expected_fragment:

            frame_buffer.extend(payload)

        # ----------------------------------------------------
        # Fragment arrived AFTER expected fragment
        #
        # => one or more fragments were lost.
        #
        # IMPORTANT:
        # Entire frame becomes CORRUPT.
        # ----------------------------------------------------

        elif (
            (
                fragment_id
                - expected_fragment
            ) & 0xFF
        ) < 128:

            missing = (
                fragment_id
                - expected_fragment
            ) & 0xFF

            if missing <= 0:
                missing = 1

            with state.lock:
                state.lost_fragments += missing

            frame_corrupt = True

            # Payload už nepridávame do assembly,
            # pretože frame je aj tak invalidný.
            #
            # Zároveň ale musíme sledovať sequence,
            # aby sme vedeli pokračovať až po nový frame.

        # ----------------------------------------------------
        # Duplicate / out-of-order
        # ----------------------------------------------------

        else:

            previous_fragment = (
                expected_fragment - 1
            ) & 0xFF

            if fragment_id == previous_fragment:

                with state.lock:
                    state.duplicates += 1

            else:

                with state.lock:
                    state.out_of_order += 1

            # Aj toto robí celý frame CORRUPT.
            frame_corrupt = True

        # ----------------------------------------------------
        # Advance expected fragment
        # ----------------------------------------------------

        expected_fragment = (
            fragment_id + 1
        ) & 0xFF

    # ========================================================
    # FINAL FRAME
    # ========================================================

    if (
        current_frame_id is not None
        and frame_buffer
    ):

        process_frame(
            state,
            current_frame_id,
            frame_buffer,
            frame_corrupt
        )

    print("[*] UDP receiver thread ukončený")


# ============================================================
# KEEP ALIVE THREAD
# ============================================================

def keep_alive_thread(
    state,
    sock_cmd
):

    last_ping = 0.0

    while state.running:

        now = time.time()

        with state.lock:
            wifi_connected = state.wifi_connected

        # ----------------------------------------------------
        # Keep-alive iba ak máme WiFi
        # ----------------------------------------------------

        if (
            wifi_connected
            and now - last_ping >= 1.0
        ):

            try:

                sock_cmd.sendto(
                    CMD_INIT_1,
                    (MIC_IP, PORT_CMD)
                )

            except Exception as e:

                print(
                    f"[!] Keep-alive error: {e}"
                )

            last_ping = now

        time.sleep(0.05)


# ============================================================
# RUN COMMAND
# ============================================================

def run_command(
    command,
    timeout=5.0
):

    try:

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if hasattr(
                    subprocess,
                    "CREATE_NO_WINDOW"
                )
                else 0
            )
        )

        return (
            result.returncode,
            result.stdout,
            result.stderr
        )

    except Exception as e:

        return (
            -1,
            "",
            str(e)
        )


# ============================================================
# WIFI INFORMATION
# ============================================================

def get_wifi_info():

    """
    Používa Windows netsh + PowerShell.

    Vracia:

        connected
        ssid
        profile
        interface
        category
        ipv4
    """

    result = {
        "connected": False,
        "ssid": "",
        "profile": "",
        "interface": "",
        "category": "Unknown",
        "ipv4": "",
        "signal": ""
    }

    # ========================================================
    # NETSH WLAN INTERFACE
    # ========================================================

    rc, stdout, stderr = run_command(
        [
            "netsh",
            "wlan",
            "show",
            "interfaces"
        ],
        timeout=5
    )

    if rc != 0:
        return result

    text = stdout

    # --------------------------------------------------------
    # State
    # --------------------------------------------------------

    state_match = re.search(
        r"^\s*State\s*:\s*(.+)$",
        text,
        re.MULTILINE | re.IGNORECASE
    )

    if state_match:

        connection_state = (
            state_match.group(1)
            .strip()
            .lower()
        )

        # English Windows
        if connection_state == "connected":
            result["connected"] = True

        # Slovak/Czech fallback
        elif connection_state in (
            "pripojené",
            "připojeno",
            "pripojeno"
        ):
            result["connected"] = True

    # --------------------------------------------------------
    # SSID
    # --------------------------------------------------------

    ssid_match = re.search(
        r"^\s*SSID\s*:\s*(.+)$",
        text,
        re.MULTILINE | re.IGNORECASE
    )

    if ssid_match:

        result["ssid"] = (
            ssid_match.group(1).strip()
        )


    # --------------------------------------------------------
    # Signal
    # --------------------------------------------------------
    signal_match = re.search(
        r"^\s*Signal\s*:\s*(.+)$",
        text,
        re.MULTILINE | re.IGNORECASE
    )

    if signal_match:
        result["signal"] = signal_match.group(1).strip()

    # --------------------------------------------------------
    # Profile
    # --------------------------------------------------------

    profile_match = re.search(
        r"^\s*Profile\s*:\s*(.+)$",
        text,
        re.MULTILINE | re.IGNORECASE
    )

    if profile_match:

        result["profile"] = (
            profile_match.group(1).strip()
        )

    # --------------------------------------------------------
    # Interface
    # --------------------------------------------------------

    interface_match = re.search(
        r"^\s*(?:Name|Názov)\s*:\s*(.+)$",
        text,
        re.MULTILINE | re.IGNORECASE
    )

    if interface_match:

        result["interface"] = (
            interface_match.group(1).strip()
        )

    # ========================================================
    # POWER SHELL - NETWORK CATEGORY / IPV4
    # ========================================================

    ps_command = (
        "Get-NetConnectionProfile | "
        "Select-Object InterfaceAlias,Name,"
        "NetworkCategory,IPv4Connectivity | "
        "ConvertTo-Json -Compress"
    )

    rc, ps_stdout, ps_stderr = run_command(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            ps_command
        ],
        timeout=5
    )

    if rc == 0 and ps_stdout.strip():

        try:

            profiles = json.loads(
                ps_stdout.strip()
            )

            if isinstance(profiles, dict):
                profiles = [profiles]

            # Preferuj profil z rovnakým SSID
            selected = None

            for profile in profiles:

                name = str(
                    profile.get("Name", "")
                )

                if (
                    result["ssid"]
                    and name == result["ssid"]
                ):

                    selected = profile
                    break

            if selected is None and profiles:

                selected = profiles[0]

            if selected:

                result["category"] = str(
                    selected.get(
                        "NetworkCategory",
                        "Unknown"
                    )
                )

                if not result["interface"]:

                    result["interface"] = str(
                        selected.get(
                            "InterfaceAlias",
                            ""
                        )
                    )

        except Exception:
            pass

    # ========================================================
    # IPV4 ADDRESS
    # ========================================================

    if result["interface"]:

        ps_ipv4 = (
            "Get-NetIPAddress "
            "-AddressFamily IPv4 "
            f"-InterfaceAlias "
            "'{result['interface'].replace(chr(39), chr(39)+chr(39))}' "
            "| Select-Object -ExpandProperty IPAddress"
        )

        rc, ip_stdout, ip_stderr = run_command(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_ipv4
            ],
            timeout=5
        )

        if rc == 0:

            ips = [
                line.strip()
                for line in ip_stdout.splitlines()
                if line.strip()
            ]

            if ips:
                result["ipv4"] = ips[0]

    return result


# ============================================================
# WIFI STATUS UPDATE
# ============================================================

def update_wifi_state(
    state,
    info,
    log_file
):

    now = time.time()

    with state.lock:

        old_connected = state.wifi_connected
        old_ssid = state.wifi_ssid

        state.wifi_connected = info["connected"]
        state.wifi_ssid = info["ssid"]
        state.wifi_profile = info["profile"]
        state.wifi_interface = info["interface"]
        state.wifi_category = info["category"]
        state.wifi_ipv4 = info["ipv4"]

    # ========================================================
    # CONNECTED -> LOST
    # ========================================================

    if old_connected and not info["connected"]:

        event_log(
            state,
            "[EVENT] WIFI DISCONNECTED",
            log_file
        )

        with state.lock:

            state.wifi_last_change = now
            state.wifi_reconnect_in_progress = False
            state.wifi_reconnect_attempt = 0

        return

    # ========================================================
    # LOST -> CONNECTED
    # ========================================================

    if (
        not old_connected
        and info["connected"]
    ):

        event_log(
            state,
            "[EVENT] WIFI CONNECTED "
            f"ssid={info['ssid']}",
            log_file
        )

        with state.lock:

            state.wifi_last_change = now
            state.wifi_restore_pending = True
            state.wifi_restore_time = now

        return

    # ========================================================
    # SSID CHANGED
    # ========================================================

    if (
        old_connected
        and info["connected"]
        and old_ssid != info["ssid"]
    ):

        event_log(
            state,
            "[EVENT] WIFI SSID CHANGED "
            f"{old_ssid} -> {info['ssid']}",
            log_file
        )

        with state.lock:

            state.wifi_restore_pending = True
            state.wifi_restore_time = now


# ============================================================
# WIFI RECONNECT
# ============================================================

def wifi_reconnect(
    state,
    log_file
):

    with state.lock:

        if state.wifi_reconnect_in_progress:
            return

        state.wifi_reconnect_in_progress = True

    event_log(
        state,
        "[EVENT] WIFI LOST - spustam reconnect",
        log_file
    )

    success = False

    for attempt in range(
        1,
        WIFI_RECONNECT_ATTEMPTS + 1
    ):

        if not state.running:
            break

        # ----------------------------------------------------
        # Get current profile
        # ----------------------------------------------------

        info = get_wifi_info()

        profile = info["profile"]

        if not profile:

            profile = EXPECTED_WIFI_SSID

        interface = (
            WIFI_INTERFACE
            or info["interface"]
        )

        event_log(
            state,
            "[EVENT] WIFI RECONNECT "
            f"attempt={attempt} "
            f"profile={profile}",
            log_file
        )

        # ----------------------------------------------------
        # netsh connect
        # ----------------------------------------------------

        command = [
            "netsh",
            "wlan",
            "connect",
            f"name={profile}"
        ]

        if interface:

            command.append(
                f"interface={interface}"
            )

        rc, stdout, stderr = run_command(
            command,
            timeout=10
        )

        if stdout.strip():

            event_log(
                state,
                "[WIFI] netsh: "
                + stdout.strip().replace(
                    "\n",
                    " | "
                ),
                log_file
            )

        if stderr.strip():

            event_log(
                state,
                "[WIFI] netsh stderr: "
                + stderr.strip().replace(
                    "\n",
                    " | "
                ),
                log_file
            )

        # ----------------------------------------------------
        # Wait for Windows connection state
        # ----------------------------------------------------

        deadline = (
            time.time() + 8.0
        )

        while (
            state.running
            and time.time() < deadline
        ):

            info = get_wifi_info()

            if (
                info["connected"]
                and (
                    not EXPECTED_WIFI_SSID
                    or info["ssid"]
                    == EXPECTED_WIFI_SSID
                )
            ):

                event_log(
                    state,
                    "[EVENT] WIFI CONNECTED "
                    f"ssid={info['ssid']}",
                    log_file
                )

                success = True

                break

            time.sleep(0.5)

        if success:
            break

        time.sleep(
            WIFI_RECONNECT_INTERVAL
        )

    # ========================================================
    # RESULT
    # ========================================================

    with state.lock:

        state.wifi_reconnect_in_progress = False
        state.wifi_reconnect_attempt = 0

    if success:

        with state.lock:

            state.wifi_restore_pending = True
            state.wifi_restore_time = time.time()

        event_log(
            state,
            "[EVENT] WIFI RESTORED",
            log_file
        )

    else:

        event_log(
            state,
            "[EVENT] WIFI RECONNECT FAILED",
            log_file
        )


# ============================================================
# WIFI MONITOR THREAD
# ============================================================

def wifi_monitor_thread(
    state,
    sock_cmd,
    log_file
):

    print("[*] WiFi monitor thread spustený")

    last_check = 0.0
    last_public_warning = 0.0
    last_reconnect = 0.0

    while state.running:

        now = time.time()

        # ----------------------------------------------------
        # Periodic WiFi query
        # ----------------------------------------------------

        if now - last_check >= WIFI_CHECK_INTERVAL:

            last_check = now

            info = get_wifi_info()

            update_wifi_state(
                state,
                info,
                log_file
            )

            # ------------------------------------------------
            # Public network warning
            # ------------------------------------------------

            if (
                info["connected"]
                and info["category"].lower()
                == "public"
            ):

                if (
                    now - last_public_warning
                    >= 30.0
                ):

                    event_log(
                        state,
                        "[WARNING] WIFI NETWORK "
                        "CATEGORY = PUBLIC "
                        f"ssid={info['ssid']} "
                        f"interface={info['interface']}",
                        log_file
                    )

                    last_public_warning = now

            # ------------------------------------------------
            # WiFi LOST
            # ------------------------------------------------

            if not info["connected"]:

                with state.lock:

                    already_running = (
                        state.wifi_reconnect_in_progress
                    )

                if (
                    not already_running
                    and (
                        now - last_reconnect
                        >= WIFI_RECONNECT_INTERVAL
                    )
                ):

                    last_reconnect = now

                    threading.Thread(
                        target=wifi_reconnect,
                        args=(
                            state,
                            log_file
                        ),
                        daemon=True
                    ).start()

            # ------------------------------------------------
            # WiFi RESTORED
            # ------------------------------------------------

            with state.lock:

                restore_pending = (
                    state.wifi_restore_pending
                )

                restore_time = (
                    state.wifi_restore_time
                )

                connected = (
                    state.wifi_connected
                )

            if (
                restore_pending
                and connected
                and (
                    now - restore_time
                    >= WIFI_RESTORE_SETTLE_TIME
                )
            ):

                # ------------------------------------------------
                # Re-read WiFi one more time.
                # ------------------------------------------------

                verify = get_wifi_info()

                if verify["connected"]:

                    event_log(
                        state,
                        "[EVENT] WIFI STABLE - "
                        "reinitializing microscope",
                        log_file
                    )

                    # ------------------------------------------------
                    # Re-init mikroskopu
                    # ------------------------------------------------

                    initialize_microscope(
                        sock_cmd,
                        state,
                        log_file
                    )

                    with state.lock:

                        state.wifi_restore_pending = False

                    event_log(
                        state,
                        "[EVENT] MICROSCOPE "
                        "REINIT AFTER WIFI RESTORE",
                        log_file
                    )

        time.sleep(0.1)

    print("[*] WiFi monitor thread ukončený")


# ============================================================
# STREAM WATCHDOG THREAD
# ============================================================

def stream_watchdog_thread(
    state,
    log_file
):

    last_status = None

    while state.running:

        now = time.time()

        with state.lock:

            connected = state.wifi_connected
            latest_time = state.latest_frame_time

        if not connected:

            status = "WIFI LOST"

        elif latest_time <= 0:

            status = "WAITING"

        else:

            age_ms = (
                now - latest_time
            ) * 1000.0

            if age_ms >= STREAM_LOST_MS:

                status = "STREAM LOST"

            elif age_ms >= STREAM_SLOW_MS:

                status = "STREAM SLOW"

            else:

                status = "OK"

        # ----------------------------------------------------
        # Only log changes
        # ----------------------------------------------------

        if status != last_status:

            event_log(
                state,
                f"[EVENT] STREAM STATUS = {status}",
                log_file
            )

            with state.lock:
                state.stream_status = status

            last_status = status

        time.sleep(0.25)


# ============================================================
# STATISTICS THREAD
# ============================================================

def statistics_thread(
    state,
    log_file,
    csv_writer,
    csv_file
):

    last_time = time.time()

    while state.running:

        time.sleep(STATS_INTERVAL)

        now = time.time()

        elapsed = now - last_time

        if elapsed <= 0:
            continue

        # ----------------------------------------------------
        # Atomic snapshot
        # ----------------------------------------------------

        with state.lock:

            packets = state.sec_packets
            bytes_sec = state.sec_bytes
            frames = state.sec_frames

            state.sec_packets = 0
            state.sec_bytes = 0
            state.sec_frames = 0

            latest_frame_id = (
                state.latest_frame_id
            )

            received_frame_id = (
                state.received_frame_id
            )

            replaced = (
                state.replaced_frames
            )

            complete = (
                state.complete_frames
            )

            incomplete = (
                state.incomplete_frames
            )

            corrupt = (
                state.corrupt_frames
            )

            lost = (
                state.lost_fragments
            )

            duplicates = (
                state.duplicates
            )

            out_order = (
                state.out_of_order
            )

            gaps = (
                state.frame_gaps
            )

            jpeg_ok = (
                state.jpeg_ok
            )

            jpeg_failed = (
                state.jpeg_failed
            )

            latest_frame_time = (
                state.latest_frame_time
            )

            wifi_connected = (
                state.wifi_connected
            )

            wifi_ssid = (
                state.wifi_ssid
            )

            wifi_category = (
                state.wifi_category
            )

            wifi_ipv4 = (
                state.wifi_ipv4
            )

            stream_status = (
                state.stream_status
            )

        # ----------------------------------------------------
        # Metrics
        # ----------------------------------------------------

        packet_rate = (
            packets / elapsed
        )

        mbps = (
            bytes_sec * 8
        ) / elapsed / 1_000_000

        if latest_frame_time > 0:

            latency_ms = (
                time.time()
                - latest_frame_time
            ) * 1000.0

        else:

            latency_ms = 0.0

        wifi_text = (
            wifi_ssid
            if wifi_connected
            else "DISCONNECTED"
        )

        # ----------------------------------------------------
        # Console
        # ----------------------------------------------------

        print(
            f"[LIVE] "
            f"UDP={packet_rate:6.1f} pkt/s | "
            f"{mbps:5.2f} Mbit/s | "
            f"frames={frames:3d}/s | "
            f"frame={latest_frame_id:5d} | "
            f"age={latency_ms:7.1f} ms | "
            f"replaced={replaced:5d} | "
            f"{stream_status}"
        )

        # ----------------------------------------------------
        # CSV
        # ----------------------------------------------------

        if csv_writer is not None:

            try:

                csv_writer.writerow([
                    datetime.now().isoformat(
                        timespec="milliseconds"
                    ),
                    packet_rate,
                    mbps,
                    frames,
                    latest_frame_id,
                    latency_ms,
                    complete,
                    incomplete,
                    corrupt,
                    lost,
                    duplicates,
                    out_order,
                    gaps,
                    jpeg_ok,
                    jpeg_failed,
                    replaced,
                    wifi_connected,
                    wifi_ssid,
                    wifi_category,
                    wifi_ipv4,
                    stream_status
                ])

                csv_file.flush()

            except Exception:
                pass

        # ----------------------------------------------------
        # Text log
        # ----------------------------------------------------

        if log_file is not None:

            try:

                log_file.write(
                    f"{datetime.now().isoformat(timespec='milliseconds')}"
                    f" | UDP={packet_rate:.1f}"
                    f" | Mbit={mbps:.3f}"
                    f" | frames={frames}"
                    f" | frame={latest_frame_id}"
                    f" | age_ms={latency_ms:.1f}"
                    f" | complete={complete}"
                    f" | incomplete={incomplete}"
                    f" | corrupt={corrupt}"
                    f" | lost={lost}"
                    f" | dup={duplicates}"
                    f" | ooo={out_order}"
                    f" | gaps={gaps}"
                    f" | jpeg_ok={jpeg_ok}"
                    f" | jpeg_failed={jpeg_failed}"
                    f" | replaced={replaced}"
                    f" | wifi={wifi_text}"
                    f" | category={wifi_category}"
                    f" | ipv4={wifi_ipv4}"
                    f" | stream={stream_status}\n"
                )

                log_file.flush()

            except Exception:
                pass

        last_time = now


# ============================================================
# MAIN
# ============================================================


# ============================================================
# RECEIVER PROCESS
# ============================================================

def receiver_process_main(
    frame_queue,
    stats_queue,
    command_queue
):
    """
    Network/decoder process.

    The GUI is a different process. The UDP receiver therefore
    cannot be stalled by Tkinter painting, resizing, recording,
    or a slow GUI refresh.
    """
    global STATE
    global DETAILED_PACKET_LOG
    global GUI_FRAME_QUEUE
    global GUI_STATS_QUEUE
    global GUI_COMMAND_QUEUE

    GUI_FRAME_QUEUE = frame_queue
    GUI_STATS_QUEUE = stats_queue
    GUI_COMMAND_QUEUE = command_queue

    STATE = StreamState()

    signal.signal(
        signal.SIGINT,
        stop_handler
    )

    log_file = open(
        TEXT_LOG,
        "w",
        encoding="utf-8"
    )

    csv_file = open(
        CSV_LOG,
        "w",
        newline="",
        encoding="utf-8"
    )

    csv_writer = csv.writer(csv_file)

    csv_writer.writerow([
        "timestamp",
        "packet_rate",
        "mbit_s",
        "frames_s",
        "latest_frame_id",
        "latency_ms",
        "complete_frames",
        "incomplete_frames",
        "corrupt_frames",
        "lost_fragments",
        "duplicates",
        "out_of_order",
        "frame_gaps",
        "jpeg_ok",
        "jpeg_failed",
        "replaced_frames",
        "wifi_connected",
        "wifi_ssid",
        "wifi_category",
        "wifi_ipv4",
        "stream_status"
    ])

    sock_cmd = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )

    sock_rtv = socket.socket(
        socket.AF_INET,
        socket.SOCK_DGRAM
    )

    sock_cmd.settimeout(1.0)

    try:
        sock_rtv.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_RCVBUF,
            SOCKET_BUFFER_SIZE
        )

        actual_buffer = sock_rtv.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_RCVBUF
        )

        print(
            f"[*] UDP receive buffer: "
            f"{actual_buffer / 1024 / 1024:.2f} MB"
        )

    except Exception as e:
        print(
            f"[!] Nepodarilo sa nastaviť "
            f"SO_RCVBUF: {e}"
        )

    sock_rtv.setsockopt(
        socket.SOL_SOCKET,
        socket.SO_REUSEADDR,
        1
    )

    sock_rtv.bind(
        ("", PORT_RTV)
    )

    sock_rtv.settimeout(0.1)

    wifi_info = get_wifi_info()

    with STATE.lock:
        STATE.wifi_connected = wifi_info["connected"]
        STATE.wifi_ssid = wifi_info["ssid"]
        STATE.wifi_profile = wifi_info["profile"]
        STATE.wifi_interface = wifi_info["interface"]
        STATE.wifi_category = wifi_info["category"]
        STATE.wifi_ipv4 = wifi_info["ipv4"]
        STATE.wifi_signal = wifi_info.get("signal", "")

    print()
    print("==============================================")
    print(" WiFi Microscope V5 - NETWORK PROCESS")
    print("==============================================")
    print(f"Microscope:       {MIC_IP}")
    print(f"Command port:     {PORT_CMD}")
    print(f"Video port:       {PORT_RTV}")
    print(f"Expected WiFi:    {EXPECTED_WIFI_SSID}")
    print(f"WiFi connected:   {wifi_info['connected']}")
    print(f"WiFi SSID:        {wifi_info['ssid']}")
    print(f"WiFi signal:      {wifi_info.get('signal', '')}")
    print(f"WiFi IPv4:        {wifi_info['ipv4']}")
    print("==============================================")
    print()

    if (
        wifi_info["connected"]
        and wifi_info["category"].lower() == "public"
    ):
        event_log(
            STATE,
            "[WARNING] WiFi je nastavená ako PUBLIC. "
            "V5 automaticky nemení firewall/network profile.",
            log_file
        )

    if wifi_info["connected"]:
        initialize_microscope(
            sock_cmd,
            STATE,
            log_file
        )
    else:
        event_log(
            STATE,
            "[WARNING] WiFi nie je pripojená - "
            "mikroskop zatiaľ neinicializujem.",
            log_file
        )

    receiver = threading.Thread(
        target=receiver_thread,
        args=(STATE, sock_rtv, log_file),
        daemon=True,
        name="UDPReceiver"
    )

    keep_alive = threading.Thread(
        target=keep_alive_thread,
        args=(STATE, sock_cmd),
        daemon=True,
        name="KeepAlive"
    )

    stats = threading.Thread(
        target=statistics_thread,
        args=(STATE, log_file, csv_writer, csv_file),
        daemon=True,
        name="Statistics"
    )

    wifi_monitor = threading.Thread(
        target=wifi_monitor_thread,
        args=(STATE, sock_cmd, log_file),
        daemon=True,
        name="WiFiMonitor"
    )

    stream_watchdog = threading.Thread(
        target=stream_watchdog_thread,
        args=(STATE, log_file),
        daemon=True,
        name="StreamWatchdog"
    )

    gui_stats = threading.Thread(
        target=gui_stats_thread,
        args=(STATE,),
        daemon=True,
        name="GUIStats"
    )

    gui_commands = threading.Thread(
        target=receiver_command_thread,
        args=(STATE, sock_cmd, log_file),
        daemon=True,
        name="GUICommands"
    )

    receiver.start()
    keep_alive.start()
    stats.start()
    wifi_monitor.start()
    stream_watchdog.start()
    gui_stats.start()
    gui_commands.start()

    try:
        while STATE.running:
            time.sleep(0.25)

    except KeyboardInterrupt:
        STATE.running = False

    finally:
        STATE.running = False
        time.sleep(0.2)

        try:
            sock_cmd.sendto(
                CMD_STOP,
                (MIC_IP, PORT_CMD)
            )
        except Exception:
            pass

        try:
            sock_rtv.close()
        except Exception:
            pass

        try:
            sock_cmd.close()
        except Exception:
            pass

        try:
            log_file.flush()
            log_file.close()
        except Exception:
            pass

        try:
            csv_file.flush()
            csv_file.close()
        except Exception:
            pass

        duration = max(
            time.time() - STATE.start_time,
            1.0
        )

        print()
        print("==============================================")
        print(" FINAL REPORT V5")
        print("==============================================")
        print(f"Duration:             {duration:.1f} s")
        print(f"UDP packets:          {STATE.packet_count}")
        print(
            f"Total data:           "
            f"{STATE.total_bytes / 1024 / 1024:.2f} MB"
        )
        print(f"Complete frames:      {STATE.complete_frames}")
        print(f"Incomplete frames:    {STATE.incomplete_frames}")
        print(f"Corrupt frames:       {STATE.corrupt_frames}")
        print(f"Lost fragments:       {STATE.lost_fragments}")
        print(f"Duplicate packets:    {STATE.duplicates}")
        print(f"Out-of-order:         {STATE.out_of_order}")
        print(f"Frame gaps:           {STATE.frame_gaps}")
        print(f"JPEG decode OK:       {STATE.jpeg_ok}")
        print(f"JPEG decode FAILED:   {STATE.jpeg_failed}")
        print(f"Replaced frames:      {STATE.replaced_frames}")
        print(f"Microscope re-init:   {STATE.microscope_reinit_count}")
        print(
            f"Packet rate:          "
            f"{STATE.packet_count / duration:.1f} packets/s"
        )
        print(
            f"Data rate:            "
            f"{STATE.total_bytes * 8 / duration / 1_000_000:.2f} Mbit/s"
        )
        print(
            f"Frame rate:           "
            f"{STATE.complete_frames / duration:.2f} FPS"
        )
        print(f"WiFi SSID:            {STATE.wifi_ssid}")
        print(f"WiFi signal:          {STATE.wifi_signal}")
        print(f"WiFi category:        {STATE.wifi_category}")
        print(f"WiFi IPv4:            {STATE.wifi_ipv4}")
        print("==============================================")
        print(f"Text log: {TEXT_LOG}")
        print(f"CSV log:  {CSV_LOG}")
        print("==============================================")


# ============================================================
# TKINTER GUI PROCESS (MAIN PROCESS)
# ============================================================

class MicroscopeGUI:
    def __init__(
        self,
        root,
        frame_queue,
        stats_queue,
        command_queue,
        receiver_process
    ):
        self.root = root
        self.frame_queue = frame_queue
        self.stats_queue = stats_queue
        self.command_queue = command_queue
        self.receiver_process = receiver_process

        self.current_frame = None
        self.current_frame_id = -1
        self.current_frame_time = 0.0
        self.photo = None

        self.last_stats = {}
        self.last_event = ""

        self.recording = False
        self.video_writer = None
        self.recording_size = None

        self.last_frame_wall_time = None
        self.gui_fps = 0.0

        self.root.title("WiFi Mikroskop V5 - GUI")
        self.root.geometry("1280x820")
        self.root.minsize(1050, 700)

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.root.after(30, self.poll_queues)
        self.root.after(250, self.update_status)

    def _build_ui(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # --------------------------------------------------------
        # Top connection bar
        # --------------------------------------------------------
        top = ttk.Frame(self.root, padding=8)
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(4, weight=1)

        ttk.Label(
            top,
            text="Microscope IP:"
        ).grid(row=0, column=0, padx=(0, 6))

        self.ip_var = tk.StringVar(value=MIC_IP)

        self.ip_entry = ttk.Entry(
            top,
            textvariable=self.ip_var,
            width=18
        )
        self.ip_entry.grid(row=0, column=1, padx=(0, 6))

        ttk.Button(
            top,
            text="Connect / Apply IP",
            command=self.connect
        ).grid(row=0, column=2, padx=4)

        ttk.Button(
            top,
            text="Microscope INIT",
            command=self.init_microscope
        ).grid(row=0, column=3, padx=4)

        ttk.Button(
            top,
            text="Reconnect WiFi",
            command=self.reconnect_wifi
        ).grid(row=0, column=5, padx=4)

        # --------------------------------------------------------
        # Main split
        # --------------------------------------------------------
        main = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        main.grid(row=1, column=0, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=0)
        main.rowconfigure(0, weight=1)

        # Video
        video_frame = ttk.LabelFrame(
            main,
            text="LIVE VIDEO",
            padding=6
        )
        video_frame.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(0, 8)
        )
        video_frame.columnconfigure(0, weight=1)
        video_frame.rowconfigure(0, weight=1)

        self.video_label = ttk.Label(
            video_frame,
            text="Čakám na validný video frame...",
            anchor="center"
        )
        self.video_label.grid(
            row=0,
            column=0,
            sticky="nsew"
        )

        # Right dashboard
        side = ttk.Frame(main)
        side.grid(
            row=0,
            column=1,
            sticky="ns"
        )

        self._build_status_panel(side)
        self._build_stats_panel(side)
        self._build_controls(side)

        # --------------------------------------------------------
        # Event log
        # --------------------------------------------------------
        event_frame = ttk.LabelFrame(
            self.root,
            text="EVENT / RECEIVER LOG",
            padding=6
        )
        event_frame.grid(
            row=2,
            column=0,
            sticky="ew",
            padx=8,
            pady=(0, 8)
        )
        event_frame.columnconfigure(0, weight=1)

        self.event_var = tk.StringVar(value="---")
        ttk.Label(
            event_frame,
            textvariable=self.event_var,
            anchor="w"
        ).grid(
            row=0,
            column=0,
            sticky="ew"
        )

    def _build_status_panel(self, parent):
        frame = ttk.LabelFrame(
            parent,
            text="CONNECTION STATUS",
            padding=8
        )
        frame.pack(fill="x", pady=(0, 8))

        self.status_vars = {}

        rows = [
            ("Stream", "stream"),
            ("WiFi", "wifi"),
            ("SSID", "ssid"),
            ("Signal", "signal"),
            ("Network", "network"),
            ("IPv4", "ipv4"),
        ]

        for r, (label, key) in enumerate(rows):
            ttk.Label(
                frame,
                text=label + ":"
            ).grid(
                row=r,
                column=0,
                sticky="w",
                padx=(0, 8),
                pady=2
            )

            var = tk.StringVar(value="---")
            self.status_vars[key] = var

            ttk.Label(
                frame,
                textvariable=var,
                width=24
            ).grid(
                row=r,
                column=1,
                sticky="w",
                pady=2
            )

    def _build_stats_panel(self, parent):
        frame = ttk.LabelFrame(
            parent,
            text="LIVE STATISTICS",
            padding=8
        )
        frame.pack(fill="x", pady=(0, 8))

        self.stat_vars = {}

        rows = [
            ("FPS", "fps"),
            ("GUI FPS", "gui_fps"),
            ("UDP packets/s", "packet_rate"),
            ("Data rate", "mbit_s"),
            ("Latency", "latency"),
            ("Lost fragments", "lost"),
            ("Loss estimate", "loss"),
            ("Corrupt frames", "corrupt"),
            ("Dropped old", "replaced"),
            ("Frame ID", "frame_id"),
            ("Re-init", "reinit"),
        ]

        for r, (label, key) in enumerate(rows):
            ttk.Label(
                frame,
                text=label + ":"
            ).grid(
                row=r,
                column=0,
                sticky="w",
                padx=(0, 8),
                pady=2
            )

            var = tk.StringVar(value="---")
            self.stat_vars[key] = var

            ttk.Label(
                frame,
                textvariable=var,
                width=18
            ).grid(
                row=r,
                column=1,
                sticky="e",
                pady=2
            )

    def _build_controls(self, parent):
        frame = ttk.LabelFrame(
            parent,
            text="CONTROLS",
            padding=8
        )
        frame.pack(fill="x")

        self.rec_button = ttk.Button(
            frame,
            text="Start REC",
            command=self.toggle_recording
        )
        self.rec_button.pack(fill="x", pady=3)

        ttk.Button(
            frame,
            text="Snapshot",
            command=self.snapshot
        ).pack(fill="x", pady=3)

        self.detail_var = tk.BooleanVar(value=False)

        ttk.Checkbutton(
            frame,
            text="Detailed packet log",
            variable=self.detail_var,
            command=self.toggle_detail_log
        ).pack(anchor="w", pady=4)

        ttk.Separator(frame).pack(fill="x", pady=5)

        ttk.Label(
            frame,
            text=(
                "Receiver je samostatný proces.\n"
                "GUI fronta má max. 1 frame.\n"
                "Starší GUI frame sa zahodí."
            )
        ).pack(anchor="w")

    def send(self, action, **kwargs):
        item = {"action": action}
        item.update(kwargs)

        try:
            self.command_queue.put_nowait(item)
        except Exception:
            self.event_var.set("Command queue nedostupná.")

    def connect(self):
        ip = self.ip_var.get().strip()
        self.send("connect", ip=ip)

    def init_microscope(self):
        self.send("init")

    def reconnect_wifi(self):
        self.send("wifi_reconnect")

    def toggle_detail_log(self):
        self.send(
            "detailed",
            enabled=bool(self.detail_var.get())
        )

    def snapshot(self):
        if self.current_frame is None:
            self.event_var.set("Snapshot: zatiaľ nemám validný frame.")
            return

        filename = (
            "mikroskop_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            ".png"
        )

        try:
            if cv2.imwrite(filename, self.current_frame):
                self.event_var.set(
                    f"Snapshot uložený: {filename}"
                )
            else:
                self.event_var.set(
                    "Snapshot sa nepodarilo uložiť."
                )
        except Exception as exc:
            self.event_var.set(
                f"Snapshot error: {exc}"
            )

    def toggle_recording(self):
        if not self.recording:
            if self.current_frame is None:
                self.event_var.set(
                    "REC: zatiaľ nemám validný frame."
                )
                return

            h, w = self.current_frame.shape[:2]

            filename = (
                "mikroskop_zaznam_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                ".avi"
            )

            fourcc = cv2.VideoWriter_fourcc(*"XVID")
            writer = cv2.VideoWriter(
                filename,
                fourcc,
                RECORDING_FPS,
                (w, h)
            )

            if not writer.isOpened():
                self.event_var.set(
                    "REC: nepodarilo sa otvoriť video writer."
                )
                return

            self.video_writer = writer
            self.recording = True
            self.recording_size = (w, h)

            self.rec_button.configure(
                text="Stop REC"
            )

            self.event_var.set(
                f"REC spustené: {filename}"
            )

        else:
            self.stop_recording()

    def stop_recording(self):
        self.recording = False

        if self.video_writer is not None:
            try:
                self.video_writer.release()
            except Exception:
                pass

        self.video_writer = None
        self.recording_size = None

        self.rec_button.configure(
            text="Start REC"
        )

        self.event_var.set("REC zastavené.")

    def poll_queues(self):
        # Drain frame queue completely and keep ONLY newest frame.
        newest = None

        while True:
            try:
                newest = self.frame_queue.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break

        if newest is not None:
            frame_id, frame_time, frame = newest

            self.current_frame_id = frame_id
            self.current_frame_time = frame_time
            self.current_frame = frame

            # GUI FPS is based on actually displayed/newest frames.
            now = time.time()

            if self.last_frame_wall_time is not None:
                dt = now - self.last_frame_wall_time

                if dt > 0:
                    instant = 1.0 / dt
                    self.gui_fps = (
                        self.gui_fps * 0.8
                        + instant * 0.2
                    )

            self.last_frame_wall_time = now

        # Drain stats queue completely and keep newest snapshot.
        newest_stats = None

        while True:
            try:
                newest_stats = self.stats_queue.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break

        if newest_stats is not None:
            self.last_stats = newest_stats

        self.render_video()

        if self.recording and self.current_frame is not None:
            try:
                if self.video_writer is not None:
                    h, w = self.current_frame.shape[:2]

                    if self.recording_size == (w, h):
                        self.video_writer.write(
                            self.current_frame
                        )
            except Exception as exc:
                self.event_var.set(
                    f"REC error: {exc}"
                )
                self.stop_recording()

        self.root.after(30, self.poll_queues)

    def render_video(self):
        frame = self.current_frame

        if frame is None:
            self.video_label.configure(
                image="",
                text="Čakám na validný video frame..."
            )
            return

        try:
            # Convert BGR -> RGB for Tkinter/Pillow.
            rgb = cv2.cvtColor(
                frame,
                cv2.COLOR_BGR2RGB
            )

            image = Image.fromarray(rgb)

            # Fit into available video area without stretching.
            box_w = max(
                self.video_label.winfo_width(),
                640
            )
            box_h = max(
                self.video_label.winfo_height(),
                480
            )

            image.thumbnail(
                (box_w, box_h),
                Image.Resampling.LANCZOS
            )

            self.photo = ImageTk.PhotoImage(image)

            self.video_label.configure(
                image=self.photo,
                text=""
            )

        except Exception as exc:
            self.video_label.configure(
                image="",
                text=f"Video GUI error: {exc}"
            )

    def update_status(self):
        s = self.last_stats

        if s:
            stream = s.get("stream_status", "WAITING")
            wifi = s.get("wifi_connected", False)

            self.status_vars["stream"].set(
                stream
            )

            self.status_vars["wifi"].set(
                "CONNECTED" if wifi else "LOST"
            )

            self.status_vars["ssid"].set(
                s.get("wifi_ssid", "") or "---"
            )

            self.status_vars["signal"].set(
                s.get("wifi_signal", "") or "---"
            )

            self.status_vars["network"].set(
                s.get("wifi_category", "") or "---"
            )

            self.status_vars["ipv4"].set(
                s.get("wifi_ipv4", "") or "---"
            )

            self.stat_vars["fps"].set(
                f"{s.get('fps', 0.0):.1f}"
            )

            self.stat_vars["gui_fps"].set(
                f"{self.gui_fps:.1f}"
            )

            self.stat_vars["packet_rate"].set(
                f"{s.get('packet_rate', 0.0):.0f}"
            )

            self.stat_vars["mbit_s"].set(
                f"{s.get('mbit_s', 0.0):.2f} Mbit/s"
            )

            self.stat_vars["latency"].set(
                f"{s.get('latency_ms', 0.0):.0f} ms"
            )

            self.stat_vars["lost"].set(
                f"{s.get('lost_fragments', 0)}"
            )

            self.stat_vars["loss"].set(
                f"{s.get('loss_pct', 0.0):.2f}%"
            )

            self.stat_vars["corrupt"].set(
                f"{s.get('corrupt_frames', 0)}"
            )

            self.stat_vars["replaced"].set(
                f"{s.get('replaced_frames', 0)}"
            )

            self.stat_vars["frame_id"].set(
                f"{s.get('latest_frame_id', -1)}"
            )

            self.stat_vars["reinit"].set(
                f"{s.get('reinit_count', 0)}"
            )

            event = s.get("last_event", "")

            if event and event != self.last_event:
                self.last_event = event
                self.event_var.set(event)

        self.root.after(250, self.update_status)

    def close(self):
        self.stop_recording()

        try:
            self.send("stop")
        except Exception:
            pass

        self.root.after(
            100,
            self._finish_close
        )

    def _finish_close(self):
        if self.receiver_process.is_alive():
            self.receiver_process.join(
                timeout=2.0
            )

        if self.receiver_process.is_alive():
            self.receiver_process.terminate()
            self.receiver_process.join(
                timeout=1.0
            )

        self.root.destroy()


def main():
    """
    Main process = GUI.
    Receiver = separate process.

    This is the important architectural change:
        GUI -> queues -> receiver process
        receiver process -> queues -> GUI

    The UDP socket and frame decoder are never run from Tkinter's
    event loop.
    """
    mp.freeze_support()

    ctx = mp.get_context("spawn")

    # maxsize=1 is intentional:
    # never build a backlog of old video frames.
    frame_queue = ctx.Queue(maxsize=1)
    stats_queue = ctx.Queue(maxsize=1)
    command_queue = ctx.Queue(maxsize=32)

    receiver = ctx.Process(
        target=receiver_process_main,
        args=(
            frame_queue,
            stats_queue,
            command_queue
        ),
        name="WiFiMicroscopeReceiver"
    )

    receiver.start()

    root = tk.Tk()

    try:
        MicroscopeGUI(
            root,
            frame_queue,
            stats_queue,
            command_queue,
            receiver
        )

        root.mainloop()

    finally:
        if receiver.is_alive():
            try:
                command_queue.put_nowait(
                    {"action": "stop"}
                )
            except Exception:
                pass

            receiver.join(timeout=2.0)

        if receiver.is_alive():
            receiver.terminate()
            receiver.join(timeout=1.0)


if __name__ == "__main__":
    main()
