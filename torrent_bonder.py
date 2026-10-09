"""TorrentBond: Windows-first BitTorrent GUI powered by aria2 (no libtorrent)."""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

APP_NAME = "TorrentBond"
RPC_TIMEOUT = 1.8

try:
    import psutil  # Optional fallback for non-Windows systems.
except ImportError:
    psutil = None


def normalize_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def format_rate(value: Any) -> str:
    try:
        number = max(0.0, float(value or 0))
    except (TypeError, ValueError):
        number = 0.0
    if number >= 1024 * 1024:
        return f"{number / (1024 * 1024):.2f} MiB/s"
    if number >= 1024:
        return f"{number / 1024:.1f} KiB/s"
    return f"{number:.0f} B/s"


def format_size(value: Any) -> str:
    try:
        number = max(0, int(value or 0))
    except (TypeError, ValueError):
        number = 0
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(number)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds > 365 * 24 * 3600:
        return "—"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


@dataclass
class NetworkInterface:
    name: str
    ips: list[str]
    is_up: bool = True

    @property
    def display(self) -> str:
        return f"{self.name}  —  {', '.join(self.ips)}"


def run_powershell_json(script: str, timeout: float = 5.0) -> Any:
    powershell = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise RuntimeError("PowerShell was not found")
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "PowerShell network query failed")
    output = result.stdout.strip()
    return json.loads(output) if output else None


def discover_interfaces() -> list[NetworkInterface]:
    """Discover usable IPv4 addresses, using Windows PowerShell or optional psutil."""
    if os.name == "nt":
        script = r'''
$up = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' } | Select-Object -ExpandProperty Name)
$addresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {
  ($up -contains $_.InterfaceAlias) -and
  ($_.IPAddress -notlike '127.*') -and
  ($_.IPAddress -notlike '169.254.*') -and
  ($_.AddressState -ne 'Duplicate') -and ($_.AddressState -ne 'Tentative')
} | Select-Object InterfaceAlias,IPAddress)
@{ Adapters = @($up); Addresses = $addresses } | ConvertTo-Json -Depth 5 -Compress
'''
        try:
            data = run_powershell_json(script)
            if isinstance(data, dict):
                names = [str(x) for x in normalize_list(data.get("Adapters")) if x]
                addresses = normalize_list(data.get("Addresses"))
                ips_by_name: dict[str, list[str]] = {name: [] for name in names}
                for item in addresses:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("InterfaceAlias", ""))
                    ip = str(item.get("IPAddress", ""))
                    if name in ips_by_name and ip and ip not in ips_by_name[name]:
                        ips_by_name[name].append(ip)
                found = [NetworkInterface(name, ips) for name, ips in ips_by_name.items() if ips]
                if found:
                    return sort_interfaces(found)
        except Exception:
            pass

    if psutil is None:
        return []
    try:
        import socket as socket_module
        all_addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        result: list[NetworkInterface] = []
        for name, addresses in all_addrs.items():
            state = stats.get(name)
            if not state or not state.isup:
                continue
            ips = []
            for address in addresses:
                if address.family != socket_module.AF_INET:
                    continue
                ip = address.address
                if ip and not ip.startswith(("127.", "169.254.")) and ip not in ips:
                    ips.append(ip)
            if ips:
                result.append(NetworkInterface(name, ips))
        return sort_interfaces(result)
    except Exception:
        return []


def sort_interfaces(items: list[NetworkInterface]) -> list[NetworkInterface]:
    preferred = ("wi-fi", "wifi", "wireless", "wlan", "ethernet", "rndis", "remote ndis", "usb", "tether", "mobile")
    return sorted(items, key=lambda item: (0 if any(word in item.name.casefold() for word in preferred) else 1, item.name.casefold()))


def get_interface_counters() -> dict[str, tuple[int, int]]:
    if os.name == "nt":
        try:
            data = run_powershell_json(
                "Get-NetAdapterStatistics -ErrorAction SilentlyContinue | "
                "Select-Object Name,ReceivedBytes,SentBytes | ConvertTo-Json -Depth 3 -Compress",
                timeout=4.0,
            )
            result: dict[str, tuple[int, int]] = {}
            for item in normalize_list(data):
                if isinstance(item, dict) and item.get("Name") is not None:
                    result[str(item["Name"])] = (int(item.get("ReceivedBytes") or 0), int(item.get("SentBytes") or 0))
            if result:
                return result
        except Exception:
            pass
    if psutil is not None:
        try:
            return {
                name: (int(counter.bytes_recv), int(counter.bytes_sent))
                for name, counter in psutil.net_io_counters(pernic=True).items()
            }
        except Exception:
            pass
    return {}


def find_aria2() -> str | None:
    found = shutil.which("aria2c.exe") or shutil.which("aria2c")
    if found:
        return str(Path(found).resolve())
    here = Path(__file__).resolve().parent
    candidates = [here / "aria2c.exe", here / "tools" / "aria2c.exe"]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data)
        candidates.append(root / "Microsoft" / "WinGet" / "Links" / "aria2c.exe")
        packages = root / "Microsoft" / "WinGet" / "Packages"
        try:
            candidates.extend(packages.glob("aria2.aria2_*/*/aria2c.exe"))
            candidates.extend(packages.glob("aria2.aria2_*/*/*/aria2c.exe"))
        except OSError:
            pass
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


class TorrentBondApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("TorrentBond — aria2 Multi-Interface Torrent Client")
        self.root.geometry("1240x860")
        self.root.minsize(1060, 740)
        self.root.configure(bg="#10151e")

        self.state_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "TorrentBond"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = self.state_dir / "aria2.session"
        self.log_file_path = self.state_dir / "aria2.log"
        self.download_dir = tk.StringVar(value=str(Path.home() / "Downloads" / "TorrentBond"))
        self.magnet_text = tk.StringVar()
        self.aria2_path = find_aria2() or ""
        self.network_summary = tk.StringVar(value="Discovering interfaces…")
        self.engine_summary = tk.StringVar(value="Starting aria2…")
        self.adapter_rate_summary = tk.StringVar(value="Adapter traffic will appear here.")
        self.total_down_var = tk.StringVar(value="0 B/s")
        self.total_up_var = tk.StringVar(value="0 B/s")
        self.active_count_var = tk.StringVar(value="0")
        self.selected_links_var = tk.StringVar(value="0 selected")
        self.detail_name_var = tk.StringVar(value="Select a torrent to inspect its live stats")
        self.detail_progress_text_var = tk.StringVar(value="No torrent selected")
        self.detail_downloaded_var = tk.StringVar(value="—")
        self.detail_peers_var = tk.StringVar(value="—")
        self.detail_connections_var = tk.StringVar(value="—")
        self.detail_seeders_var = tk.StringVar(value="—")
        self.detail_down_var = tk.StringVar(value="—")
        self.detail_up_var = tk.StringVar(value="—")
        self.detail_eta_var = tk.StringVar(value="—")
        self.detail_status_var = tk.StringVar(value="Waiting for selection")
        self.detail_progress = tk.DoubleVar(value=0.0)

        self.interfaces: list[NetworkInterface] = []
        self.interface_vars: dict[str, tk.BooleanVar] = {}
        self.previous_io: dict[str, tuple[int, int]] = {}
        self.task_cache: dict[str, dict[str, Any]] = {}
        self.peer_cache: dict[str, dict[str, int | None]] = {}
        self.last_peer_poll: dict[str, float] = {}
        self.previous_io_time = time.monotonic()
        self.last_adapter_poll = 0.0
        self.last_rpc_error = ""
        self.last_log_error = ""
        self.process: subprocess.Popen | None = None
        self.log_stream = None
        self.rpc_secret = secrets.token_urlsafe(32)
        self.rpc_port = self._choose_rpc_port()
        self.closing = False
        self._build_ui()
        self.refresh_interfaces(initial=True)
        self._start_engine()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(900, self.refresh_status)

    def _choose_rpc_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#0A0F1B"
        panel = "#111A2B"
        panel_alt = "#152136"
        text = "#EDF3FF"
        muted = "#91A2BE"
        accent = "#7C93FF"
        style.configure("TFrame", background=bg)
        style.configure("App.TFrame", background=bg)
        style.configure("Card.TFrame", background=panel)
        style.configure("Metric.TFrame", background=panel)
        style.configure("Inner.TFrame", background=panel_alt)
        style.configure("TLabel", background=bg, foreground=text, font=("Segoe UI", 9))
        style.configure("Muted.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        style.configure("Card.TLabel", background=panel, foreground=text, font=("Segoe UI", 9))
        style.configure("CardMuted.TLabel", background=panel, foreground=muted, font=("Segoe UI", 8))
        style.configure("Eyebrow.TLabel", background=bg, foreground="#A8B7D1", font=("Segoe UI Semibold", 8))
        style.configure("Title.TLabel", background=bg, foreground="#F5F7FF", font=("Segoe UI Semibold", 23))
        style.configure("Subtitle.TLabel", background=bg, foreground=muted, font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=panel, foreground="#CAD6EA", font=("Segoe UI Semibold", 10))
        style.configure("MetricLabel.TLabel", background=panel, foreground=muted, font=("Segoe UI Semibold", 8))
        style.configure("MetricValue.TLabel", background=panel, foreground="#F5F7FF", font=("Segoe UI Semibold", 15))
        style.configure("DetailValue.TLabel", background=panel, foreground="#F5F7FF", font=("Segoe UI Semibold", 10))
        style.configure("TEntry", fieldbackground="#0B1322", foreground=text, insertcolor=text,
                        bordercolor="#26344D", lightcolor="#26344D", darkcolor="#26344D", padding=(9, 7))
        style.configure("TButton", font=("Segoe UI Semibold", 9), padding=(10, 7),
                        background="#26354E", foreground=text, borderwidth=0)
        style.map("TButton", background=[("active", "#344764"), ("disabled", "#202B3E")],
                  foreground=[("disabled", "#74839A")])
        style.configure("Primary.TButton", background=accent, foreground="#091022", padding=(11, 8))
        style.map("Primary.TButton", background=[("active", "#9BACFF"), ("disabled", "#424D6D")],
                  foreground=[("disabled", "#7B86A3")])
        style.configure("Secondary.TButton", background="#1E2B42", foreground="#E7EEFC", padding=(9, 7))
        style.map("Secondary.TButton", background=[("active", "#2B3D5A")])
        style.configure("Interface.TCheckbutton", background=panel_alt, foreground=text,
                        font=("Segoe UI Semibold", 9), padding=(4, 5))
        style.map("Interface.TCheckbutton", background=[("active", panel_alt)], foreground=[("disabled", muted)],
                  indicatorcolor=[("selected", accent), ("!selected", "#26354E")])
        style.configure("TCheckbutton", background=panel, foreground=text, font=("Segoe UI", 9))
        style.map("TCheckbutton", background=[("active", panel)])
        style.configure("Treeview", background="#0E1727", foreground="#E6EDF9", fieldbackground="#0E1727",
                        rowheight=30, borderwidth=0, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background="#1A2940", foreground="#AFC0DC",
                        font=("Segoe UI Semibold", 8), padding=(8, 9), relief="flat")
        style.map("Treeview", background=[("selected", "#263B68")], foreground=[("selected", "#FFFFFF")])
        style.configure("Torrent.Horizontal.TProgressbar", troughcolor="#25334C", background="#53E0BE",
                        bordercolor="#25334C", lightcolor="#53E0BE", darkcolor="#53E0BE", thickness=9)
        style.configure("Vertical.TScrollbar", background="#17243A", troughcolor="#0E1727", bordercolor="#0E1727",
                        arrowcolor="#AFC0DC")

    def _build_ui(self) -> None:
        self._configure_style()
        self.root.configure(bg="#0A0F1B")
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(20, 16, 20, 14))
        outer.pack(fill="both", expand=True)

        # App header
        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 13))
        mark = tk.Frame(header, bg="#7C93FF", width=42, height=42)
        mark.pack(side="left", padx=(0, 12))
        mark.pack_propagate(False)
        tk.Label(mark, text="TB", bg="#7C93FF", fg="#0A1020", font=("Segoe UI Semibold", 14)).pack(expand=True)
        title_col = ttk.Frame(header, style="App.TFrame")
        title_col.pack(side="left", fill="x", expand=True)
        ttk.Label(title_col, text="TorrentBond", style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_col, text="A clean control room for torrents and multi-link downloads", style="Subtitle.TLabel").pack(anchor="w", pady=(1, 0))
        status_col = ttk.Frame(header, style="App.TFrame")
        status_col.pack(side="right", anchor="center")
        tk.Label(status_col, text="ARIA2 ENGINE", bg="#182B35", fg="#75E5C6",
                 font=("Segoe UI Semibold", 8), padx=10, pady=6).pack(side="left", padx=(0, 8))
        ttk.Button(status_col, text="Locate engine", style="Secondary.TButton", command=self.choose_aria2).pack(side="left")

        # Live summary cards
        metrics = ttk.Frame(outer, style="App.TFrame")
        metrics.pack(fill="x", pady=(0, 12))
        self._make_metric_card(metrics, "DOWNLOAD SPEED", self.total_down_var, "#53E0BE", 0)
        self._make_metric_card(metrics, "UPLOAD SPEED", self.total_up_var, "#7C93FF", 1)
        self._make_metric_card(metrics, "ACTIVE DOWNLOADS", self.active_count_var, "#F8C76A", 2)
        self._make_metric_card(metrics, "SELECTED LINKS", self.selected_links_var, "#D69BFF", 3)

        # Network controls and add-download panel
        config_row = ttk.Frame(outer, style="App.TFrame")
        config_row.pack(fill="x", pady=(0, 12))
        network_card = ttk.Frame(config_row, style="Card.TFrame", padding=(14, 12))
        network_card.pack(side="left", fill="both", expand=True, padx=(0, 10))
        add_card = ttk.Frame(config_row, style="Card.TFrame", padding=(14, 12))
        add_card.pack(side="left", fill="both", expand=True)

        network_header = ttk.Frame(network_card, style="Card.TFrame")
        network_header.pack(fill="x")
        ttk.Label(network_header, text="NETWORK INTERFACES", style="Section.TLabel").pack(side="left")
        ttk.Button(network_header, text="Refresh", style="Secondary.TButton", command=self.refresh_interfaces).pack(side="right")
        self.interface_panel = ttk.Frame(network_card, style="Card.TFrame")
        self.interface_panel.pack(fill="x", pady=(9, 5))
        network_actions = ttk.Frame(network_card, style="Card.TFrame")
        network_actions.pack(fill="x", pady=(2, 0))
        ttk.Label(network_actions, text="Choose Wi-Fi + USB tethering", style="CardMuted.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Button(network_actions, text="Apply selection", style="Primary.TButton", command=self.apply_interfaces).pack(side="right")
        ttk.Button(network_actions, text="Restart", style="Secondary.TButton", command=self.restart_engine).pack(side="right", padx=(0, 6))
        ttk.Label(network_card, textvariable=self.network_summary, style="CardMuted.TLabel", wraplength=580).pack(anchor="w", pady=(6, 0))
        ttk.Label(network_card, textvariable=self.adapter_rate_summary, style="CardMuted.TLabel", wraplength=580).pack(anchor="w", pady=(4, 0))

        add_header = ttk.Frame(add_card, style="Card.TFrame")
        add_header.pack(fill="x")
        ttk.Label(add_header, text="ADD A DOWNLOAD", style="Section.TLabel").pack(side="left")
        ttk.Label(add_header, text="MAGNET OR .TORRENT", style="CardMuted.TLabel").pack(side="right")
        ttk.Label(add_card, text="Paste a magnet link to start, or open a local torrent file.", style="CardMuted.TLabel").pack(anchor="w", pady=(5, 8))
        magnet_row = ttk.Frame(add_card, style="Card.TFrame")
        magnet_row.pack(fill="x")
        ttk.Entry(magnet_row, textvariable=self.magnet_text).pack(side="left", fill="x", expand=True, ipady=2)
        ttk.Button(magnet_row, text="Add magnet", style="Primary.TButton", command=self.add_magnet).pack(side="left", padx=(7, 0))
        folder_row = ttk.Frame(add_card, style="Card.TFrame")
        folder_row.pack(fill="x", pady=(9, 0))
        ttk.Label(folder_row, text="SAVE TO", style="CardMuted.TLabel").pack(side="left", padx=(0, 8))
        ttk.Entry(folder_row, textvariable=self.download_dir).pack(side="left", fill="x", expand=True, ipady=2)
        ttk.Button(folder_row, text="Browse", style="Secondary.TButton", command=self.choose_folder).pack(side="left", padx=(7, 0))
        ttk.Button(add_card, text="Open .torrent file…", style="Secondary.TButton", command=self.add_torrent_file).pack(anchor="w", pady=(8, 0))

        # Queue header + table
        queue_header = ttk.Frame(outer, style="App.TFrame")
        queue_header.pack(fill="x", pady=(0, 7))
        ttk.Label(queue_header, text="DOWNLOAD QUEUE", foreground="#E3EBFA", background="#0A0F1B",
                  font=("Segoe UI Semibold", 10)).pack(side="left")
        ttk.Label(queue_header, textvariable=self.engine_summary, style="Muted.TLabel").pack(side="right")

        table_frame = ttk.Frame(outer, style="Card.TFrame", padding=1)
        table_frame.pack(fill="both", expand=True, pady=(0, 10))
        columns = ("name", "progress", "size", "down", "up", "peers", "connections", "seeders", "state")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=5)
        headings = {
            "name": ("TORRENT", 270, "w"), "progress": ("DONE", 65, "center"),
            "size": ("TOTAL SIZE", 82, "e"), "down": ("DOWNLOAD", 90, "e"),
            "up": ("UPLOAD", 85, "e"), "peers": ("PEERS", 60, "center"),
            "connections": ("CONN.", 58, "center"), "seeders": ("SEEDERS", 60, "center"),
            "state": ("STATUS", 100, "w"),
        }
        for col, (heading, width, anchor) in headings.items():
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=width, minwidth=48, anchor=anchor, stretch=(col == "name"))
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self._on_task_select)

        # Selected torrent detail panel
        detail_card = ttk.Frame(outer, style="Card.TFrame", padding=(14, 11))
        detail_card.pack(fill="x", pady=(0, 9))
        detail_header = ttk.Frame(detail_card, style="Card.TFrame")
        detail_header.pack(fill="x", pady=(0, 7))
        ttk.Label(detail_header, text="SELECTED TORRENT", style="Section.TLabel").pack(side="left", padx=(0, 12))
        ttk.Label(detail_header, textvariable=self.detail_name_var, style="Card.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Label(detail_header, textvariable=self.detail_progress_text_var, style="Card.TLabel",
                  foreground="#53E0BE", font=("Segoe UI Semibold", 9)).pack(side="right")
        self.detail_progressbar = ttk.Progressbar(detail_card, style="Torrent.Horizontal.TProgressbar", maximum=100,
                                                  variable=self.detail_progress)
        self.detail_progressbar.pack(fill="x", pady=(0, 9))
        detail_metrics = ttk.Frame(detail_card, style="Card.TFrame")
        detail_metrics.pack(fill="x")
        detail_items = [
            ("DOWNLOADED", self.detail_downloaded_var), ("PEERS", self.detail_peers_var),
            ("CONNECTIONS", self.detail_connections_var), ("SEEDERS", self.detail_seeders_var),
            ("DOWNLOAD", self.detail_down_var), ("UPLOAD", self.detail_up_var), ("ETA", self.detail_eta_var),
        ]
        for idx, (label, var) in enumerate(detail_items):
            cell = ttk.Frame(detail_metrics, style="Card.TFrame")
            cell.pack(side="left", fill="x", expand=True, padx=(0 if idx == 0 else 12, 0))
            ttk.Label(cell, text=label, style="MetricLabel.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=var, style="DetailValue.TLabel").pack(anchor="w", pady=(3, 0))

        actions = ttk.Frame(outer, style="App.TFrame")
        actions.pack(fill="x", pady=(0, 7))
        ttk.Button(actions, text="Pause", style="Secondary.TButton", command=self.pause_selected).pack(side="left")
        ttk.Button(actions, text="Resume", style="Secondary.TButton", command=self.resume_selected).pack(side="left", padx=(7, 0))
        ttk.Button(actions, text="Remove", style="Secondary.TButton", command=self.remove_selected).pack(side="left", padx=(7, 0))
        ttk.Button(actions, text="Open download folder", style="Secondary.TButton", command=self.open_download_folder).pack(side="left", padx=(7, 0))
        ttk.Label(actions, textvariable=self.detail_status_var, style="Muted.TLabel").pack(side="right")

        self.log = tk.Text(outer, height=2, wrap="word", bg="#080D16", fg="#93A7C6", insertbackground="white",
                           relief="flat", font=("Consolas", 8), padx=8, pady=5, highlightthickness=1,
                           highlightbackground="#17243A")
        self.log.pack(fill="x")
        self.log.configure(state="disabled")
        self._log("Ready. Select your network links, apply the selection, and add a magnet link or .torrent file.")

    def _make_metric_card(self, parent: ttk.Frame, label: str, variable: tk.StringVar, accent: str, index: int) -> None:
        card = ttk.Frame(parent, style="Metric.TFrame", padding=(12, 10))
        card.pack(side="left", fill="x", expand=True, padx=(0 if index == 0 else 8, 0))
        bar = tk.Frame(card, width=3, bg=accent)
        bar.pack(side="left", fill="y", padx=(0, 10))
        body = ttk.Frame(card, style="Metric.TFrame")
        body.pack(side="left", fill="both", expand=True)
        ttk.Label(body, text=label, style="MetricLabel.TLabel").pack(anchor="w")
        ttk.Label(body, textvariable=variable, style="MetricValue.TLabel").pack(anchor="w", pady=(4, 0))

    def _on_task_select(self, _event: Any = None) -> None:
        self._update_selected_details()
    def _log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{stamp}] {message}\n")
        self.log.see("end")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 120:
            self.log.delete("1.0", f"{line_count - 100}.0")
        self.log.configure(state="disabled")

    def choose_aria2(self) -> None:
        path = filedialog.askopenfilename(title="Select aria2c.exe", filetypes=[("aria2 executable", "aria2c.exe"), ("Executable files", "*.exe"), ("All files", "*.*")])
        if path:
            self.aria2_path = path
            self.restart_engine()

    def _selected_ips(self) -> list[str]:
        ips: list[str] = []
        for interface in self.interfaces:
            var = self.interface_vars.get(interface.name)
            if var and var.get():
                for ip in interface.ips:
                    if ip not in ips:
                        ips.append(ip)
        return ips

    def refresh_interfaces(self, initial: bool = False) -> None:
        previous = {name: value.get() for name, value in self.interface_vars.items()}
        self.interfaces = discover_interfaces()
        for child in self.interface_panel.winfo_children():
            child.destroy()
        self.interface_vars.clear()
        if not self.interfaces:
            ttk.Label(self.interface_panel, text="No active IPv4 adapters found. Connect Wi-Fi / USB tethering and refresh.",
                      style="Card.TLabel", wraplength=500).grid(row=0, column=0, sticky="w")
            self.network_summary.set("No eligible adapters found — default system routing will be used.")
            self.selected_links_var.set("0 selected")
            if not initial:
                self._log("No active IPv4 adapters found.")
            return
        preferred_words = ("wi-fi", "wifi", "wireless", "wlan", "ethernet", "rndis", "remote ndis", "usb", "tether", "mobile")
        any_default = False
        for index, interface in enumerate(self.interfaces):
            if interface.name in previous:
                checked = previous[interface.name]
            else:
                checked = any(word in interface.name.casefold() for word in preferred_words)
                if index == 0 and not any_default:
                    checked = True
            var = tk.BooleanVar(value=checked)
            self.interface_vars[interface.name] = var
            any_default = any_default or checked
            cell = ttk.Frame(self.interface_panel, style="Inner.TFrame", padding=(5, 2))
            cell.grid(row=index // 2, column=index % 2, sticky="ew", padx=(0, 7), pady=(0, 5))
            ttk.Checkbutton(cell, text=interface.name, variable=var, style="Interface.TCheckbutton",
                            command=self._update_network_summary).pack(anchor="w", fill="x")
            ttk.Label(cell, text=", ".join(interface.ips), background="#152136", foreground="#91A2BE",
                      font=("Segoe UI", 8)).pack(anchor="w", padx=(26, 5), pady=(0, 3))
        self.interface_panel.columnconfigure(0, weight=1)
        self.interface_panel.columnconfigure(1, weight=1)
        self._update_network_summary()
        if not initial:
            self._log("Network adapters refreshed. Click Apply selection to use the selected links.")

    def _update_network_summary(self) -> None:
        selected = [item for item in self.interfaces if self.interface_vars.get(item.name) and self.interface_vars[item.name].get()]
        self.selected_links_var.set(f"{len(selected)} selected")
        if not selected:
            self.network_summary.set("No interfaces selected — aria2 will use the operating system default route.")
        else:
            self.network_summary.set("Selected: " + " + ".join(item.name for item in selected))

    def _build_aria2_command(self) -> list[str]:
        path = Path(self.aria2_path) if self.aria2_path else None
        if not path or not path.is_file():
            raise FileNotFoundError("aria2c.exe was not found. Install it with 'winget install --id aria2.aria2' or use Locate aria2c.exe.")
        output_dir = Path(self.download_dir.get().strip() or (Path.home() / "Downloads" / "TorrentBond"))
        output_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.set(str(output_dir))
        args = [
            str(path),
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            "--rpc-listen-port=" + str(self.rpc_port),
            "--rpc-secret=" + self.rpc_secret,
            "--dir=" + str(output_dir),
            "--continue=true",
            "--check-integrity=true",
            "--enable-dht=true",
            "--bt-enable-lpd=true",
            "--bt-enable-pex=true",
            "--bt-save-metadata=true",
            "--rpc-save-upload-metadata=true",
            "--save-session=" + str(self.session_file),
            "--save-session-interval=30",
            "--max-download-result=1000",
            "--keep-unfinished-download-result=true",
            "--max-concurrent-downloads=5",
            "--bt-max-peers=100",
            "--disk-cache=32M",
            "--file-allocation=none",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--log-level=warn",
        ]
        if self.session_file.is_file() and self.session_file.stat().st_size:
            args.append("--input-file=" + str(self.session_file))
        selected_ips = self._selected_ips()
        if selected_ips:
            # aria2's --multiple-interface accepts comma-separated interface names or IPs.
            args.append("--multiple-interface=" + ",".join(selected_ips))
        return args

    def _start_engine(self) -> bool:
        if self.process and self.process.poll() is None:
            return True
        if self.process is not None:
            self._cleanup_process(force=False)
        try:
            command = self._build_aria2_command()
            self.log_stream = open(self.log_file_path, "ab")
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=self.log_stream,
                stderr=subprocess.STDOUT,
                cwd=str(Path(command[0]).parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            self.process = None
            self.engine_summary.set("aria2 unavailable")
            self._log(str(exc))
            return False

        for _ in range(30):
            if self.process.poll() is not None:
                break
            try:
                version = self._rpc("aria2.getVersion", [])
                aria_ver = version.get("version", "running") if isinstance(version, dict) else "running"
                self.engine_summary.set(f"aria2 {aria_ver}  |  starting…")
                self.last_rpc_error = ""
                self._log(f"aria2 engine started ({aria_ver}).")
                return True
            except Exception:
                time.sleep(0.2)
        detail = self._read_log_tail()
        self.engine_summary.set("Could not start aria2 RPC")
        self._log("aria2 did not start correctly. Check aria2.log.")
        if detail:
            self._log(detail[-700:].replace("\r", " ").replace("\n", " | "))
        self._cleanup_process(force=True)
        return False

    def _read_log_tail(self) -> str:
        try:
            with open(self.log_file_path, "rb") as stream:
                stream.seek(max(0, os.path.getsize(self.log_file_path) - 3000))
                return stream.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _rpc(self, method: str, params: list[Any] | None = None) -> Any:
        if not self.process or self.process.poll() is not None:
            raise RuntimeError("aria2 is not running")
        payload = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(6),
            "method": method,
            "params": ["token:" + self.rpc_secret, *(params or [])],
        }
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.rpc_port}/jsonrpc",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=RPC_TIMEOUT) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        if "error" in response_data:
            error = response_data["error"]
            raise RuntimeError(f"aria2 RPC error {error.get('code')}: {error.get('message')}")
        return response_data.get("result")

    def _stop_engine(self, save_session: bool = True) -> None:
        proc = self.process
        if not proc:
            return
        if proc.poll() is None:
            if save_session:
                try:
                    self._rpc("aria2.saveSession", [])
                except Exception:
                    pass
            try:
                self._rpc("aria2.shutdown", [])
            except Exception:
                pass
            try:
                proc.wait(timeout=7)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        self._cleanup_process(force=False)

    def _cleanup_process(self, force: bool) -> None:
        if self.process and self.process.poll() is None and force:
            try:
                self.process.kill()
                self.process.wait(timeout=2)
            except Exception:
                pass
        self.process = None
        if self.log_stream:
            try:
                self.log_stream.close()
            except Exception:
                pass
            self.log_stream = None

    def restart_engine(self) -> None:
        if not self.aria2_path:
            guessed = find_aria2()
            if guessed:
                self.aria2_path = guessed
        self._stop_engine(save_session=True)
        if self._start_engine():
            self._log("Engine restarted. Incomplete downloads should resume from the saved session.")
        else:
            messagebox.showerror(APP_NAME, "Could not start aria2.\n\nInstall it with:\nwinget install --id aria2.aria2\n\nThen click Locate aria2c.exe if it is not found automatically.")

    def apply_interfaces(self) -> None:
        if not self.aria2_path or not Path(self.aria2_path).is_file():
            self._update_network_summary()
            messagebox.showerror(APP_NAME, "aria2 is required before applying interfaces. Install it with:\n\nwinget install --id aria2.aria2")
            return
        selected = [item for item in self.interfaces if self.interface_vars.get(item.name) and self.interface_vars[item.name].get()]
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno(APP_NAME, "Applying interfaces restarts the download engine. Incomplete downloads should resume. Continue?"):
                return
        self._update_network_summary()
        self._stop_engine(save_session=True)
        if self._start_engine():
            if selected:
                self._log("Applied multi-interface selection: " + "; ".join(f"{item.name}={','.join(item.ips)}" for item in selected))
                self._log("aria2 will distribute supported sockets across the selected local IPv4 addresses; actual use depends on Windows routing and reachability.")
            else:
                self._log("No interfaces selected; using Windows default routing.")

    def _ensure_download_dir(self) -> str:
        folder = Path(self.download_dir.get().strip() or (Path.home() / "Downloads" / "TorrentBond"))
        folder.mkdir(parents=True, exist_ok=True)
        self.download_dir.set(str(folder))
        return str(folder)

    def choose_folder(self) -> None:
        current = self.download_dir.get().strip() or str(Path.home() / "Downloads")
        path = filedialog.askdirectory(title="Choose download folder", initialdir=current)
        if path:
            self.download_dir.set(path)

    def add_magnet(self) -> None:
        uri = self.magnet_text.get().strip()
        if not uri.lower().startswith("magnet:?"):
            messagebox.showwarning(APP_NAME, "Paste a valid magnet link beginning with magnet:?")
            return
        self._add_download(uri=uri)

    def add_torrent_file(self) -> None:
        path = filedialog.askopenfilename(title="Choose torrent file", filetypes=[("Torrent files", "*.torrent"), ("All files", "*.*")])
        if path:
            self._add_download(torrent_path=path)

    def _add_download(self, uri: str | None = None, torrent_path: str | None = None) -> None:
        if not self.process or self.process.poll() is not None:
            messagebox.showerror(APP_NAME, "aria2 is not running. Install/select aria2c.exe and restart the engine.")
            return
        try:
            options = {"dir": self._ensure_download_dir(), "continue": "true"}
            if uri:
                gid = self._rpc("aria2.addUri", [[uri], options])
                self.magnet_text.set("")
                self._log("Added magnet link to the queue.")
            elif torrent_path:
                torrent_data = Path(torrent_path).read_bytes()
                if not torrent_data:
                    raise ValueError("The selected .torrent file is empty.")
                encoded = base64.b64encode(torrent_data).decode("ascii")
                gid = self._rpc("aria2.addTorrent", [encoded, [], options])
                self._log(f"Added torrent file: {Path(torrent_path).name}")
            else:
                return
            self._log(f"aria2 task ID: {gid}")
        except Exception as exc:
            self._log(f"Could not add download: {exc}")
            messagebox.showerror(APP_NAME, f"Could not add this download:\n\n{exc}")

    def _selected_gid(self) -> str | None:
        rows = self.tree.selection()
        if not rows:
            return None
        row = rows[0]
        return row[2:] if row.startswith("g_") else None

    def pause_selected(self) -> None:
        self._task_action("aria2.pause", "Paused selected download.")

    def resume_selected(self) -> None:
        self._task_action("aria2.unpause", "Resumed selected download.")

    def _task_action(self, method: str, message: str) -> None:
        gid = self._selected_gid()
        if not gid:
            messagebox.showinfo(APP_NAME, "Select a download first.")
            return
        try:
            self._rpc(method, [gid])
            self._log(message)
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def remove_selected(self) -> None:
        gid = self._selected_gid()
        if not gid:
            messagebox.showinfo(APP_NAME, "Select a download first.")
            return
        if not messagebox.askyesno(APP_NAME, "Remove this task from aria2? Downloaded files will be kept."):
            return
        try:
            row = self.tree.item("g_" + gid, "values")
            state = str(row[-1]) if row else ""
            if state.lower() in ("active", "waiting", "paused"):
                try:
                    self._rpc("aria2.forceRemove", [gid])
                except Exception:
                    pass
            try:
                self._rpc("aria2.removeDownloadResult", [gid])
            except Exception:
                pass
            if self.tree.exists("g_" + gid):
                self.tree.delete("g_" + gid)
            self._log("Removed task from queue; downloaded files were kept.")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not remove task:\n\n{exc}")

    def open_download_folder(self) -> None:
        try:
            folder = Path(self._ensure_download_dir())
            if os.name == "nt":
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys_platform() == "Darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open download folder:\n\n{exc}")

    def _collect_tasks(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        keys = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "uploadSpeed", "connections", "numSeeders", "files", "bittorrent", "errorMessage"]
        active = self._rpc("aria2.tellActive", [keys]) or []
        waiting = self._rpc("aria2.tellWaiting", [0, 1000, keys]) or []
        stopped = self._rpc("aria2.tellStopped", [0, 1000, keys]) or []
        all_tasks: dict[str, dict[str, Any]] = {}
        for task in [*stopped, *waiting, *active]:
            if isinstance(task, dict) and task.get("gid"):
                all_tasks[str(task["gid"])] = task
        return list(all_tasks.values()), list(active)

    @staticmethod
    def _task_name(task: dict[str, Any]) -> str:
        bt = task.get("bittorrent") or {}
        info = bt.get("info") or {} if isinstance(bt, dict) else {}
        name = info.get("name") if isinstance(info, dict) else None
        if name:
            return str(name)
        files = task.get("files") or []
        if files:
            path = str(files[0].get("path", "")) if isinstance(files[0], dict) else ""
            if path:
                return Path(path).name or path
        error = task.get("errorMessage")
        if error:
            return "Download error"
        return "Fetching metadata…"

    def _render_task(self, task: dict[str, Any]) -> None:
        gid = str(task.get("gid", ""))
        if not gid:
            return
        iid = "g_" + gid
        try:
            total = int(task.get("totalLength") or 0)
            completed = int(task.get("completedLength") or 0)
            down = int(task.get("downloadSpeed") or 0)
            up = int(task.get("uploadSpeed") or 0)
            connections = int(task.get("connections") or 0)
        except (TypeError, ValueError):
            total = completed = down = up = connections = 0
        progress = 100.0 if total and completed >= total else (completed / total * 100 if total else 0.0)
        eta = (total - completed) / down if down > 0 and total > completed else None
        status = str(task.get("status", "unknown")).capitalize()
        if status == "Active" and total and completed >= total:
            status = "Seeding / complete"
        if status == "Error" and task.get("errorMessage"):
            status = "Error"
        peer_info = self.peer_cache.get(gid, {})
        peer_count = peer_info.get("count")
        seeders = peer_info.get("seeders")
        values = (
            self._task_name(task), f"{progress:.1f}%", format_size(total), format_rate(down), format_rate(up),
            str(peer_count) if peer_count is not None else "—", str(connections),
            str(seeders) if seeders is not None else "—", status,
        )
        if self.tree.exists(iid):
            self.tree.item(iid, values=values)
        else:
            self.tree.insert("", "end", iid=iid, values=values)

    def _refresh_peer_count(self, task: dict[str, Any]) -> None:
        """Refresh the actual connected peer list for a torrent, throttled per task."""
        gid = str(task.get("gid", ""))
        if not gid or str(task.get("status", "")).lower() != "active":
            return
        now = time.monotonic()
        if now - self.last_peer_poll.get(gid, 0.0) < 2.8:
            return
        self.last_peer_poll[gid] = now
        try:
            peers = self._rpc("aria2.getPeers", [gid]) or []
            if not isinstance(peers, list):
                peers = []
            seeders = sum(1 for peer in peers if isinstance(peer, dict) and str(peer.get("seeder", "false")).lower() == "true")
            self.peer_cache[gid] = {"count": len(peers), "seeders": seeders}
        except Exception:
            # Metadata-only magnet tasks or non-BitTorrent tasks can return an RPC error here.
            self.peer_cache.setdefault(gid, {"count": None, "seeders": None})

    def _update_selected_details(self) -> None:
        gid = self._selected_gid()
        task = self.task_cache.get(gid or "")
        if not task:
            self.detail_name_var.set("Select a torrent to inspect its live stats")
            self.detail_progress_text_var.set("No torrent selected")
            self.detail_downloaded_var.set("—")
            self.detail_peers_var.set("—")
            self.detail_connections_var.set("—")
            self.detail_seeders_var.set("—")
            self.detail_down_var.set("—")
            self.detail_up_var.set("—")
            self.detail_eta_var.set("—")
            self.detail_status_var.set("Waiting for selection")
            self.detail_progress.set(0.0)
            return
        total = int(task.get("totalLength") or 0)
        completed = int(task.get("completedLength") or 0)
        down = int(task.get("downloadSpeed") or 0)
        up = int(task.get("uploadSpeed") or 0)
        connections = int(task.get("connections") or 0)
        progress = 100.0 if total and completed >= total else (completed / total * 100 if total else 0.0)
        eta = (total - completed) / down if down > 0 and total > completed else None
        peer_info = self.peer_cache.get(gid, {})
        peer_count = peer_info.get("count")
        seeders = peer_info.get("seeders")
        self.detail_name_var.set(self._task_name(task))
        self.detail_progress_text_var.set(f"{progress:.1f}% complete")
        self.detail_progress.set(progress)
        self.detail_downloaded_var.set(f"{format_size(completed)} / {format_size(total)}")
        self.detail_peers_var.set(str(peer_count) if peer_count is not None else "Loading…")
        self.detail_connections_var.set(str(connections))
        self.detail_seeders_var.set(str(seeders) if seeders is not None else "—")
        self.detail_down_var.set(format_rate(down))
        self.detail_up_var.set(format_rate(up))
        self.detail_eta_var.set(format_eta(eta))
        status = str(task.get("status", "unknown")).capitalize()
        if task.get("errorMessage"):
            status = "Error: " + str(task.get("errorMessage"))[:90]
        self.detail_status_var.set(status)

    def refresh_status(self) -> None:
        if self.closing:
            return
        if self.process and self.process.poll() is None:
            try:
                tasks, active = self._collect_tasks()
                self.task_cache = {str(item.get("gid")): item for item in tasks if item.get("gid")}
                for task in active:
                    self._refresh_peer_count(task)
                for task in tasks:
                    self._render_task(task)
                total_down = sum(int(item.get("downloadSpeed") or 0) for item in active)
                total_up = sum(int(item.get("uploadSpeed") or 0) for item in active)
                self.total_down_var.set(format_rate(total_down))
                self.total_up_var.set(format_rate(total_up))
                self.active_count_var.set(str(len(active)))
                self.engine_summary.set(f"aria2 online  ·  ↓ {format_rate(total_down)}  /  ↑ {format_rate(total_up)}")
                if self.last_rpc_error:
                    self._log("aria2 RPC connection restored.")
                    self.last_rpc_error = ""
                self._update_selected_details()
            except Exception as exc:
                message = str(exc)
                if message != self.last_rpc_error:
                    self._log(f"RPC/status warning: {message}")
                    self.last_rpc_error = message
                self.engine_summary.set("aria2 online · waiting for RPC")
        elif self.aria2_path:
            self.engine_summary.set("aria2 stopped — restart the engine")
            self.total_down_var.set("0 B/s")
            self.total_up_var.set("0 B/s")
            self.active_count_var.set("0")
        else:
            self.engine_summary.set("aria2 not found — locate aria2c.exe")
        self._update_adapter_traffic()
        self.root.after(1300, self.refresh_status)

    def _update_adapter_traffic(self) -> None:
        now = time.monotonic()
        if now - self.last_adapter_poll < 2.0:
            return
        counters = get_interface_counters()
        elapsed = max(0.001, now - self.previous_io_time)
        selected = [item for item in self.interfaces if self.interface_vars.get(item.name) and self.interface_vars[item.name].get()]
        total_rx = 0.0
        total_tx = 0.0
        parts = []
        for item in selected:
            current = counters.get(item.name)
            previous = self.previous_io.get(item.name)
            rx = max(0, current[0] - previous[0]) / elapsed if current and previous else 0.0
            tx = max(0, current[1] - previous[1]) / elapsed if current and previous else 0.0
            total_rx += rx
            total_tx += tx
            parts.append(f"{item.name}: RX {format_rate(rx)} / TX {format_rate(tx)}")
        self.previous_io = counters
        self.previous_io_time = now
        self.last_adapter_poll = now
        if selected:
            self.adapter_rate_summary.set("Adapter traffic (all apps): " + "; ".join(parts) + f"  |  Combined RX {format_rate(total_rx)} / TX {format_rate(total_tx)}")
        else:
            self.adapter_rate_summary.set("Adapter traffic: no selected interfaces")

    def on_close(self) -> None:
        self.closing = True
        self._stop_engine(save_session=True)
        self.root.destroy()


def sys_platform() -> str:
    import sys
    return sys.platform


def main() -> None:
    root = tk.Tk()
    app = TorrentBondApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
