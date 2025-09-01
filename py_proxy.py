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
import signal
import errno

__version__ = "2.4"


# Custom Exceptions
class ProxyException(Exception):
    """Base exception for proxy errors"""
    pass

class ConnectionException(ProxyException):
    """Connection-related exceptions"""
    pass

class BlacklistException(ProxyException):
    """Blacklist-related exceptions"""
    pass

class WhitelistException(ProxyException):
    """Whitelist-related exceptions"""
    pass

class FragmentationException(ProxyException):
    """Fragmentation-related exceptions"""
    pass


@dataclass
class ConnectionInfo:
    src_ip: str
    dst_domain: str
    method: str
    start_time: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    traffic_in: int = 0
    traffic_out: int = 0
    error: str = ""
    whitelisted: bool = False
    fragmented: bool = False


@dataclass
class IdleState:
    last_activity: float = field(default_factory=time.monotonic)


class ProxyServer:

    def __init__(self, host, port, blacklist, whitelist, log_access, log_err, 
                 auto_blacklist, quiet, verbose, idle_timeout=0):
        self.host = host
        self.port = port
        self.blacklist = blacklist
        self.whitelist_file = whitelist
        self.log_access_file = log_access
        self.log_err_file = log_err
        self.auto_blacklist = auto_blacklist
        self.quiet = quiet
        self.verbose = verbose
        self.idle_timeout = idle_timeout

        self.logger = logging.getLogger(__name__)

        # Stats
        self.total_connections = 0
        self.allowed_connections = 0
        self.blocked_connections = 0
        self.whitelisted_connections = 0
        self.failed_connections = 0
        self.traffic_in = 0
        self.traffic_out = 0
        self.last_traffic_in = 0
        self.last_traffic_out = 0
        self.speed_in = 0
        self.speed_out = 0
        self.last_time = None

        # Performance tuning
        self.bufsize = 65536
        self.high_water = 512 * 1024

        # Filtering state
        self.blocked = []
        self.blocked_bytes = []
        self.whitelist = set()
        self.whitelist_bytes = set()
        self._blacklist_write_lock = asyncio.Lock()
        self._whitelist_write_lock = asyncio.Lock()

        self.server = None
        self.active_connections = {}
        self.shutdown_event = asyncio.Event()
        self.stats_task = None
        self._running = False
        self._shutdown_complete = asyncio.Event()

        self.setup_logging()
        self.load_blacklist()
        self.load_whitelist()

    def print(self, *args, **kwargs):
        if not self.quiet:
            print(*args, **kwargs)

    def setup_logging(self):
        handlers = []

        try:
            if self.log_err_file:
                h_err = logging.FileHandler(self.log_err_file, encoding='utf-8')
                h_err.setFormatter(logging.Formatter("[%(asctime)s][%(levelname)s]: %(message)s", "%Y-%m-%d %H:%M:%S"))
                h_err.setLevel(logging.ERROR)
                h_err.addFilter(lambda record: record.levelno == logging.ERROR)
                handlers.append(h_err)
        except (OSError, IOError) as e:
            self.print(f"\033[93m[WARN]: Failed to create error log file: {e}\033[0m")

        try:
            if self.log_access_file:
                h_acc = logging.FileHandler(self.log_access_file, encoding='utf-8')
                h_acc.setFormatter(logging.Formatter("%(message)s"))
                h_acc.setLevel(logging.INFO)
                h_acc.addFilter(lambda record: record.levelno == logging.INFO)
                handlers.append(h_acc)
        except (OSError, IOError) as e:
            self.print(f"\033[93m[WARN]: Failed to create access log file: {e}\033[0m")

        self.logger.handlers = []
        self.logger.propagate = False
        self.logger.setLevel(logging.INFO if self.log_access_file else logging.ERROR)
        for h in handlers:
            self.logger.addHandler(h)

    def load_blacklist(self):
        if self.auto_blacklist:
            return
            
        try:
            if not os.path.exists(self.blacklist):
                raise BlacklistException(f"File {self.blacklist} not found")
                
            with open(self.blacklist, "r", encoding="utf-8") as f:
                domains = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            self.blocked = domains
            self.blocked_bytes = [d.encode("utf-8", "ignore") for d in domains]
            
            if not self.quiet:
                self.print(f"\033[92m[INFO]:\033[97m Loaded {len(self.blocked)} domains from blacklist")
            
        except BlacklistException as e:
            self.print(f"\033[91m[ERROR]: {e}\033[0m")
            self.logger.error(str(e))
            sys.exit(1)
        except (OSError, IOError) as e:
            self.print(f"\033[91m[ERROR]: Failed to read blacklist: {e}\033[0m")
            self.logger.error("Failed to read blacklist: %s", e)
            sys.exit(1)
        except UnicodeDecodeError as e:
            self.print(f"\033[91m[ERROR]: Invalid encoding in blacklist file: {e}\033[0m")
            self.logger.error("Invalid encoding in blacklist: %s", e)
            sys.exit(1)

    def load_whitelist(self):
        if not self.whitelist_file:
            return
            
        try:
            if not os.path.exists(self.whitelist_file):
                self.print(f"\033[93m[WARN]: Whitelist file {self.whitelist_file} not found, creating empty file\033[0m")
                # Create empty whitelist file
                try:
                    with open(self.whitelist_file, "w", encoding="utf-8") as f:
                        f.write("# Whitelist - domains that should never be fragmented\n")
                        f.write("# One domain per line\n")
                        f.write("# Lines starting with # are comments\n")
                except (OSError, IOError) as e:
                    self.print(f"\033[93m[WARN]: Failed to create whitelist file: {e}\033[0m")
                return
                
            with open(self.whitelist_file, "r", encoding="utf-8") as f:
                domains = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            
            self.whitelist = set(domains)
            self.whitelist_bytes = {d.encode("utf-8", "ignore") for d in domains}
            
            if not self.quiet and domains:
                self.print(f"\033[92m[INFO]:\033[97m Loaded {len(self.whitelist)} domains from whitelist")
            
        except (OSError, IOError) as e:
            self.print(f"\033[93m[WARN]: Failed to read whitelist: {e}\033[0m")
            self.logger.error("Failed to read whitelist: %s", e)
        except UnicodeDecodeError as e:
            self.print(f"\033[93m[WARN]: Invalid encoding in whitelist file: {e}\033[0m")
            self.logger.error("Invalid encoding in whitelist: %s", e)

    def is_whitelisted(self, domain: str, domain_bytes: bytes = None) -> bool:
        """Check if a domain is whitelisted"""
        if not self.whitelist and not self.whitelist_bytes:
            return False
            
        # Check string version
        if domain in self.whitelist:
            return True
            
        # Check bytes version
        if domain_bytes and domain_bytes in self.whitelist_bytes:
            return True
            
        # Check with wildcard support
        for whitelisted in self.whitelist:
            if whitelisted.startswith('*.'):
                # Wildcard subdomain match
                if domain.endswith(whitelisted[2:]) or domain == whitelisted[2:]:
                    return True
            elif '*' in whitelisted:
                # General wildcard match
                import fnmatch
                if fnmatch.fnmatch(domain, whitelisted):
                    return True
                    
        return False

    def is_blacklisted(self, domain: str, domain_bytes: bytes = None) -> bool:
        """Check if a domain is blacklisted"""
        # Whitelist takes precedence
        if self.is_whitelisted(domain, domain_bytes):
            return False
            
        if domain_bytes and domain_bytes in self.blocked_bytes:
            return True
            
        if domain in self.blocked:
            return True
            
        # Check with wildcard support
        for blocked in self.blocked:
            if blocked.startswith('*.'):
                # Wildcard subdomain match
                if domain.endswith(blocked[2:]) or domain == blocked[2:]:
                    return True
            elif '*' in blocked:
                # General wildcard match
                import fnmatch
                if fnmatch.fnmatch(domain, blocked):
                    return True
                    
        return False

    async def run(self):
        self._running = True
        self.print_banner()
        
        if not self.quiet:
            self.stats_task = asyncio.create_task(self.display_stats())
        
        # Set up signal handlers
        loop = asyncio.get_running_loop()
        
        def signal_handler(sig):
            if not self.shutdown_event.is_set():
                asyncio.create_task(self.shutdown(sig))
        
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass
        
        try:
            kwargs = dict(backlog=1024, limit=1 << 20, reuse_port=True)
            self.server = await asyncio.start_server(self.handle_connection, self.host, self.port, **kwargs)
            
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                self.print(f"\033[91m[ERROR]: Port {self.port} is already in use\033[0m")
                self.logger.error("Port %s is already in use", self.port)
            elif e.errno == errno.EACCES:
                self.print(f"\033[91m[ERROR]: Permission denied for port {self.port}\033[0m")
                self.logger.error("Permission denied for port %s", self.port)
            else:
                self.print(f"\033[91m[ERROR]: Failed to start proxy: {e}\033[0m")
                self.logger.error("Failed to start proxy: %s", e)
            sys.exit(1)
            
        except Exception as e:
            self.print(f"\033[91m[ERROR]: Unexpected error starting proxy: {e}\033[0m")
            self.logger.error("Unexpected error: %s", traceback.format_exc())
            sys.exit(1)

        try:
            async with self.server:
                await self.server.serve_forever()
        except asyncio.CancelledError:
            pass
        finally:
            self._running = False

    def print_banner(self):
        self.print("\n")
        self.print(f"\033[92mVersion: {__version__}".center(50))
        self.print("\033[97m" + "Enjoy watching!".center(50))
        self.print(f"Proxy is running on {self.host}:{self.port}".center(50))
        self.print("\n")
        self.print(f"\033[92m[INFO]:\033[97m Proxy started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.print(f"\033[92m[INFO]:\033[97m Fragmentation enabled for HTTPS connections")
        if not self.auto_blacklist:
            self.print(f"\033[92m[INFO]:\033[97m Blacklist contains {len(self.blocked)} domains")
        if self.whitelist:
            self.print(f"\033[92m[INFO]:\033[97m Whitelist contains {len(self.whitelist)} domains")
        self.print("\033[92m[INFO]:\033[97m To stop the proxy, press Ctrl+C")

    async def display_stats(self):
        try:
            while not self.shutdown_event.is_set():
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
                    f"\033[97mActive: \033[94m{len(self.active_connections)}\033[0m | "
                    f"\033[97mFragmented: \033[91m{self.blocked_connections}\033[0m | "
                    f"\033[97mWhitelisted: \033[92m{self.whitelisted_connections}\033[0m | "
                    f"\033[97mFailed: \033[91m{self.failed_connections}\033[0m | "
                    f"\033[97mDL: \033[96m{self.format_size(self.traffic_in)}\033[0m | "
                    f"\033[97mUL: \033[96m{self.format_size(self.traffic_out)}\033[0m | "
                    f"\033[97mSpeed DL: \033[96m{self.format_speed(self.speed_in)}\033[0m | "
                    f"\033[97mSpeed UL: \033[96m{self.format_speed(self.speed_out)}\033[0m"
                )
                self.print("\u001b[2K" + stats, end="\r", flush=True)
                
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.verbose:
                self.logger.error("Stats display error: %s", e)

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
        except OSError as e:
            pass
        except AttributeError:
            pass
        except Exception:
            pass

    @staticmethod
    def is_writer_alive(writer: asyncio.StreamWriter) -> bool:
        if writer is None:
            return False
            
        try:
            if hasattr(writer, 'is_closing') and writer.is_closing():
                return False
            transport = getattr(writer, 'transport', None)
            if transport:
                if hasattr(transport, 'is_closing') and transport.is_closing():
                    return False
                if hasattr(transport, '_closed') and transport._closed:
                    return False
        except Exception:
            return False
            
        return True

    async def _probe_and_maybe_blacklist(self, host_bytes: bytes):
        """Auto-detect blocked domains and add to blacklist"""
        try:
            host = host_bytes.decode("idna", "ignore") or host_bytes.decode("utf-8", "ignore") or ""
            if not host:
                return
                
            # Check if already processed
            if self.is_whitelisted(host, host_bytes):
                return
            if self.is_blacklisted(host, host_bytes):
                return
        except Exception:
            return

        try:
            ctx = ssl.create_default_context()
            await asyncio.wait_for(asyncio.open_connection(host, 443, ssl=ctx), timeout=3.5)
            # Connection succeeded, add to whitelist
            await self._add_to_whitelist(host, host_bytes)
            
        except asyncio.TimeoutError:
            # Connection timed out - likely blocked
            await self._add_to_blacklist(host, host_bytes)
        except (ssl.SSLError, ssl.CertificateError):
            # SSL issues - likely blocked
            await self._add_to_blacklist(host, host_bytes)
        except (OSError, ConnectionError):
            # Network issues - might be blocked
            await self._add_to_blacklist(host, host_bytes)
        except Exception as e:
            if self.verbose:
                self.logger.error("Probe error for %s: %s", host, e)

    async def _add_to_blacklist(self, host: str, host_bytes: bytes):
        """Add domain to blacklist"""
        try:
            if not self.is_blacklisted(host, host_bytes):
                self.blocked.append(host)
                self.blocked_bytes.append(host_bytes)
                
                async with self._blacklist_write_lock:
                    try:
                        with open(self.blacklist, "a", encoding="utf-8") as f:
                            f.write(host + "\n")
                    except (OSError, IOError) as e:
                        if self.verbose:
                            self.logger.error("Failed to write to blacklist: %s", e)
        except Exception as e:
            if self.verbose:
                self.logger.error("Failed to update blacklist: %s", e)

    async def _add_to_whitelist(self, host: str, host_bytes: bytes):
        """Add domain to whitelist"""
        try:
            if not self.is_whitelisted(host, host_bytes):
                self.whitelist.add(host)
                self.whitelist_bytes.add(host_bytes)
                
                if self.whitelist_file:
                    async with self._whitelist_write_lock:
                        try:
                            with open(self.whitelist_file, "a", encoding="utf-8") as f:
                                f.write(host + "\n")
                        except (OSError, IOError) as e:
                            if self.verbose:
                                self.logger.error("Failed to write to whitelist: %s", e)
        except Exception as e:
            if self.verbose:
                self.logger.error("Failed to update whitelist: %s", e)

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        conn_id = id(writer)
        tasks = []
        conn_info = None
        remote_writer = None
        
        try:
            # Store connection and its tasks
            self.active_connections[conn_id] = tasks
            
            client = writer.get_extra_info("peername")
            client_ip = (client or ("unknown", 0))[0]

            self._tune_socket(writer)

            try:
                http_data = await asyncio.wait_for(reader.read(65536), timeout=10)
            except asyncio.TimeoutError:
                self.failed_connections += 1
                raise ConnectionException("Client read timeout")
            
            if not http_data:
                return

            # Parse HTTP request
            try:
                headers = http_data.split(b"\r\n")
                first_line = headers[0].split(b" ")
                method = first_line[0] if first_line else b""
                url = first_line[1] if len(first_line) > 1 else b""
            except (IndexError, ValueError) as e:
                self.failed_connections += 1
                raise ConnectionException(f"Invalid HTTP request: {e}")

            # Extract host and port
            try:
                if method == b"CONNECT":
                    host_port = url.split(b":", 1)
                    host_b = host_port[0].strip()
                    port = int(host_port[1]) if len(host_port) > 1 else 443
                else:
                    host_header = next((h for h in headers if h.lower().startswith(b"host: ")), None)
                    if not host_header:
                        raise ConnectionException("Missing Host header")
                    host_value = host_header.split(b":", 1)[1].strip()
                    host_port = host_value.split(b":", 1)
                    host_b = host_port[0].strip()
                    port = int(host_port[1]) if len(host_port) > 1 else 80
            except (ValueError, IndexError) as e:
                self.failed_connections += 1
                raise ConnectionException(f"Invalid host/port: {e}")

            # Decode hostname
            try:
                host_str = host_b.decode("utf-8", "ignore")
                host_str = host_str.encode("idna").decode("ascii")
            except (UnicodeDecodeError, UnicodeError):
                host_str = host_b.decode("utf-8", "replace")
            
            # Check if domain is whitelisted
            is_whitelisted = self.is_whitelisted(host_str, host_b)
            
            conn_info = ConnectionInfo(
                src_ip=str(client_ip), 
                dst_domain=host_str, 
                method=method.decode(errors="ignore") or "?",
                whitelisted=is_whitelisted
            )

            if is_whitelisted:
                self.whitelisted_connections += 1
                if self.verbose:
                    self.print(f"\033[92m[WHITELIST]:\033[97m {host_str} - bypassing fragmentation")

            # Auto-detect blocked domains if enabled
            if self.auto_blacklist and not is_whitelisted:
                asyncio.create_task(self._probe_and_maybe_blacklist(host_b))

            if method == b"CONNECT":
                # HTTPS proxy
                try:
                    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    await writer.drain()
                except (OSError, ConnectionError) as e:
                    self.failed_connections += 1
                    conn_info.error = f"Client write failed: {e}"
                    raise

                # Connect to remote
                try:
                    remote_reader, remote_writer = await asyncio.wait_for(
                        asyncio.open_connection(host_str, port), 
                        timeout=5
                    )
                except asyncio.TimeoutError:
                    self.failed_connections += 1
                    conn_info.error = "Remote connection timeout"
                    raise ConnectionException(f"Connection to {host_str}:{port} timed out")
                except (OSError, ConnectionError) as e:
                    self.failed_connections += 1
                    conn_info.error = f"Remote connection failed: {e}"
                    raise ConnectionException(f"Failed to connect to {host_str}:{port}: {e}")
                
                self._tune_socket(remote_writer)

                # Fragment initial data (unless whitelisted)
                if not is_whitelisted:
                    try:
                        fragmented = await self.fragment_data(reader, remote_writer)
                        conn_info.fragmented = fragmented
                    except FragmentationException as e:
                        if self.verbose:
                            self.logger.error("Fragmentation error: %s", e)
                
                self.total_connections += 1

                # Create pipe tasks
                idle = IdleState()
                t1 = asyncio.create_task(self.pipe(reader, remote_writer, "out", conn_info, idle))
                t2 = asyncio.create_task(self.pipe(remote_reader, writer, "in", conn_info, idle))
                wd = asyncio.create_task(self.idle_watchdog(idle, writer, remote_writer))
                
                tasks.extend([t1, t2, wd])
                
                # Wait for completion
                done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
                
            else:
                # HTTP proxy
                try:
                    remote_reader, remote_writer = await asyncio.wait_for(
                        asyncio.open_connection(host_str, port), 
                        timeout=5
                    )
                except asyncio.TimeoutError:
                    self.failed_connections += 1
                    conn_info.error = "Remote connection timeout"
                    raise ConnectionException(f"Connection to {host_str}:{port} timed out")
                except (OSError, ConnectionError) as e:
                    self.failed_connections += 1
                    conn_info.error = f"Remote connection failed: {e}"
                    raise ConnectionException(f"Failed to connect to {host_str}:{port}: {e}")
                    
                self._tune_socket(remote_writer)
                
                try:
                    remote_writer.write(http_data)
                    await remote_writer.drain()
                except (OSError, ConnectionError) as e:
                    self.failed_connections += 1
                    conn_info.error = f"Remote write failed: {e}"
                    raise
                    
                self.total_connections += 1

                idle = IdleState()
                t1 = asyncio.create_task(self.pipe(reader, remote_writer, "out", conn_info, idle))
                t2 = asyncio.create_task(self.pipe(remote_reader, writer, "in", conn_info, idle))
                wd = asyncio.create_task(self.idle_watchdog(idle, writer, remote_writer))
                
                tasks.extend([t1, t2, wd])
                
                done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)

        except ConnectionException as e:
            if self.verbose:
                self.print(f"\033[93m[CONN_ERROR]: {e}\033[0m")
            self.logger.error("Connection error: %s", e)
            
        except asyncio.TimeoutError:
            if conn_info:
                conn_info.error = "Timeout"
            self.failed_connections += 1
            
        except asyncio.CancelledError:
            raise
            
        except Exception as e:
            self.failed_connections += 1
            if conn_info:
                conn_info.error = f"Unexpected error: {type(e).__name__}"
            if self.verbose:
                self.print(f"\033[91m[ERROR]: Unexpected error in connection handler: {e}\033[0m")
            self.logger.error("Unexpected error: %s\n%s", e, traceback.format_exc())
            
        finally:
            # Log connection info
            if conn_info and self.log_access_file:
                try:
                    log_msg = (f"{conn_info.start_time} | {conn_info.src_ip} -> {conn_info.dst_domain} | "
                              f"{conn_info.method} | IN: {self.format_size(conn_info.traffic_in)} | "
                              f"OUT: {self.format_size(conn_info.traffic_out)}")
                    if conn_info.whitelisted:
                        log_msg += " | WHITELISTED"
                    if conn_info.fragmented:
                        log_msg += " | FRAGMENTED"
                    if conn_info.error:
                        log_msg += f" | ERROR: {conn_info.error}"
                    self.logger.info(log_msg)
                except Exception:
                    pass
            
            # Cancel all tasks for this connection
            for task in tasks:
                if not task.done():
                    task.cancel()
            
            # Wait briefly for cancellation
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            
            # Remove from active connections
            self.active_connections.pop(conn_id, None)
            
            # Close connections
            for w in [writer, remote_writer]:
                if w and self.is_writer_alive(w):
                    try:
                        w.close()
                        await asyncio.wait_for(w.wait_closed(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
                    except Exception:
                        pass

    async def pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, 
                   direction: str, conn_info: ConnectionInfo, idle: IdleState = None):
        try:
            while not self.shutdown_event.is_set():
                if not self.is_writer_alive(writer):
                    break
                
                try:
                    data = await asyncio.wait_for(reader.read(self.bufsize), timeout=30)
                except asyncio.TimeoutError:
                    break
                except (OSError, ConnectionError) as e:
                    if self.verbose:
                        self.logger.error("Read error in pipe: %s", e)
                    break
                except Exception as e:
                    if self.verbose:
                        self.logger.error("Unexpected read error: %s", e)
                    break
                
                if not data:
                    break

                if idle:
                    idle.last_activity = time.monotonic()

                # Update traffic stats
                if direction == "out":
                    self.traffic_out += len(data)
                    conn_info.traffic_out += len(data)
                else:
                    self.traffic_in += len(data)
                    conn_info.traffic_in += len(data)

                if not self.is_writer_alive(writer):
                    break
                
                try:
                    writer.write(data)
                    
                    # Check write buffer size
                    transport = getattr(writer, 'transport', None)
                    if transport and hasattr(transport, 'get_write_buffer_size'):
                        if transport.get_write_buffer_size() > self.high_water:
                            await writer.drain()
                            
                except (OSError, ConnectionError) as e:
                    if self.verbose:
                        self.logger.error("Write error in pipe: %s", e)
                    break
                except Exception as e:
                    if self.verbose:
                        self.logger.error("Unexpected write error: %s", e)
                    break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if self.verbose:
                self.logger.error("Pipe error: %s", e)
        finally:
            if self.is_writer_alive(writer):
                try:
                    writer.close()
                    await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass

    async def fragment_data(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """Fragment TLS handshake data. Returns True if fragmented, False otherwise."""
        try:
            head = await asyncio.wait_for(reader.readexactly(5), timeout=5)
        except asyncio.TimeoutError:
            raise FragmentationException("Timeout reading TLS header")
        except asyncio.IncompleteReadError:
            return False  # Not enough data
        except Exception as e:
            raise FragmentationException(f"Failed to read TLS header: {e}")

        if not head or head[0] != 0x16:
            # Not TLS handshake, pass through
            try:
                rest = await reader.read(self.bufsize)
                writer.write(head + rest)
                await writer.drain()
            except (OSError, ConnectionError) as e:
                raise FragmentationException(f"Failed to pass through non-TLS data: {e}")
            return False

        rec_len = int.from_bytes(head[3:5], "big")
        
        try:
            body = await asyncio.wait_for(reader.readexactly(rec_len), timeout=5)
        except asyncio.TimeoutError:
            raise FragmentationException("Timeout reading TLS body")
        except asyncio.IncompleteReadError as e:
            raise FragmentationException(f"Incomplete TLS body: expected {rec_len}, got {len(e.partial)}")
        except Exception as e:
            raise FragmentationException(f"Failed to read TLS body: {e}")

        self.blocked_connections += 1

        def frame(chunk: bytes) -> bytes:
            return b"\x16\x03\x04" + len(chunk).to_bytes(2, "big") + chunk

        # Fragment the data
        host_end = body.find(b"\x00")
        if 0 <= host_end < len(body):
            chunks = [body[:host_end + 1], body[host_end + 1:]]
        else:
            if len(body) <= 512:
                cut1 = max(1, len(body) // 2)
                chunks = [body[:cut1], body[cut1:]]
            else:
                c1 = random.randint(32, 128)
                c2 = random.randint(128, 512)
                c1 = min(c1, len(body))
                c2 = min(c1 + c2, len(body))
                chunks = [body[:c1], body[c1:c2], body[c2:]]

        # Send fragmented data
        try:
            for chunk in chunks:
                if chunk:
                    writer.write(frame(chunk))
                    await writer.drain()
        except (OSError, ConnectionError) as e:
            raise FragmentationException(f"Failed to send fragmented data: {e}")
        except Exception as e:
            raise FragmentationException(f"Unexpected error sending fragments: {e}")
            
        return True

    async def idle_watchdog(self, idle: IdleState, client_writer: asyncio.StreamWriter, 
                           remote_writer: asyncio.StreamWriter):
        if not self.idle_timeout or self.idle_timeout <= 0:
            return
        
        try:
            while not self.shutdown_event.is_set():
                await asyncio.sleep(1)
                if time.monotonic() - idle.last_activity > self.idle_timeout:
                    if self.verbose:
                        self.print(f"\033[93m[IDLE]: Connection idle for {self.idle_timeout}s, closing\033[0m")
                    
                    for w in (client_writer, remote_writer):
                        if self.is_writer_alive(w):
                            try:
                                w.close()
                            except Exception:
                                pass
                    break
                    
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.verbose:
                self.logger.error("Idle watchdog error: %s", e)

    async def shutdown(self, sig=None):
        """Gracefully shutdown the proxy server"""
        if self.shutdown_event.is_set():
            return
        
        self.shutdown_event.set()
        
        if sig:
            self.print(f"\n\n\033[92m[INFO]:\033[97m Received signal {sig.name if hasattr(sig, 'name') else sig}")
        
        self.print("\033[92m[INFO]:\033[97m Initiating graceful shutdown...")
        
        # Stop accepting new connections
        if self.server:
            try:
                self.server.close()
                await self.server.wait_closed()
            except Exception as e:
                self.logger.error("Error closing server: %s", e)
        
        # Cancel stats task
        if self.stats_task and not self.stats_task.done():
            self.stats_task.cancel()
            try:
                await self.stats_task
            except asyncio.CancelledError:
                pass
        
        # Cancel all active connection tasks
        all_tasks = []
        for conn_id, tasks in list(self.active_connections.items()):
            for task in tasks:
                if not task.done():
                    task.cancel()
                    all_tasks.append(task)
        
        # Wait briefly for all tasks to cancel
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        
        # Clear active connections
        self.active_connections.clear()
        
        # Print final statistics
        self.print("\n\033[92m[FINAL STATS]:\033[97m")
        self.print(f"  Total connections: {self.total_connections}")
        self.print(f"  Fragmented connections: {self.blocked_connections}")
        self.print(f"  Whitelisted connections: {self.whitelisted_connections}")
        self.print(f"  Failed connections: {self.failed_connections}")
        self.print(f"  Total downloaded: {self.format_size(self.traffic_in)}")
        self.print(f"  Total uploaded: {self.format_size(self.traffic_out)}")
        
        self.print("\033[92m[INFO]:\033[97m Proxy shut down successfully")
        self._shutdown_complete.set()


class ProxyApplication:
    @staticmethod
    def parse_args():
        parser = argparse.ArgumentParser(
            description="HTTP/HTTPS proxy with TLS fragmentation support",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Examples:
  %(prog)s --port 8080
  %(prog)s --blacklist block.txt --whitelist allow.txt
  %(prog)s --autoblacklist --whitelist important.txt
  %(prog)s --quiet --log_access access.log --log_error error.log
            """
        )
        
        # Network settings
        net_group = parser.add_argument_group('Network Settings')
        net_group.add_argument("--host", default="127.0.0.1", help="Proxy host (default: 127.0.0.1)")
        net_group.add_argument("--port", type=int, default=8881, help="Proxy port (default: 8881)")
        net_group.add_argument("--idle-timeout", type=int, default=300,
                               help="Close connections after N seconds of inactivity (0 to disable, default: 300)")
        
        # Filtering settings
        filter_group = parser.add_argument_group('Filtering Settings')
        blacklist_group = filter_group.add_mutually_exclusive_group()
        blacklist_group.add_argument("--blacklist", default="blacklist.txt", 
                                     help="Path to blacklist file (default: blacklist.txt)")
        blacklist_group.add_argument("--autoblacklist", action="store_true", 
                                     help="Automatic detection of blocked domains")
        filter_group.add_argument("--whitelist", 
                                 help="Path to whitelist file (domains that bypass fragmentation)")
        
        # Logging settings
        log_group = parser.add_argument_group('Logging Settings')
        log_group.add_argument("--log_access", help="Path to access log file")
        log_group.add_argument("--log_error", help="Path to error log file")
        
        # Output settings
        output_group = parser.add_argument_group('Output Settings')
        output_group.add_argument("-q", "--quiet", action="store_true", help="Suppress console output")
        output_group.add_argument("-v", "--verbose", action="store_true", help="Show detailed debug info")
        
        return parser.parse_args()

    @classmethod
    async def run(cls):
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        args = cls.parse_args()

        proxy = ProxyServer(
            args.host,
            args.port,
            args.blacklist,
            args.whitelist,
            args.log_access,
            args.log_error,
            args.autoblacklist,
            args.quiet,
            args.verbose,
            args.idle_timeout,
        )

        try:
            await proxy.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"\n\033[91m[FATAL]: Unexpected error: {e}\033[0m")
            logging.error("Fatal error: %s\n%s", e, traceback.format_exc())
        finally:
            if not proxy.shutdown_event.is_set():
                await proxy.shutdown()
            # Ensure complete shutdown
            await proxy._shutdown_complete.wait()


def main():
    loop = None
    try:
        # Create new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Run the application
        loop.run_until_complete(ProxyApplication.run())
        
    except KeyboardInterrupt:
        print("\n\033[92m[INFO]:\033[97m Keyboard interrupt received")
    except Exception as e:
        print(f"\n\033[91m[FATAL]: {e}\033[0m")
        traceback.print_exc()
    finally:
        # Cancel all remaining tasks
        if loop:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            
            # Wait for all tasks to complete
            if pending:
                try:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except:
                    pass
            
            # Close the loop
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
            except:
                pass
        
        # Force exit if still hanging
        sys.exit(0)


if __name__ == "__main__":
    main()

# python3 main.py --host 0.0.0.0 --port 8884 --autoblacklist --whitelist /opt/dpi/trusted.txt
