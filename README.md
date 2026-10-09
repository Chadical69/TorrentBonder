# TorrentBond — aria2 Edition

TorrentBond is a Windows-first desktop BitTorrent client with a refined dark dashboard. It uses **aria2** as its download engine and does not require `libtorrent` or any Python torrent-binding package.

## What's in the UI

- Live overview cards for total download speed, upload speed, active downloads, and selected network links
- A download queue showing completion percentage, total size, per-torrent download/upload speeds, connected peers, connections, connected seeders, and status
- A selected-torrent panel with a progress bar, downloaded-versus-total data, peer/connection counts, speeds, seeders, and ETA
- Checkboxes for active IPv4 network adapters so you can select Wi-Fi, USB tethering, Ethernet, or another eligible interface
- Per-adapter RX/TX traffic estimates and combined adapter traffic
- Add magnet links or open `.torrent` files, choose the save folder, pause/resume downloads, remove tasks, and open the download directory

Peer counts are read from aria2's `aria2.getPeers` RPC method. Connection counts come from the task's `connections` status field. Adapter RX/TX counters include traffic from all applications, not just TorrentBond.

## Multi-interface behavior

The app passes the selected adapter IPv4 addresses to aria2's `--multiple-interface` option. This requests link aggregation by distributing supported sockets across the selected interfaces. Actual throughput depends on Windows routes, adapter reachability, NAT/mobile-carrier behavior, and the peers available in the swarm; selecting two interfaces does not guarantee that measured speeds will add together.

## Requirements

- Windows 10 or Windows 11
- Python 3.10+ with Tkinter (the regular python.org Windows installer normally includes Tkinter)
- aria2 (`aria2c.exe`)

No `pip install libtorrent` is needed. The app uses Python's standard library; adapter discovery and traffic counters use built-in PowerShell commands on Windows.

## Install and run

1. Extract the ZIP to a normal folder (not inside a protected Windows directory).
2. Double-click `install_and_run.bat`.
3. If aria2 is missing, the launcher attempts to install it with Windows Package Manager (`winget`). If `winget` is unavailable, install aria2 from the official releases page: <https://github.com/aria2/aria2/releases>.
4. If the app cannot find `aria2c.exe`, click **Locate engine** and select the executable.
5. Check the adapters you want to use, then click **Apply selection**. Applying an interface selection restarts aria2 so it can bind to the selected local IP addresses.
6. Paste a magnet link or use **Open .torrent file…** to add a torrent.

To verify aria2 is installed, run `aria2c --version` in a new PowerShell window.

## Troubleshooting

- **aria2 not found:** run `winget install --id aria2.aria2 --exact`, reopen the app, or use **Locate engine**.
- **No adapters listed:** connect Wi-Fi or USB tethering and click **Refresh**. Only active adapters with eligible IPv4 addresses are shown.
- **Peer count displays “Loading…” or “—”:** magnet links may need time to fetch metadata; a torrent with no connected peers may show zero.
- **No speed increase:** `--multiple-interface` can distribute connections but cannot guarantee that the selected links are both used effectively. Check that both adapters have usable IPv4 routes and that the swarm has enough peers.
- **RPC/engine startup error:** review `%LOCALAPPDATA%\TorrentBond\aria2.log`.
- **Downloads after restart:** the app requests a session save before stopping aria2, and incomplete downloads should resume. Downloaded files remain on disk if a task is removed from the queue.

Use BitTorrent only for content you have the right to download and share.
