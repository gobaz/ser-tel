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
        self.read_thread: Optional[threading.Thread] = None
        self.write_thread: Optional[threading.Thread] = None

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.open_serial()
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

    def open_serial(self):
        self.ser = serial.Serial(
            self.args.serial,
            self.args.baud,
            timeout=0.0 if self.args.unbuffered_serial else 1.0,
            write_timeout=1.0,
        )
        logging.info(
            "Opened serial %s @ %d (%s mode)",
            self.args.serial,
            self.args.baud,
            "unbuffered" if self.args.unbuffered_serial else "buffered",
        )

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

        if self.ser is not None:
            try:
                self.ser.cancel_read()
            except Exception:
                pass
            try:
                self.ser.cancel_write()
            except Exception:
                pass

        if self.read_thread is not None:
            self.read_thread.join(timeout=2.0)
        if self.write_thread is not None:
            self.write_thread.join(timeout=2.0)

        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass

    def serial_read_worker(self):
        assert self.ser is not None
        while not self.stop_event.is_set():
            try:
                if self.args.unbuffered_serial:
                    first = self.ser.read(1)
                    if not first:
                        time.sleep(0.001)
                        continue
                    waiting = self.ser.in_waiting
                    data = first + (self.ser.read(waiting) if waiting else b"")
                else:
                    data = self.ser.read(self.args.chunk_size)
            except serial.SerialException as exc:
                logging.error("Serial read failed: %s", exc)
                self.request_stop()
                return

            if data and self.loop is not None:
                self.loop.call_soon_threadsafe(self.broadcast_to_clients, data)

    def serial_write_worker(self):
        assert self.ser is not None
        while not self.stop_event.is_set():
            try:
                data = self.serial_write_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            if data is None:
                return

            try:
                self.ser.write(data)
                if self.args.unbuffered_serial:
                    self.ser.flush()
            except serial.SerialTimeoutException:
                logging.warning("Serial write timeout; dropping payload")
            except serial.SerialException as exc:
                logging.error("Serial write failed: %s", exc)
                self.request_stop()
                return

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

    async def shell(self, reader, writer):
        peer = writer.get_extra_info("peername")
        self.clients.add(writer)
        logging.info("Client connected: %s", format_peer(peer))

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
