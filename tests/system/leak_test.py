#!/usr/bin/env python3
"""Leak tests: prove a blocked domain really cannot be reached (SPEC 8.4).

Blocking that works only through the front door is not blocking. SPEC 8.4 names
five ways around it, and each one is tried here against a real session on a
real machine: plain DNS, a direct query to a public resolver, DNS over TLS, a
DoH endpoint from the shipped list, and an address cached before the session
began.

Run as root, on a machine you can afford to disturb:

    sudo python3 tests/system/leak_test.py [results-dir]

Everything is undone at the end, including if a check fails, and the last
thing the script does is confirm that ordinary name resolution works again.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from anchor.blocker.constants import NFT_FAMILY, NFT_TABLE  # noqa: E402

#: A name that resolves perfectly well, so that failing to resolve it means
#: Anchor did it. A .invalid name would prove nothing, as Milestone 0 learned.
BLOCKED = "example.com"

#: A name the session does not block, to show the machine still works.
ALLOWED = "github.com"

#: A DoH endpoint from the shipped list, and a DoT one.
DOH_ADDRESS = "1.1.1.1"
DOT_ADDRESS = "1.1.1.1"

ANCHOR_ROOT = Path("/tmp/anchor-leak")  # noqa: S108


@dataclass
class Check:
    name: str
    leaked: bool
    detail: str

    def line(self) -> str:
        return f"[{'LEAK' if self.leaked else 'ok  '}] {self.name}: {self.detail}"


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, leaked: bool, detail: str) -> None:
        check = Check(name, leaked, detail)
        self.checks.append(check)
        print(check.line(), flush=True)

    @property
    def leaked(self) -> bool:
        return any(check.leaked for check in self.checks)


def run(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def dns_query(name: str) -> bytes:
    import struct

    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".") if p)
    header = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0)
    return header + labels + b"\x00" + struct.pack(">HH", 1, 1)


def rcode_of(packet: bytes) -> int:
    import struct

    return struct.unpack(">H", packet[2:4])[0] & 0x000F


# -- the five ways around ---------------------------------------------------


def check_plain_dns(report: Report) -> None:
    """The ordinary path every program uses."""
    result = run("getent", "hosts", BLOCKED)
    resolved = bool(result.stdout.strip())
    report.add(
        "plain DNS",
        resolved,
        f"getent returned {result.stdout.strip()!r}" if resolved else "no answer, as intended",
    )


def check_public_resolver(report: Report) -> None:
    """Asking a public resolver directly, ignoring the system configuration."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(5)
            sock.sendto(dns_query(BLOCKED), ("8.8.8.8", 53))
            reply, _ = sock.recvfrom(4096)
    except OSError as error:
        report.add("direct query to a public resolver", False, f"refused: {error}")
        return

    code = rcode_of(reply)
    report.add(
        "direct query to a public resolver",
        code != 3,
        "the redirect sent it to Anchor, which answered NXDOMAIN"
        if code == 3
        else f"a public resolver answered directly (rcode {code})",
    )


def check_dns_over_tls(report: Report) -> None:
    """Port 853, which would reach a resolver Anchor cannot see."""
    try:
        context = ssl.create_default_context()
        with (
            socket.create_connection((DOT_ADDRESS, 853), timeout=6) as raw,
            context.wrap_socket(raw, server_hostname="cloudflare-dns.com") as tls,
        ):
            tls.send(b"\x00\x00")
    except (OSError, ssl.SSLError) as error:
        report.add("DNS over TLS", False, f"refused: {type(error).__name__}")
        return

    report.add("DNS over TLS", True, f"connected to {DOT_ADDRESS}:853")


def check_doh_endpoint(report: Report) -> None:
    """Port 443 to a DoH provider on the shipped list."""
    try:
        with socket.create_connection((DOH_ADDRESS, 443), timeout=6):
            pass
    except OSError as error:
        report.add("DoH endpoint", False, f"refused: {type(error).__name__}")
        return

    report.add("DoH endpoint", True, f"connected to {DOH_ADDRESS}:443")


