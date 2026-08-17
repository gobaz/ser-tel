# ser-split

Lightweight serial-to-multi-client Telnet bridge in Python.

`ser-tel` opens one or more serial ports and shares each one with multiple
Telnet clients:

- Client input -> serial TX
- Serial RX -> broadcast to all connected clients

It uses `pyserial` for serial I/O and `telnetlib3` for Telnet protocol handling.

## Features

- Multi-client Telnet server
- Multi-serial support: repeat `--tty` to create an independent bridge per device
- Predictable TCP ports: `/dev/ttyUSB0` uses `2000`; `/dev/ttyACM0` uses `3000`
- Multiple clients can watch and interact with the same serial session at once
- Easy to automate from scripts and CI jobs (plain TCP/Telnet endpoint)
- Low-latency serial mode enabled by default
- Automatic serial reconnect when device disappears/reappears
- Clients get short in-band notices: `[serial] lost` and `[serial] reconnected`
- Safe default bind (`127.0.0.1`)
- Graceful shutdown on `Ctrl+C` / `SIGTERM`
- Bounded queue for client -> serial backpressure

## Why It's Useful

- Team debugging: one person can type commands while others monitor live output.
- Fast automation: use shell scripts, Python, or test harnesses against a stable TCP port instead of direct serial device handling.
- Tool interoperability: works with standard clients (`telnet`, terminal apps, custom TCP clients).

## Requirements

- Python `>= 3.12`
- Dependencies:
  - `pyserial>=3.5`
  - `telnetlib3>=4.0.2`

## Quick Start (uv)

This project is managed with `uv`, so this is the recommended workflow:

```bash
uv sync
uv run ser-tel --tty /dev/ttyUSB0 --baud 115200
```

## Install Command Globally (uv)

Install `ser-tel` as a persistent shell command:

```bash
uv tool install -e .
uv tool update-shell
ser-tel --help
```

Useful maintenance commands:

```bash
uv tool upgrade ser-split
uv tool uninstall ser-split
```

## Run Without uv

You can run the script with standard Python tooling too.
`requirements.txt` contains the runtime dependencies (`pyserial`, `telnetlib3`).

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
ser-tel --tty /dev/ttyUSB0 --baud 115200
```

Without `--tty`, `ser-tel` starts bridges for `/dev/ttyUSB0`,
`/dev/ttyUSB1`, and `/dev/ttyACM0`. They listen on `127.0.0.1:2000`,
`127.0.0.1:2001`, and `127.0.0.1:3000`, respectively. Missing devices stay
in their automatic reconnect loop until they become available.

Connect from another terminal:

```bash
telnet 127.0.0.1 2000
```

## Multiple Serial Devices

Repeat `--tty` for every device. Each device is served independently and
uses a TCP port derived from its device number: `ttyUSBN` uses `2000 + N`, and
`ttyACMN` uses `3000 + N`:

```bash
uv run ser-tel \
  --tty /dev/ttyUSB0 \
  --tty /dev/ttyACM0
```

This starts `/dev/ttyUSB0` at `127.0.0.1:2000` and `/dev/ttyACM0` at
`127.0.0.1:3000`. The baud rate is `115200` by default and applies to every
configured device; set a different shared value with `--baud`.

Only `ttyUSBN` and `ttyACMN` device names are accepted because the suffix
determines the TCP port. For example, `/dev/ttyUSB42` uses TCP port `2042` and
`/dev/ttyACM42` uses TCP port `3042`.

## Common Usage

Using `uv` workflow:

Default low-latency mode:

```bash
uv run ser-tel \
  --tty /dev/ttyUSB0 \
  --baud 115200
```

Use buffered serial mode:

```bash
uv run ser-tel \
  --tty /dev/ttyUSB0 \
  --baud 115200 \
  --buffered
```

Expose on all interfaces (trusted networks only):

```bash
uv run ser-tel \
  --tty /dev/ttyUSB0 \
  --baud 115200 \
  --host 0.0.0.0
```

## CLI Options

```text
--tty TTY
--baud BAUD
--host HOST
--chunk-size CHUNK_SIZE
--serial-write-queue-size SERIAL_WRITE_QUEUE_SIZE
--serial-reconnect-delay SERIAL_RECONNECT_DELAY
--unbuffered-serial / --buffered
--log-level {DEBUG,INFO,WARNING,ERROR}
```

See full help:

```bash
uv run ser-tel --help
```

## Security Notes

- Telnet is unencrypted.
- Default bind is loopback for safety.
- If you bind to a non-loopback host (for example `0.0.0.0`), restrict access with firewall/VPN/isolated network.

## Troubleshooting

- `Permission denied` on serial device:
  - Check device path (`/dev/ttyUSB0`, `/dev/ttyACM0`, etc.)
  - Ensure your user has serial device access (often `dialout` group on Linux)
- No data received:
  - Verify baud rate and serial settings on the target device
  - If USB serial was unplugged, reconnect it and wait for auto-reconnect attempts
  - Test raw serial quickly with `picocom`, `screen` etc.
- Telnet connects but shell is weird:
  - Confirm remote endpoint over serial is actually a shell/console
  - Try resetting terminal on remote side (`stty sane`, TERM settings) if needed
