#!/usr/bin/env python3
"""Serial-to-multi-client Telnet bridge with auto-reconnect support."""

import argparse
import asyncio
import logging
import queue
import re
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import serial
import telnetlib3

SERIAL_LOST_NOTICE = b"\r\n[serial] lost\r\n"
SERIAL_RECONNECTED_NOTICE = b"\r\n[serial] reconnected\r\n"
TTYUSB_PORT_BASE = 2000
TTYACM_PORT_BASE = 3000
DEFAULT_TTY_DEVICES = ("/dev/ttyUSB0", "/dev/ttyUSB1", "/dev/ttyACM0")


@dataclass(frozen=True)
class BridgeConfig:
    """Configuration for one independent serial-to-Telnet bridge."""

    device: str
    baud: int
    port: int


@dataclass
class SerialConnectionState:
    """Mutable state shared by the serial worker threads."""

    serial_lock: threading.Lock
    ser: Optional[serial.Serial] = None
    next_reconnect_log_at: float = 0.0
    serial_was_lost: bool = False
    read_thread: Optional[threading.Thread] = None
    write_thread: Optional[threading.Thread] = None


def port_for_device(device: str) -> int:
    """Map ttyUSBN to 2000 + N and ttyACMN to 3000 + N."""
    device_name = Path(device).name
    match = re.fullmatch(r"tty(USB|ACM)(\d+)", device_name)
    if match is None:
        raise ValueError(
            "device name must end in ttyUSBN or ttyACMN "
            "(for example /dev/ttyUSB0 or /dev/ttyACM0)"
        )

    port_base = TTYUSB_PORT_BASE if match.group(1) == "USB" else TTYACM_PORT_BASE
    port = port_base + int(match.group(2))
    if port > 65535:
        raise ValueError(f"derived TCP port {port} is outside the valid range")
    return port