def check_cached_address(report: Report, address: str | None) -> None:
    """An address resolved before the session began (SPEC 8.2)."""
    if address is None:
        report.add("cached address", False, "nothing was cached to try; skipped")
        return

    try:
        with socket.create_connection((address, 443), timeout=6):
            pass
    except OSError as error:
        report.add("cached address", False, f"refused: {type(error).__name__}")
        return

    report.add("cached address", True, f"connected to {address}:443, which was cached before")


# -- the machinery ----------------------------------------------------------


def write_profile() -> None:
    from anchor.engine.core import Engine
    from anchor.engine.paths import Paths, Settings
    from anchor.engine.profiles import Profile
    from anchor.protocol.types import WebMode

    paths = Paths.resolve(ANCHOR_ROOT)
    paths.ensure_directories()
    engine = Engine(paths, Settings(owner_uid=os.getuid()))
    engine.profiles["Leak"] = Profile(
        name="Leak", web_mode=WebMode.BLOCKLIST, domains=frozenset({BLOCKED})
    )
    engine.save_config()


def wait_for(path: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.1)
    raise SystemExit(f"{path} never appeared")


def main() -> int:
    if os.geteuid() != 0:
        print("this must run as root: it loads firewall rules", file=sys.stderr)
        return 2

    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "leak-results"
    report = Report()

    run("rm", "-rf", str(ANCHOR_ROOT))
    (ANCHOR_ROOT / "var" / "lib" / "anchor").mkdir(parents=True, exist_ok=True)
    write_profile()

    environment = {**os.environ, "ANCHOR_ROOT": str(ANCHOR_ROOT), "PYTHONPATH": str(ROOT / "src")}
    engine = subprocess.Popen(
        [sys.executable, "-m", "anchor.engine.main", "--owner-uid", str(os.getuid())],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    blocker = subprocess.Popen(
        [sys.executable, "-m", "anchor.blocker.main", "--data-dir", str(ROOT / "data")],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    cached: str | None = None
    try:
        wait_for(ANCHOR_ROOT / "run" / "anchor" / "engine.sock")
        time.sleep(2)

        # Resolve the soon-to-be-blocked name first, so its address is in the
        # recent-answer map and the cached-address check has something real.
        before = run("getent", "hosts", BLOCKED)
        if before.stdout.strip():
            cached = before.stdout.split()[0]
            print(f"--- {BLOCKED} resolves to {cached} before the session", flush=True)

        from anchor.cli.client import EngineClient
        from anchor.engine.paths import Paths

        paths = Paths.resolve(ANCHOR_ROOT)
        with EngineClient(paths.engine_socket) as client:
            started = client.call(
                "session.start",
                {"profile": "Leak", "duration_seconds": 600, "level": "soft"},
            )
            if not started.ok:
                print(f"could not start a session: {started.error}", file=sys.stderr)
                return 1

        time.sleep(3)
        print(f"--- session running; {BLOCKED} should now be unreachable\n", flush=True)

        check_plain_dns(report)
        check_public_resolver(report)
        check_dns_over_tls(report)
        check_doh_endpoint(report)
        check_cached_address(report, cached)

        allowed = run("getent", "hosts", ALLOWED)
        report.add(
            "an unblocked site still works",
            not allowed.stdout.strip(),
            f"{ALLOWED} resolves" if allowed.stdout.strip() else f"{ALLOWED} did NOT resolve",
        )
    finally:
        for process in (blocker, engine):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

        run("nft", "delete", "table", NFT_FAMILY, NFT_TABLE)
        subprocess.run(
            [sys.executable, "-m", "anchor.blocker.main", "--restore"],
            env=environment,
            check=False,
            capture_output=True,
        )

    after = run("getent", "hosts", ALLOWED)
    report.add(
        "the machine is left working",
        not after.stdout.strip(),
        "name resolution works after the session" if after.stdout.strip() else "DNS IS BROKEN",
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "leak-results.json").write_text(
        json.dumps(
            {"leaked": report.leaked, "checks": [asdict(c) for c in report.checks]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"\n{'LEAKS FOUND' if report.leaked else 'no leaks'}", flush=True)
    return 1 if report.leaked else 0


if __name__ == "__main__":
    raise SystemExit(main())
