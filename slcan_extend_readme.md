# slcan_extend — Extended SLCAN Interface

`slcan_extend` is an extended SLCAN interface for [python-can](https://python-can.readthedocs.io/), designed for custom CAN USB adapters that support **precise sample point selection** via a firmware lookup table index.

The standard SLCAN interface only allows selecting bitrate (e.g. `S6` = 500 Kbps). This extension adds the ability to also select the exact sample point by appending a table index to the command:

```
S<bitrate_code><index>    →  Classic CAN   e.g.  S600  = 500K, index 0
Y<fd_code><index>         →  CAN FD data   e.g.  Y205  = 2Mbps, index 5
```

If no index is provided, the behavior is **identical to the original `slcan` interface** — fully backward compatible.

---

## Requirements

- Python ≥ 3.9
- Git

---

## Installation

```bash
pip install git+https://github.com/tuyennd89/python-can.git@feature/slcan-fd-samplerate
```

---

## Usage

### 1. Basic — no index (identical to original slcan)

```python
import can

bus = can.Bus(
    interface="slcan_extend",
    channel="COM3",        # Windows: COM3, COM4, ...
                           # Linux:   /dev/ttyUSB0, /dev/ttyUSB1, ...
                           # macOS:   /dev/tty.usbserial-*
    tty_baudrate=115200,
    bitrate=500_000,
)

# Send a message
msg = can.Message(
    arbitration_id=0x123,
    data=[0x01, 0x02, 0x03],
    is_extended_id=False,
)
bus.send(msg)

# Receive a message
msg = bus.recv(timeout=1.0)
if msg:
    print(msg)

bus.shutdown()
```

---

### 2. Classic CAN with specific sample point

Look up the desired index from [`slcan_extend_baudrate_tables.xlsx`](./slcan_extend_baudrate_tables.xlsx), then pass it as `bitrate_cfg_index`:

```python
import can

bus = can.Bus(
    interface="slcan_extend",
    channel="COM3",
    tty_baudrate=115200,
    bitrate=500_000,
    bitrate_cfg_index=0,    # index 0 → 87.5% sample point
)
```

---

### 3. CAN FD — nominal and data phase with different sample points

```python
import can

bus = can.Bus(
    interface="slcan_extend",
    channel="COM3",
    tty_baudrate=115200,
    bitrate=500_000,
    bitrate_cfg_index=0,          # nominal 500K  → index 0  = 87.5% SP
    data_bitrate=2_000_000,
    data_bitrate_cfg_index=24,    # FD data 2Mbps → index 24 = 80.0% SP
)
```

---

### 4. Find index by target sample point (lookup helper)

```python
import can
from can.interfaces.slcan_sp_tables import lookup_cfg_index

# Find the index closest to 87.5% for 500K nominal
nom_idx, nom_cfg = lookup_cfg_index(500_000, 87.5)
print(f"Nominal  → index={nom_idx}, actual SP={nom_cfg.sample_point:.2f}%")

# Find the index closest to 80% for 2Mbps FD data
fd_idx, fd_cfg = lookup_cfg_index(2_000_000, 80.0, fd_data=True)
print(f"FD data  → index={fd_idx}, actual SP={fd_cfg.sample_point:.2f}%")

bus = can.Bus(
    interface="slcan_extend",
    channel="COM3",
    tty_baudrate=115200,
    bitrate=500_000,
    bitrate_cfg_index=nom_idx,
    data_bitrate=2_000_000,
    data_bitrate_cfg_index=fd_idx,
)
```

---

## Baudrate Lookup Tables

See **[`slcan_extend_baudrate_tables.xlsx`](./slcan_extend_baudrate_tables.xlsx)** for the complete list of available indexes for each bitrate. Each sheet corresponds to one bitrate. Rows highlighted in yellow indicate common sample points (50%, 75%, 80%, 87.5%, 90%).

### Supported bitrates

| Bitrate  | Command code | Sheet in xlsx |
|----------|:---:|---|
| 10 Kbps  | `0` | `10K`   |
| 20 Kbps  | `1` | `20K`   |
| 50 Kbps  | `2` | `50K`   |
| 100 Kbps | `3` | `100K`  |
| 125 Kbps | `4` | `125K`  |
| 250 Kbps | `5` | `250K`  |
| 500 Kbps | `6` | `500K`  |
| 1 Mbps   | `8` | `1000K` |

### Supported CAN FD data bitrates

| Data Bitrate | Command code | Sheet in xlsx |
|--------------|:---:|---|
| 2 Mbps       | `2` | `FD_2M` |
| 5 Mbps       | `5` | `FD_5M` |

---

## Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `channel` | `str` | — | Serial port name (e.g. `COM3`, `/dev/ttyUSB0`) |
| `tty_baudrate` | `int` | `115200` | Serial baud rate |
| `bitrate` | `int` | `None` | Nominal CAN bitrate in bit/s |
| `bitrate_cfg_index` | `int` | `None` | Lookup table index for nominal bitrate. `None` = original command |
| `data_bitrate` | `int` | `None` | CAN FD data-phase bitrate in bit/s |
| `data_bitrate_cfg_index` | `int` | `None` | Lookup table index for FD data bitrate. `None` = original command |
| `listen_only` | `bool` | `False` | Open in listen-only mode |
| `rtscts` | `bool` | `False` | Enable RTS/CTS hardware handshake |
| `sleep_after_open` | `float` | `2.0` | Seconds to wait after opening serial port |
| `timeout` | `float` | `0.001` | Serial read timeout in seconds |