def parse_args():
    """Parse and validate CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Share one or more serial ports with multiple Telnet clients.",
        add_help=False,
    )
    parser.add_argument("-?", "--help", action="help", help="Show this help message and exit.")
    parser.add_argument(
        "-t",
        "--tty",
        action="append",
        default=None,
        metavar="DEVICE",
        help=(
            "TTY device path; repeat for multiple bridges. TCP port is "
            "derived from the device name (ttyUSB0 -> 2000; ttyACM0 -> 3000). "
            "Default: /dev/ttyUSB0, /dev/ttyUSB1, and /dev/ttyACM0."
        ),
    )
    parser.add_argument(
        "-B",
        "--baud",
        type=int,
        default=115200,
        help="Serial baud rate (default: 115200).",
    )
    parser.add_argument(
        "-h",
        "--host",
        default="127.0.0.1",
        help="Bind host/IP address (default: 127.0.0.1).",
    )
    parser.add_argument(
        "-c",
        "--chunk-size",
        type=int,
        default=1024,
        help="Read/write chunk size in bytes (default: 1024).",
    )
    parser.add_argument(
        "-q",
        "--serial-write-queue-size",
        type=int,
        default=1024,
        help="Queue depth for client->serial data (default: 1024).",
    )
    parser.add_argument(
        "-d",
        "--serial-reconnect-delay",
        type=float,
        default=1.0,
        help=(
            "Seconds between serial reconnect attempts after disconnect/open "
            "failure (default: 1.0)."
        ),
    )
    unbuffered_group = parser.add_mutually_exclusive_group()
    unbuffered_group.add_argument(
        "-u",
        "--unbuffered-serial",
        dest="unbuffered_serial",
        action="store_true",
        help="Minimize serial buffering/latency (default mode).",
    )
    unbuffered_group.add_argument(
        "-b",
        "--buffered",
        dest="unbuffered_serial",
        action="store_false",
        help="Use buffered serial mode (default: disabled).",
    )
    parser.set_defaults(unbuffered_serial=True)
    parser.add_argument(
        "-l",
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log verbosity (default: INFO).",
    )
    args = parser.parse_args()

    if args.chunk_size <= 0:
        parser.error("--chunk-size must be > 0")
    if args.baud <= 0:
        parser.error("--baud must be > 0")
    if args.serial_write_queue_size <= 0:
        parser.error("--serial-write-queue-size must be > 0")
    if args.serial_reconnect_delay <= 0:
        parser.error("--serial-reconnect-delay must be > 0")

    devices = args.tty or DEFAULT_TTY_DEVICES
    try:
        args.bridges = tuple(
            BridgeConfig(device=device, baud=args.baud, port=port_for_device(device))
            for device in devices
        )
    except ValueError as exc:
        parser.error(str(exc))

    if len({bridge.device for bridge in args.bridges}) != len(args.bridges):
        parser.error("each --tty device must be specified only once")
    if len({bridge.port for bridge in args.bridges}) != len(args.bridges):
        parser.error("each --tty device must map to a unique TCP port")

    return args


def format_peer(peername):
    """Format a peer address tuple as `host:port` when possible."""
    if isinstance(peername, tuple) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"
    return str(peername)


class SerialTelnetRepeater:
    """Bridge one serial port to many concurrent Telnet clients."""

    def __init__(self, args, bridge: BridgeConfig):
        self.args = args
        self.bridge = bridge
        self.stop_event = threading.Event()
        self.serial_write_queue = queue.Queue(maxsize=args.serial_write_queue_size)
        self.clients = set()

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server = None
        self.serial_state = SerialConnectionState(serial_lock=threading.Lock())

    async def run(self):
        """Start workers and serve Telnet clients until stop is requested."""
        self.loop = asyncio.get_running_loop()
        self.start_serial_workers()
        try:
            self.server = await telnetlib3.create_server(
                host=self.args.host,
                port=self.bridge.port,
                shell=self.shell,
                encoding=False,
                line_mode=False,
                timeout=False,
            )
            logging.info(
                "%s: listening on %s:%d",
                self.bridge.device,
                self.args.host,
                self.bridge.port,
            )
            while not self.stop_event.is_set():
                await asyncio.sleep(0.2)
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Stop server, disconnect clients, and terminate serial workers."""
        self.stop_event.set()

        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

        for writer in list(self.clients):
            self._safe_writer_close(writer)
        self.clients.clear()

        self.stop_serial_workers()

    def request_stop(self):
        """Signal all loops/workers to stop and close the listening server."""
        self.stop_event.set()
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._close_server)

    def _close_server(self):
        """Close the telnet server socket if it exists."""
        if self.server is not None:
            self.server.close()

    def _open_serial(self):
        """Open and return a configured serial handle."""
        return serial.Serial(
            self.bridge.device,
            self.bridge.baud,
            timeout=0.0 if self.args.unbuffered_serial else 1.0,
            write_timeout=1.0,
        )

    def _close_serial_handle(self, ser):
        """Best-effort close for a serial handle."""
        try:
            ser.cancel_read()
        except (AttributeError, OSError, serial.SerialException):
            pass
        try:
            ser.cancel_write()
        except (AttributeError, OSError, serial.SerialException):
            pass
        try:
            ser.close()
        except (OSError, serial.SerialException):
            pass

    def _disconnect_serial(self, reason=None):
        """Detach current serial handle and optionally notify on loss."""
        with self.serial_state.serial_lock:
            ser = self.serial_state.ser
            self.serial_state.ser = None

        if ser is None:
            return

        if reason:
            self.serial_state.serial_was_lost = True
            logging.warning(
                "%s: serial disconnected (%s). Reconnecting every %.1fs...",
                self.bridge.device,
                reason,
                self.args.serial_reconnect_delay,
            )
            self._notify_clients(SERIAL_LOST_NOTICE)

        self._close_serial_handle(ser)

    def _get_or_reconnect_serial(self):
        """Return an open serial handle, reconnecting until available/stop."""
        while not self.stop_event.is_set():
            with self.serial_state.serial_lock:
                current = self.serial_state.ser
                if current is not None and current.is_open:
                    return current

            try:
                opened = self._open_serial()
            except (serial.SerialException, OSError, ValueError) as exc:
                now = time.monotonic()
                if now >= self.serial_state.next_reconnect_log_at:
                    logging.warning(
                        "%s: serial unavailable (%s). Retrying in %.1fs...",
                        self.bridge.device,
                        exc,
                        self.args.serial_reconnect_delay,
                    )
                    self.serial_state.next_reconnect_log_at = now + 5.0
                time.sleep(self.args.serial_reconnect_delay)
                continue

            with self.serial_state.serial_lock:
                if self.serial_state.ser is None:
                    self.serial_state.ser = opened
                    self.serial_state.next_reconnect_log_at = 0.0
                    logging.info(
                        "Serial connected: %s @ %d (%s mode)",
                        self.bridge.device,
                        self.bridge.baud,
                        "unbuffered" if self.args.unbuffered_serial else "buffered",
                    )
                    if self.serial_state.serial_was_lost:
                        self.serial_state.serial_was_lost = False
                        self._notify_clients(SERIAL_RECONNECTED_NOTICE)
                    return opened

            self._close_serial_handle(opened)

        return None

    def start_serial_workers(self):
        """Start serial RX and TX worker threads."""
        self.serial_state.read_thread = threading.Thread(
            target=self.serial_read_worker, daemon=True
        )
        self.serial_state.write_thread = threading.Thread(
            target=self.serial_write_worker, daemon=True
        )
        self.serial_state.read_thread.start()
        self.serial_state.write_thread.start()

    def stop_serial_workers(self):
        """Stop serial workers and wait briefly for thread exit."""
        self.stop_event.set()

        try:
            self.serial_write_queue.put_nowait(None)
        except queue.Full:
            pass

        self._disconnect_serial()

        if self.serial_state.read_thread is not None:
            self.serial_state.read_thread.join(timeout=2.0)
        if self.serial_state.write_thread is not None:
            self.serial_state.write_thread.join(timeout=2.0)

    def serial_read_worker(self):
        """Read from serial and broadcast payload to all connected clients."""
        while not self.stop_event.is_set():
            ser = self._get_or_reconnect_serial()
            if ser is None:
                return

            try:
                if self.args.unbuffered_serial:
                    first = ser.read(1)
                    if not first:
                        time.sleep(0.001)
                        continue
                    waiting = ser.in_waiting
                    data = first + (ser.read(waiting) if waiting else b"")
                else:
                    data = ser.read(self.args.chunk_size)
            except (serial.SerialException, OSError) as exc:
                self._disconnect_serial(reason=f"read error: {exc}")
                time.sleep(self.args.serial_reconnect_delay)
                continue

            if data and self.loop is not None:
                self.loop.call_soon_threadsafe(self.broadcast_to_clients, data)

    def serial_write_worker(self):
        """Write client payloads to serial, reconnecting on serial failures."""
        while not self.stop_event.is_set():
            try:
                data = self.serial_write_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            if data is None:
                return

            while not self.stop_event.is_set():
                ser = self._get_or_reconnect_serial()
                if ser is None:
                    return

                try:
                    ser.write(data)
                    if self.args.unbuffered_serial:
                        ser.flush()
                    break
                except serial.SerialTimeoutException:
                    logging.warning("Serial write timeout; dropping payload")
                    break
                except (serial.SerialException, OSError) as exc:
                    self._disconnect_serial(reason=f"write error: {exc}")
                    time.sleep(self.args.serial_reconnect_delay)

    def broadcast_to_clients(self, data):
        """Send bytes to every connected client and drop dead connections."""
        dead_clients = []
        for writer in list(self.clients):
            if writer.is_closing():
                dead_clients.append(writer)
                continue
            try:
                writer.write(data)
            except (ConnectionError, OSError, RuntimeError):
                dead_clients.append(writer)

        for writer in dead_clients:
            self.clients.discard(writer)
            self._safe_writer_close(writer)

    def _notify_clients(self, message):
        """Schedule a lightweight in-band status message to all clients."""
        if self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self.broadcast_to_clients, message)
        except RuntimeError:
            # Event loop may already be shutting down.
            pass

    def _safe_writer_close(self, writer):
        """Best-effort close for a Telnet writer."""
        try:
            writer.close()
        except (ConnectionError, OSError, RuntimeError):
            pass

    async def _safe_writer_close_wait(self, writer):
        """Best-effort close + wait-closed for a Telnet writer."""
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError, RuntimeError):
            pass

    async def shell(self, reader, writer):
        """Handle one Telnet client session."""
        peer = writer.get_extra_info("peername")
        self.clients.add(writer)
        logging.info("%s: client connected: %s", self.bridge.device, format_peer(peer))

        with self.serial_state.serial_lock:
            serial_up = (
                self.serial_state.ser is not None and self.serial_state.ser.is_open
            )
        if not serial_up:
            writer.write(SERIAL_LOST_NOTICE)
            try:
                await writer.drain()
            except (ConnectionError, OSError, RuntimeError):
                pass

        try:
            while not self.stop_event.is_set() and not writer.is_closing():
                data = await reader.read(self.args.chunk_size)
                if not data:
                    break
                if isinstance(data, str):
                    data = data.encode("utf-8", errors="replace")

                try:
                    self.serial_write_queue.put_nowait(data)
                except queue.Full:
                    logging.warning(
                        "%s: serial write queue full; disconnecting %s",
                        self.bridge.device,
                        format_peer(peer),
                    )
                    break
        finally:
            self.clients.discard(writer)
            await self._safe_writer_close_wait(writer)
            logging.info("%s: client disconnected: %s", self.bridge.device, format_peer(peer))


async def run_bridges(repeaters):
    """Run all configured bridges until one stops or startup fails."""
    await asyncio.gather(*(repeater.run() for repeater in repeaters))


def main():
    """Program entrypoint."""
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    repeaters = [SerialTelnetRepeater(args, bridge) for bridge in args.bridges]

    def handle_signal(_signum, _frame):
        """Signal callback: request a clean asynchronous shutdown."""
        for repeater in repeaters:
            repeater.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        asyncio.run(run_bridges(repeaters))
    except KeyboardInterrupt:
        pass
    except serial.SerialException as exc:
        logging.error("Failed to open/configure serial port: %s", exc)
    except OSError as exc:
        logging.error("Socket error: %s", exc)


if __name__ == "__main__":
    main()
