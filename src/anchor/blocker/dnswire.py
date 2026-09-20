"""Just enough of the DNS wire format to decide about a query (SPEC 8.2).

Anchor's resolver reads the question, decides whether the name is blocked, and
either refuses it or forwards the original bytes untouched. It never rewrites
an answer, so nothing here parses one. That is deliberate: a resolver that
reassembles replies is a resolver that can corrupt them, and the less of an
attacker-supplied packet Anchor interprets, the less there is to get wrong.

Only the standard library is used, as SPEC 5.4 requires of root daemons.
"""

from __future__ import annotations

import struct
from typing import Final

#: Header: identifier, flags, and the four section counts.
_HEADER: Final = struct.Struct(">HHHHHH")
_HEADER_SIZE: Final = _HEADER.size

#: Flag bits, from RFC 1035 section 4.1.1.
_QR: Final = 0x8000
_OPCODE: Final = 0x7800
_RD: Final = 0x0100
_RA: Final = 0x0080
_RCODE: Final = 0x000F

#: RCODE 3: the name does not exist. This is what a blocked name gets, so the
#: browser shows its ordinary "server not found" page (SPEC 8.3).
NXDOMAIN: Final = 3

#: RCODE 2: something went wrong here. Used when no upstream would answer.
#:
#: Deliberately not NXDOMAIN: claiming a name does not exist because Anchor
#: could not reach a server would be a lie, and a lie clients cache.
SERVFAIL: Final = 2

#: A label is at most 63 bytes, and the top two bits of a length byte being set
#: marks a compression pointer rather than a length (RFC 1035, 4.1.4).
_MAX_LABEL: Final = 63
_POINTER_MASK: Final = 0xC0

#: A name is at most 255 bytes on the wire.
_MAX_NAME: Final = 255


class MalformedMessageError(ValueError):
    """The packet is not a DNS message Anchor is willing to interpret."""


def is_query(packet: bytes) -> bool:
    """Whether ``packet`` is a query rather than a response."""
    if len(packet) < _HEADER_SIZE:
        return False
    flags = _HEADER.unpack_from(packet)[1]
    return not flags & _QR


def _question_end(packet: bytes) -> tuple[str, int]:
    """Return the question's name and the offset just past the question."""
    if len(packet) < _HEADER_SIZE:
        raise MalformedMessageError("message is shorter than a DNS header")

    qdcount = _HEADER.unpack_from(packet)[2]
    if qdcount < 1:
        raise MalformedMessageError("message carries no question")

    labels: list[str] = []
    offset = _HEADER_SIZE
    total = 0

    while True:
        if offset >= len(packet):
            raise MalformedMessageError("the question name runs past the end of the message")

        length = packet[offset]
        if length == 0:
            offset += 1
            break

        if length & _POINTER_MASK:
            # Compression is legal in an answer, never in a query's question.
            # Refusing it costs nothing and removes a class of parsing trick.
            raise MalformedMessageError("the question name uses compression")

        if length > _MAX_LABEL:
            raise MalformedMessageError(f"label length {length} exceeds {_MAX_LABEL}")

        total += length + 1
        if total > _MAX_NAME:
            raise MalformedMessageError("the question name is longer than DNS allows")

        start = offset + 1
        end = start + length
        if end > len(packet):
            raise MalformedMessageError("a label runs past the end of the message")

        labels.append(packet[start:end].decode("ascii", errors="replace"))
        offset = end

    # The question ends with its type and class.
    offset += 4
    if offset > len(packet):
        raise MalformedMessageError("the question is truncated")

    return ".".join(labels).lower(), offset


def question_name(packet: bytes) -> str:
    """The name being asked about, lowercased and without a trailing dot.

    Lowercasing matters: DNS is case-insensitive, so a rule for ``example.com``
    has to catch ``ExAmPlE.CoM`` too.
    """
    return _question_end(packet)[0]


def _refusal(query: bytes, rcode: int) -> bytes:
    name_end = _question_end(query)[1]
    ident, flags = _HEADER.unpack_from(query)[:2]

    # Keep the opcode and the client's recursion-desired bit, mark this as a
    # response that could have recursed, and report the code.
    response_flags = _QR | (flags & _OPCODE) | (flags & _RD) | _RA | rcode

    header = _HEADER.pack(ident, response_flags, 1, 0, 0, 0)
    return header + query[_HEADER_SIZE:name_end]


def nxdomain_response(query: bytes) -> bytes:
    """Build the refusal for ``query`` (SPEC 8.2).

    The identifier and the question come straight back, because that is how a
    client matches an answer to what it asked. Nothing is claimed in the answer
    sections: Anchor says the name does not exist and offers nothing else.
    """
    return _refusal(query, NXDOMAIN)


def servfail_response(query: bytes) -> bytes:
    """Build the answer for a query no upstream would take."""
    return _refusal(query, SERVFAIL)


#: Record types carrying an address.
TYPE_A: Final = 1
TYPE_AAAA: Final = 28

#: A cap on how many names a single message may make us walk, so a malicious
#: reply cannot spin the parser.
_MAX_RECORDS: Final = 64


def _skip_name(packet: bytes, offset: int) -> int:
    """Return the offset just past the name starting at ``offset``.

    Names in an answer may be compressed into a pointer to somewhere earlier in
    the message. The pointer is not followed here: nothing needs the name, only
    where it ends, and following pointers is how a parser gets led in a circle.
    """
    steps = 0
    while offset < len(packet):
        length = packet[offset]
        if length == 0:
            return offset + 1
        if length & _POINTER_MASK:
            # A pointer is two bytes and always ends the name.
            return offset + 2
        offset += length + 1
        steps += 1
        if steps > _MAX_RECORDS:
            raise MalformedMessageError("a name in the message does not end")
    raise MalformedMessageError("a name runs past the end of the message")


def parse_addresses(packet: bytes) -> list[str]:
    """Read the IPv4 and IPv6 addresses out of an answer (SPEC 8.2).

    Used for the short-lived map of recent answers, so that when a session
    starts Anchor can reject the addresses of names it now blocks, and a page
    already open cannot keep loading from a connection made moments earlier.

    Only address records are read. Anything malformed yields what was
    understood so far rather than raising: a partly readable answer is still
    worth the addresses it did contain, and the caller is building a blocklist,
    not trusting the packet.
    """
    import ipaddress

    if len(packet) < _HEADER_SIZE:
        return []

    _, _, qdcount, ancount, _, _ = _HEADER.unpack_from(packet)
    if ancount == 0:
        return []

    offset = _HEADER_SIZE
    addresses: list[str] = []

    try:
        # Step over the questions.
        for _ in range(min(qdcount, _MAX_RECORDS)):
            offset = _skip_name(packet, offset) + 4

        for _ in range(min(ancount, _MAX_RECORDS)):
            offset = _skip_name(packet, offset)
            if offset + 10 > len(packet):
                break
            rtype, _rclass, _ttl, rdlength = struct.unpack_from(">HHIH", packet, offset)
            offset += 10

            if offset + rdlength > len(packet):
                break

            data = packet[offset : offset + rdlength]
            offset += rdlength

            if rtype == TYPE_A and rdlength == 4:
                addresses.append(str(ipaddress.IPv4Address(data)))
            elif rtype == TYPE_AAAA and rdlength == 16:
                addresses.append(str(ipaddress.IPv6Address(data)))
    except (MalformedMessageError, struct.error, ValueError):
        # Whatever was read before the damage is still usable.
        return addresses

    return addresses
