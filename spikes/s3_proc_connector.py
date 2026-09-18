#!/usr/bin/env python3
"""Spike 3: watching process launches with the netlink proc connector (SPEC 9).

Application blocking has to notice a program the moment it starts, not two
seconds later, so the specification uses kernel process events with polling
only as a fallback. This spike subscribes to the connector, launches a
process, and measures how quickly the exec event arrives.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import time

from lib import SpikeReport, Verdict, main

NETLINK_CONNECTOR = 11
CN_IDX_PROC = 1
CN_VAL_PROC = 1
PROC_CN_MCAST_LISTEN = 1
PROC_CN_MCAST_IGNORE = 2

PROC_EVENT_EXEC = 0x00000002
PROC_EVENT_EXIT = 0x80000000

_NLMSGHDR = struct.Struct("=IHHII")
_CN_MSG = struct.Struct("=IIIIHH")


def _subscribe_payload(operation: int) -> bytes:
    """A netlink message asking the connector to start or stop sending events."""
    body = struct.pack("=I", operation)
    cn_msg = _CN_MSG.pack(CN_IDX_PROC, CN_VAL_PROC, 0, 0, len(body), 0)
    payload = cn_msg + body
    header = _NLMSGHDR.pack(
        _NLMSGHDR.size + len(payload),
        0x0003,  # NLMSG_DONE
        0,
        0,
        os.getpid(),
    )
    return header + payload


def spike(report: SpikeReport) -> None:
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_CONNECTOR)
    except OSError as error:
        report.add(
            "netlink connector available",
            Verdict.UNAVAILABLE,
            f"could not open a NETLINK_CONNECTOR socket: {error}",
        )
        return

    try:
        try:
            sock.bind((os.getpid(), CN_IDX_PROC))
        except OSError as error:
            report.add(
                "bind to the proc group",
                Verdict.UNAVAILABLE,
                f"binding to the process event group failed: {error}. "
                "This needs CAP_NET_ADMIN and a kernel built with "
                "CONFIG_PROC_EVENTS.",
            )
            return

        report.add("bind to the proc group", Verdict.WORKS, "subscribed to CN_IDX_PROC")

        sock.send(_subscribe_payload(PROC_CN_MCAST_LISTEN))
        sock.settimeout(5.0)

        # Drain whatever is already in flight, so the measurement below is of
        # our own process rather than of the machine's background noise.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                sock.recv(4096)
            except (TimeoutError, OSError):
                break

        started = time.monotonic()
        child = subprocess.Popen(["/bin/true"])
        child_pid = child.pid
        child.wait()

        seen_exec = False
        latency = 0.0
        events = 0
        end = time.monotonic() + 5.0
        while time.monotonic() < end and not seen_exec:
            try:
                data = sock.recv(4096)
            except TimeoutError:
                break
            except OSError:
                break

            events += 1
            offset = _NLMSGHDR.size + _CN_MSG.size
            if len(data) < offset + 8:
                continue
            what, _cpu = struct.unpack_from("=II", data, offset)
            if what not in (PROC_EVENT_EXEC, PROC_EVENT_EXIT):
                continue
            # The event body starts after what/cpu/timestamp_ns.
            body = offset + 16
            if len(data) < body + 8:
                continue
            pid, _tgid = struct.unpack_from("=ii", data, body)
            if what == PROC_EVENT_EXEC and pid == child_pid:
                seen_exec = True
                latency = time.monotonic() - started

        if seen_exec:
            report.add(
                "exec events arrive",
                Verdict.WORKS,
                f"the exec of pid {child_pid} was seen {latency * 1000:.1f} ms after launch, "
                f"among {events} events",
            )
        else:
            report.add(
                "exec events arrive",
                Verdict.FAILS,
                f"no exec event for pid {child_pid} arrived within 5 s "
                f"({events} events seen). Application blocking would have to fall "
                "back to the 2-second poll of SPEC 9.",
            )

        sock.send(_subscribe_payload(PROC_CN_MCAST_IGNORE))
        report.add("unsubscribe", Verdict.WORKS, "stopped listening cleanly")
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(
        main(
            spike,
            SpikeReport(
                spike="s3-proc-connector",
                question="Does the netlink proc connector report process launches promptly?",
            ),
        )
    )
