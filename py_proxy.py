#!/usr/bin/env python3

import argparse
import asyncio
import logging
import os
import sys
import ssl
import socket
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
import time
import traceback
import random

__version__ = "2.1"

# Enable ANSI colors on Windows terminals
os.system("")

if sys.platform == "win32":
    import winreg


@dataclass
class ConnectionInfo:
    src_ip: str
    dst_domain: str
    method: str
    start_time: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    traffic_in: int = 0
    traffic_out: int = 0


@dataclass
class IdleState:
    last_activity: float = field(default_factory=time.monotonic)


class ProxyServer:

    def __init__(self, host, port, blacklist, log_access, log_err, no_blacklist, auto_blacklist, quiet, verbose, idle_timeout=0):
        self.host = host
        self.port = port
        self.blacklist = blacklist
        self.log_access_file = log_access
        self.log_err_file = log_err
        self.no_blacklist = no_blacklist
        self.auto_blacklist = auto_blacklist
        self.quiet = quiet
        self.verbose = verbose
        self.idle_timeout = idle_timeout  # seconds; 0 disables

        self.logger = logging.getLogger(__name__)

        # Stats
        self.total_connections = 0
        self.allowed_connections = 0
        self.blocked_connections = 0
        self.traffic_in = 0
        self.traffic_out = 0
        self.last_traffic_in = 0
        self.last_traffic_out = 0
        self.speed_in = 0
        self.speed_out = 0
        self.last_time = None

        # Performance tuning
        self.bufsize = 65536             # 64 KiB read chunks
        self.high_water = 512 * 1024     # 512 KiB drain threshold

        # Filtering state
        self.blocked = []                # list[str]
        self.blocked_bytes = []          # list[bytes]
        self.whitelist = set()           # set[bytes]
        self._blacklist_write_lock = asyncio.Lock()

        self.server = None

        self.setup_logging()
        self.load_blacklist()

    def print(self, *args, **kwargs):
        if not self.quiet:
            print(*args, **kwargs)

    def setup_logging(self):
        # Prepare log handlers
        handlers = []

        if self.log_err_file:
            h_err = logging.FileHandler(self.log_err_file, encoding='utf-8')
            h_err.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s]: %(message)s", "%Y-%m-%d %H:%M:%S"))
            h_err.setLevel(logging.ERROR)
            h_err.addFilter(lambda record: record.levelno == logging.ERROR)
            handlers.append(h_err)

        if self.log_access_file:
            h_acc = logging.FileHandler(self.log_access_file, encoding='utf-8')
            h_acc.setFormatter(logging.Formatter("%(message)s"))
            h_acc.setLevel(logging.INFO)
            h_acc.addFilter(lambda record: record.levelno == logging.INFO)
            handlers.append(h_acc)

        # Apply handlers
        self.logger.handlers = []
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO if self.log_access_file else logging.ERROR)
        for h in handlers:
            self.logger.addHandler(h)

    def load_blacklist(self):
        if self.no_blacklist or self.auto_blacklist:
            return
        if not os.path.exists(self.blacklist):
            self.print(f"\033[91m[ERROR]: File {self.blacklist} not found\033[0m")
            self.logger.error("File %s not found", self.blacklist)
            sys.exit(1)

        with open(self.blacklist, "r", encoding="utf-8") as f:
            domains = [line.strip() for line in f if line.strip()]
        self.blocked = domains
        self.blocked_bytes = [d.encode("utf-8", "ignore") for d in domains]

    async def run(self):
        self.print_banner()
        if not self.quiet:
            asyncio.create_task(self.display_stats())
        try:
            kwargs = dict(backlog=1024, limit=1 << 20)
            if sys.platform != "win32":
                kwargs["reuse_port"] = True
            self.server = await asyncio.start_server(self.handle_connection, self.host, self.port, **kwargs)
        except Exception:
            self.print(f"\033[91m[ERROR]: Failed to start proxy on this address ({self.host}:{self.port}). It looks like the port is already in use\033[0m")
            self.logger.error("Port %s is already in use", self.port)
            sys.exit(1)

        await self.server.serve_forever()

    def print_banner(self):
        self.print("=====PROXY=====")
        self.print(f"\033[92mVersion: {__version__}".center(50))
        self.print("\033[97m" + "Enjoy watching!".center(50))
        self.print(f"Proxy is running on {self.host}:{self.port}".center(50))
        self.print("\n")
        self.print(f"\033[92m[INFO]:\033[97m Proxy started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if not self.no_blacklist and not self.auto_blacklist:
            self.print(f"\033[92m[INFO]:\033[97m Blacklist contains {len(self.blocked)} domains")
        self.print("\033[92m[INFO]:\033[97m To stop the proxy, press Ctrl+C twice")
        if self.log_err_file:
            self.print("\033[92m[INFO]:\033[97m Logging is in progress. You can see the list of errors in the file " f"{self.log_err_file}")

    async def display_stats(self):
        while True:
            await asyncio.sleep(1)
            current_time = time.time()

            if self.last_time is not None:
                time_diff = current_time - self.last_time
                if time_diff > 0:
                    self.speed_in = (self.traffic_in - self.last_traffic_in) * 8 / time_diff
                    self.speed_out = (self.traffic_out - self.last_traffic_out) * 8 / time_diff

            self.last_traffic_in = self.traffic_in
            self.last_traffic_out = self.traffic_out
            self.last_time = current_time

            stats = (
                f"\033[92m[STATS]:\033[0m "
                f"\033[97mConns: \033[93m{self.total_connections}\033[0m | "
                f"\033[97mMiss: \033[92m{self.allowed_connections}\033[0m | "
                f"\033[97mUnblock: \033[91m{self.blocked_connections}\033[0m | "
                f"\033[97mDL: \033[96m{self.format_size(self.traffic_in)}\033[0m | "
                f"\033[97mUL: \033[96m{self.format_size(self.traffic_out)}\033[0m | "
                f"\033[97mSpeed DL: \033[96m{self.format_speed(self.speed_in)}\033[0m | "
                f"\033[97mSpeed UL: \033[96m{self.format_speed(self.speed_out)}\033[0m"
            )
            self.print("\u001b[2K" + stats, end="\r", flush=True)

    @staticmethod
    def format_size(size):
        units = ["B", "KB", "MB", "GB", "TB"]
        unit = 0
        size = float(size)
        while size >= 1024 and unit < len(units) - 1:
            size /= 1024
            unit += 1
        return f"{size:.1f} {units[unit]}"

    @staticmethod
    def format_speed(speed_bps):
        units = ["bps", "Kbps", "Mbps", "Gbps"]
        unit = 0
        speed = float(speed_bps)
        while speed >= 1000 and unit < len(units) - 1:
            speed /= 1000
            unit += 1
        return f"{speed:.1f} {units[unit]}"

    @staticmethod
    def _tune_socket(writer):
        try:
            sock = writer.get_extra_info("socket")
            if not isinstance(sock, socket.socket):
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except Exception:
            pass

    async def _probe_and_maybe_blacklist(self, host_bytes: bytes):
        # Avoid repeated probes
        if host_bytes in self.whitelist:
            return
        if any((host_bytes == b if isinstance(b, bytes) else host_bytes == b.encode()) for b in self.blocked):
            return

        host = host_bytes.decode("idna", "ignore") or host_bytes.decode("utf-8", "ignore") or ""
        if not host:
            return

        try:
            ctx = ssl.create_default_context()
            # If handshake succeeds quickly -> whitelist
            await asyncio.wait_for(asyncio.open_connection(host, 443, ssl=ctx), timeout=3.5)
            self.whitelist.add(host_bytes)
        except Exception:
            # Treat timeouts/handshake failures as blocked
            self.blocked.append(host)
            self.blocked_bytes.append(host_bytes)
            async with self._blacklist_write_lock:
                try:
                    with open(self.blacklist, "a", encoding="utf-8") as f:
                        f.write(host + "\n")
                except Exception:
                    pass

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        client = writer.get_extra_info("peername")
        client_ip, client_port = (client or ("unknown", 0))[:2]

        # Tune client socket
        self._tune_socket(writer)

        conn_info = None
        try:
            http_data = await reader.read(65536)
            if not http_data:
                writer.close()
                await writer.wait_closed()
                return

            headers = http_data.split(b"\r\n")
            first_line = headers[0].split(b" ")
            method = first_line[0] if first_line else b""
            url = first_line[1] if len(first_line) > 1 else b""

            if method == b"CONNECT":
                host_port = url.split(b":", 1)
                host_b = host_port[0].strip()
                try:
                    port = int(host_port[1]) if len(host_port) > 1 else 443
                except Exception:
                    port = 443
            else:
                host_header = next((h for h in headers if h.lower().startswith(b"host: ")), None)
                if not host_header:
                    raise ValueError("Missing Host header")
                host_value = host_header.split(b":", 1)[1].strip()  # after "Host:"
                host_port = host_value.split(b":", 1)
                host_b = host_port[0].strip()
                try:
                    port = int(host_port[1]) if len(host_port) > 1 else 80
                except Exception:
                    port = 80

            # Decode host for both branches (CONNECT and HTTP)
            try:
                host_str = host_b.decode("utf-8", "ignore")
                host_str = host_str.encode("idna").decode("ascii")
            except Exception:
                host_str = ""
            conn_info = ConnectionInfo(src_ip=str(client_ip), dst_domain=host_str, method=method.decode(errors="ignore") or "?")

            # Background probe for auto-blacklist
            if method == b"CONNECT" and self.auto_blacklist and host_b not in self.whitelist:
                asyncio.create_task(self._probe_and_maybe_blacklist(host_b))

            if method == b"CONNECT":
                # Establish tunnel
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()

                try:
                    remote_reader, remote_writer = await asyncio.wait_for(asyncio.open_connection(host_str, port), timeout=5)
                    self._tune_socket(remote_writer)
                except Exception:
                    self.logger.error("%s: %s", host_str, traceback.format_exc())
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass
                    return

                # First TLS record (possibly fragmented) goes out
                await self.fragment_data(reader, remote_writer)

                self.total_connections += 1

                # Pipe both directions with a connection-wide idle watchdog
                idle = IdleState()
                idle.last_activity = time.monotonic()
                t1 = asyncio.create_task(self.pipe(reader, remote_writer, "out", conn_info, idle))
                t2 = asyncio.create_task(self.pipe(remote_reader, writer, "in", conn_info, idle))
                wd = asyncio.create_task(self.idle_watchdog(idle, writer, remote_writer))
                try:
                    await asyncio.gather(t1, t2)
                finally:
                    wd.cancel()
                    with contextlib.suppress(Exception):
                        await wd

            else:
                # HTTP proxying
                try:
                    remote_reader, remote_writer = await asyncio.wait_for(asyncio.open_connection(host_str, port), timeout=5)
                    self._tune_socket(remote_writer)
                except Exception:
                    try:
                        writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                        await writer.drain()
                    except Exception:
                        pass
                    self.logger.error("%s: %s", host_str, traceback.format_exc())
                    writer.close()
                    await writer.wait_closed()
                    return

                # Forward initial request
                remote_writer.write(http_data)
                await remote_writer.drain()
                self.allowed_connections += 1
                self.total_connections += 1

                idle = IdleState()
                idle.last_activity = time.monotonic()
                t1 = asyncio.create_task(self.pipe(reader, remote_writer, "out", conn_info, idle))
                t2 = asyncio.create_task(self.pipe(remote_reader, writer, "in", conn_info, idle))
                wd = asyncio.create_task(self.idle_watchdog(idle, writer, remote_writer))
                try:
                    await asyncio.gather(t1, t2)
                finally:
                    wd.cancel()
                    with contextlib.suppress(Exception):
                        await wd

        except Exception as e:
            try:
                writer.write(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
            self.logger.error("handle_connection: %s", traceback.format_exc())
            if self.verbose:
                self.print(f"\033[93m[DEBUG]:\033[97m {e}\033[0m")
        finally:
            # One access log per connection
            try:
                if conn_info:
                    self.logger.info("%s %s %s %s", conn_info.start_time, conn_info.src_ip, conn_info.method, conn_info.dst_domain)
            except Exception:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, direction: str, conn_info: ConnectionInfo, idle: IdleState = None):
        try:
            transport = writer.transport if hasattr(writer, "transport") else None
            while True:
                data = await reader.read(self.bufsize)
                if not data:
                    break

                if idle is not None:
                    idle.last_activity = time.monotonic()

                if direction == "out":
                    self.traffic_out += len(data)
                    conn_info.traffic_out += len(data)
                else:
                    self.traffic_in += len(data)
                    conn_info.traffic_in += len(data)

                writer.write(data)
                if transport and transport.get_write_buffer_size() > self.high_water:
                    await writer.drain()

        except (ConnectionResetError, asyncio.IncompleteReadError, ConnectionAbortedError, BrokenPipeError) as e:
            if self.verbose and conn_info:
                self.print(f"\033[93m[INFO]: Connection closed: {conn_info.dst_domain} [{e}]\033[0m")
        except Exception as e:
            if conn_info:
                self.logger.error("%s: %s", conn_info.dst_domain, traceback.format_exc())
            if self.verbose and conn_info:
                self.print(f"\033[93m[DEBUG]:\033[97m {conn_info.dst_domain}: {e}\033[0m")
        finally:
            try:
                writer.write_eof()
            except Exception:
                pass
            try:
                await writer.drain()
            except Exception:
                pass
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def fragment_data(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            if self.idle_timeout and self.idle_timeout > 0:
                head = await asyncio.wait_for(reader.readexactly(5), timeout=self.idle_timeout)
            else:
                head = await reader.readexactly(5)
        except asyncio.IncompleteReadError:
            if self.verbose:
                self.print("\033[93m[DEBUG]:\033[97m IncompleteReadError: TLS record incomplete, client may have closed the connection early.\033[0m")
            return
        except asyncio.TimeoutError:
            if self.verbose:
                self.print(f"\033[93m[INFO]: Idle timeout waiting for first TLS record; closing tunnel\033[0m")
            return
        except Exception as e:
            self.logger.error(traceback.format_exc())
            if self.verbose:
                self.print(f"\033[93m[DEBUG]:\033[97m Unexpected error in fragment_data: {e}\033[0m")
            return

        if not head or head[0] != 0x16:
            try:
                rest = await reader.read(self.bufsize)
            except Exception:
                rest = b''
            writer.write(head + rest)
            await writer.drain()
            return

        rec_len = int.from_bytes(head[3:5], "big")
        try:
            body = await reader.readexactly(rec_len)
        except Exception as e:
            if self.verbose:
                self.print("\033[93m[DEBUG]:\033[97m Failed to read full TLS record body\033[0m")
            return

        # Otherwise, fragment into a few records to confuse DPI but keep speed
        self.blocked_connections += 1

        # Heuristic: split at first 0x00 if present; else, split into 2-3 chunks
        def frame(chunk: bytes) -> bytes:
            # Use 0x16 0x03 0x04 to mimic TLS 1.3 as in original code
            return b"\x16\x03\x04" + len(chunk).to_bytes(2, "big") + chunk

        parts = []
        host_end = body.find(b"\x00")
        if 0 <= host_end < len(body):
            cut1 = min(host_end + 1, len(body))
            chunks = [body[:cut1], body[cut1:]]
        else:
            # Split into 2-3 chunks with small first chunk
            if len(body) <= 512:
                # Keep it simple
                cut1 = max(1, len(body) // 2)
                chunks = [body[:cut1], body[cut1:]]
            else:
                # 3 chunks: small, medium, remainder
                c1 = random.randint(32, 128)
                c2 = random.randint(128, 512)
                c1 = min(c1, len(body))
                c2 = min(c1 + c2, len(body))
                chunks = [body[:c1], body[c1:c2], body[c2:]]

        for ch in chunks:
            if ch:
                parts.append(frame(ch))

        for p in parts:
            writer.write(p)
            await writer.drain()

    async def idle_watchdog(self, idle: IdleState, client_writer: asyncio.StreamWriter, remote_writer: asyncio.StreamWriter):
        if not self.idle_timeout or self.idle_timeout <= 0:
            return
        try:
            while True:
                await asyncio.sleep(1)
                if time.monotonic() - idle.last_activity > self.idle_timeout:
                    if self.verbose:
                        self.print(f"\033[93m[INFO]: Idle timeout {self.idle_timeout}s exceeded; closing connection\033[0m")
                    for w in (client_writer, remote_writer):
                        try:
                            w.close()
                        except Exception:
                            pass
                    break
        except asyncio.CancelledError:
            pass

    async def shutdown(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()


class ProxyApplication:
    @staticmethod
    def parse_args():
        parser = argparse.ArgumentParser()
        parser.add_argument("--host", default="127.0.0.1", help="Proxy host")
        parser.add_argument("--port", type=int, default=8881, help="Proxy port")
        parser.add_argument("--idle-timeout", type=int, default=300,
                            help="Close connections after N seconds of inactivity (0 to disable)")

        blacklist_group = parser.add_mutually_exclusive_group()
        blacklist_group.add_argument("--blacklist", default="blacklist.txt", help="Path to blacklist file")
        blacklist_group.add_argument("--no_blacklist", action="store_true", help="Use fragmentation for all domains")
        blacklist_group.add_argument("--autoblacklist", action="store_true", help="Automatic detection of blocked domains")

        parser.add_argument("--log_access", required=False, help="Path to the access control log")
        parser.add_argument("--log_error", required=False, help="Path to log file for errors")
        parser.add_argument("-q", "--quiet", action="store_true", help="Remove UI output")
        parser.add_argument("-v", "--verbose", action="store_true", help="Show more info (only for devs)")

        autostart_group = parser.add_mutually_exclusive_group()
        autostart_group.add_argument("--install", action="store_true", help="Add proxy to Windows autostart (only for EXE)")
        autostart_group.add_argument("--uninstall", action="store_true", help="Remove proxy from Windows autostart (only for EXE)")

        return parser.parse_args()

    @staticmethod
    def manage_autostart(action="install"):
        if sys.platform != "win32":
            print("\033[91m[ERROR]:\033[97m Autostart only available on Windows")
            return

        app_name = "NoDPIProxy"
        exe_path = sys.executable

        try:
            key = winreg.HKEY_CURRENT_USER
            reg_path = r"Software\Microsoft\Windows\CurrentVersion\Run"

            if action == "install":
                with winreg.OpenKey(key, reg_path, 0, winreg.KEY_WRITE) as regkey:
                    winreg.SetValueEx(
                        regkey,
                        app_name,
                        0,
                        winreg.REG_SZ,
                        f'"{exe_path}" --blacklist "{os.path.dirname(exe_path)}/blacklist.txt"',
                    )
                print(f"\033[92m[INFO]:\033[97m Added to autostart: {exe_path}")

            elif action == "uninstall":
                try:
                    with winreg.OpenKey(key, reg_path, 0, winreg.KEY_WRITE) as regkey:
                        winreg.DeleteValue(regkey, app_name)
                    print("\033[92m[INFO]:\033[97m Removed from autostart")
                except FileNotFoundError:
                    print("\033[91m[ERROR]: Not found in autostart\033[0m")

        except PermissionError:
            print("\033[91m[ERROR]: Access denied. Run as administrator\033[0m")
        except Exception as e:
            print(f"\033[91m[ERROR]: Autostart operation failed: {e}\033[0m")

    @classmethod
    async def run(cls):
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        args = cls.parse_args()

        if args.install or args.uninstall:
            if getattr(sys, 'frozen', False):
                if args.install:
                    cls.manage_autostart("install")
                elif args.uninstall:
                    cls.manage_autostart("uninstall")
                sys.exit(0)
            else:
                print("\033[91m[ERROR]: Autostart works only in EXE version\033[0m")
                sys.exit(1)

        proxy = ProxyServer(
            args.host,
            args.port,
            args.blacklist,
            args.log_access,
            args.log_error,
            args.no_blacklist,
            args.autoblacklist,
            args.quiet,
            args.verbose,
            args.idle_timeout,
        )

        try:
            await proxy.run()
        except asyncio.CancelledError:
            await proxy.shutdown()
            proxy.print("\n\n\033[92m[INFO]:\033[97m Shutting down proxy...")
            try:
                sys.exit(0)
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    try:
        #import uvloop
        #uvloop.install()
        asyncio.run(ProxyApplication.run())
    except KeyboardInterrupt:
        pass