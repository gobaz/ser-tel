# ser-split

Lightweight serial-to-multi-client Telnet bridge in Python.

`ser-tel.py` opens one serial port and shares it with multiple Telnet clients:

- Client input -> serial TX
- Serial RX -> broadcast to all connected clients

It uses `pyserial` for serial I/O and `telnetlib3` for Telnet protocol handling.

## Features

- Multi-client Telnet server
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
UV_CACHE_DIR=/tmp/uv-cache uv run python ser-tel.py --serial /dev/ttyUSB0 --baud 115200
```

## Run Without uv

You can run the script with standard Python tooling too.
`requirements.txt` contains the runtime dependencies (`pyserial`, `telnetlib3`).

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python ser-tel.py --serial /dev/ttyUSB0 --baud 115200
```

By default, server listens on `127.0.0.1:2000`.

Connect from another terminal:

```bash
telnet 127.0.0.1 2000
```

## Common Usage

Using `uv` workflow:

Default low-latency mode:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python ser-tel.py \
  --serial /dev/ttyUSB0 \
  --baud 115200
```

Disable unbuffered serial mode:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python ser-tel.py \
  --serial /dev/ttyUSB0 \
  --baud 115200 \
  --no-unbuffered-serial
```

Expose on all interfaces (trusted networks only):

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python ser-tel.py \
  --serial /dev/ttyUSB0 \
  --baud 115200 \
  --host 0.0.0.0 \
  --allow-remote
```

## CLI Options

```text
--serial SERIAL
--baud BAUD
--host HOST
--port PORT
--allow-remote
--chunk-size CHUNK_SIZE
--serial-write-queue-size SERIAL_WRITE_QUEUE_SIZE
--serial-reconnect-delay SERIAL_RECONNECT_DELAY
--unbuffered-serial / --no-unbuffered-serial
--log-level {DEBUG,INFO,WARNING,ERROR}
```

See full help:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python ser-tel.py --help
```

## Security Notes

- Telnet is unencrypted.
- Default bind is loopback for safety.
- If you use `--allow-remote`, restrict access with firewall/VPN/isolated network.

## Troubleshooting

- `Permission denied` on serial device:
  - Check device path (`/dev/ttyUSB0`, `/dev/ttyACM0`, etc.)
  - Ensure your user has serial device access (often `dialout` group on Linux)
- No data received:
  - Verify baud rate and serial settings on the target device
  - If USB serial was unplugged, reconnect it and wait for auto-reconnect attempts
  - Test raw serial quickly with `serial.tools.miniterm` or `screen`
- Telnet connects but shell is weird:
  - Confirm remote endpoint over serial is actually a shell/console
  - Try resetting terminal on remote side (`stty sane`, TERM settings) if needed
