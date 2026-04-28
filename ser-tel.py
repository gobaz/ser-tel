#!/usr/bin/env python3

import argparse
import asyncio
import ipaddress
import logging
import queue
import signal
import threading
import time
from typing import Optional

import serial
import telnetlib3

SERIAL_LOST_NOTICE = b"\r\n[serial] lost\r\n"
SERIAL_RECONNECTED_NOTICE = b"\r\n[serial] reconnected\r\n"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Share one serial port with multiple telnet clients."
    )
    parser.add_argument("--serial", default="/dev/ttyUSB0", help="Serial device path.")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: loopback only for safety).",
    )
    parser.add_argument("--port", type=int, default=2000, help="TCP bind port.")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow binding to a non-loopback host/address.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=1024,
        help="Read/write chunk size in bytes.",
    )
    parser.add_argument(
        "--serial-write-queue-size",
        type=int,
        default=1024,
        help="Queue depth for client->serial data.",
    )
    parser.add_argument(
        "--serial-reconnect-delay",
        type=float,
        default=1.0,
        help="Seconds between serial reconnect attempts after disconnect/open failure.",
    )
    parser.add_argument(
        "--unbuffered-serial",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Minimize serial buffering/latency (default: enabled). "
            "Use --no-unbuffered-serial to disable."
        ),
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log verbosity.",
    )
    args = parser.parse_args()

    if not args.allow_remote and not is_loopback_host(args.host):
        parser.error(
            "Refusing non-loopback bind without --allow-remote. "
            "Use --allow-remote only on trusted networks."
        )
    if args.chunk_size <= 0:
        parser.error("--chunk-size must be > 0")
    if args.serial_write_queue_size <= 0:
        parser.error("--serial-write-queue-size must be > 0")
    if args.serial_reconnect_delay <= 0:
        parser.error("--serial-reconnect-delay must be > 0")

    return args


def is_loopback_host(host):
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def format_peer(peername):
    if isinstance(peername, tuple) and len(peername) >= 2:
        return f"{peername[0]}:{peername[1]}"
    return str(peername)


class SerialTelnetRepeater:
    def __init__(self, args):
        self.args = args
        self.stop_event = threading.Event()
        self.serial_write_queue = queue.Queue(maxsize=args.serial_write_queue_size)
        self.clients = set()

        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server = None
        self.ser: Optional[serial.Serial] = None
        self.serial_lock = threading.Lock()
        self._next_reconnect_log_at = 0.0
        self._serial_was_lost = False
        self.read_thread: Optional[threading.Thread] = None
        self.write_thread: Optional[threading.Thread] = None

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.start_serial_workers()

        self.server = await telnetlib3.create_server(
            host=self.args.host,
            port=self.args.port,
            shell=self.shell,
            encoding=False,
            line_mode=False,
            timeout=False,
        )
        logging.info("Listening on %s:%d", self.args.host, self.args.port)

        try:
            while not self.stop_event.is_set():
                await asyncio.sleep(0.2)
        finally:
            await self.shutdown()

    async def shutdown(self):
        self.stop_event.set()

        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

        for writer in list(self.clients):
            try:
                writer.close()
            except Exception:
                pass
        self.clients.clear()

        self.stop_serial_workers()

    def request_stop(self):
        self.stop_event.set()
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._close_server)

    def _close_server(self):
        if self.server is not None:
            self.server.close()

    def _open_serial(self):
        return serial.Serial(
            self.args.serial,
            self.args.baud,
            timeout=0.0 if self.args.unbuffered_serial else 1.0,
            write_timeout=1.0,
        )

    def _close_serial_handle(self, ser):
        try:
            ser.cancel_read()
        except Exception:
            pass
        try:
            ser.cancel_write()
        except Exception:
            pass
        try:
            ser.close()
        except Exception:
            pass

    def _disconnect_serial(self, reason=None):
        with self.serial_lock:
            ser = self.ser
            self.ser = None

        if ser is None:
            return

        if reason:
            self._serial_was_lost = True
            logging.warning(
                "Serial disconnected (%s). Reconnecting every %.1fs...",
                reason,
                self.args.serial_reconnect_delay,
            )
            self._notify_clients(SERIAL_LOST_NOTICE)

        self._close_serial_handle(ser)

    def _get_or_reconnect_serial(self):
        while not self.stop_event.is_set():
            with self.serial_lock:
                current = self.ser
                if current is not None and current.is_open:
                    return current

            try:
                opened = self._open_serial()
            except (serial.SerialException, OSError, ValueError) as exc:
                now = time.monotonic()
                if now >= self._next_reconnect_log_at:
                    logging.warning(
                        "Serial unavailable (%s). Retrying in %.1fs...",
                        exc,
                        self.args.serial_reconnect_delay,
                    )
                    self._next_reconnect_log_at = now + 5.0
                time.sleep(self.args.serial_reconnect_delay)
                continue

            with self.serial_lock:
                if self.ser is None:
                    self.ser = opened
                    self._next_reconnect_log_at = 0.0
                    logging.info(
                        "Serial connected: %s @ %d (%s mode)",
                        self.args.serial,
                        self.args.baud,
                        "unbuffered" if self.args.unbuffered_serial else "buffered",
                    )
                    if self._serial_was_lost:
                        self._serial_was_lost = False
                        self._notify_clients(SERIAL_RECONNECTED_NOTICE)
                    return opened

            self._close_serial_handle(opened)

        return None

    def start_serial_workers(self):
        self.read_thread = threading.Thread(target=self.serial_read_worker, daemon=True)
        self.write_thread = threading.Thread(target=self.serial_write_worker, daemon=True)
        self.read_thread.start()
        self.write_thread.start()

    def stop_serial_workers(self):
        self.stop_event.set()

        try:
            self.serial_write_queue.put_nowait(None)
        except queue.Full:
            pass

        self._disconnect_serial()

        if self.read_thread is not None:
            self.read_thread.join(timeout=2.0)
        if self.write_thread is not None:
            self.write_thread.join(timeout=2.0)

    def serial_read_worker(self):
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
        dead_clients = []
        for writer in list(self.clients):
            if writer.is_closing():
                dead_clients.append(writer)
                continue
            try:
                writer.write(data)
            except Exception:
                dead_clients.append(writer)

        for writer in dead_clients:
            self.clients.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    def _notify_clients(self, message):
        if self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self.broadcast_to_clients, message)
        except RuntimeError:
            # Event loop may already be shutting down.
            pass

    async def shell(self, reader, writer):
        peer = writer.get_extra_info("peername")
        self.clients.add(writer)
        logging.info("Client connected: %s", format_peer(peer))

        with self.serial_lock:
            serial_up = self.ser is not None and self.ser.is_open
        if not serial_up:
            writer.write(SERIAL_LOST_NOTICE)
            try:
                await writer.drain()
            except Exception:
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
                    logging.warning("Serial write queue full; disconnecting %s", format_peer(peer))
                    break
        finally:
            self.clients.discard(writer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logging.info("Client disconnected: %s", format_peer(peer))


def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    repeater = SerialTelnetRepeater(args)

    def handle_signal(_signum, _frame):
        repeater.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        asyncio.run(repeater.run())
    except KeyboardInterrupt:
        pass
    except serial.SerialException as exc:
        logging.error("Failed to open/configure serial port: %s", exc)
    except OSError as exc:
        logging.error("Socket error: %s", exc)


if __name__ == "__main__":
    main()
