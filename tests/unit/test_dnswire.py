"""Reading just enough of a DNS message to decide about it (SPEC 8.2).

The resolver parses only the question section and forwards everything else
untouched, so these tests are about that one job: getting the name out, and
building a refusal that a client will accept.
"""

from __future__ import annotations

import struct

import pytest

from anchor.blocker.dnswire import (
    MalformedMessageError,
    is_query,
    nxdomain_response,
    question_name,
)


def query(name: str, *, qtype: int = 1, ident: int = 0x1234, flags: int = 0x0100) -> bytes:
    """Build a DNS query for ``name``."""
    labels = b"".join(bytes([len(part)]) + part.encode("ascii") for part in name.split(".") if part)
    header = struct.pack(">HHHHHH", ident, flags, 1, 0, 0, 0)
    return header + labels + b"\x00" + struct.pack(">HH", qtype, 1)


class TestReadingTheName:
    def test_a_plain_name(self) -> None:
        assert question_name(query("example.com")) == "example.com"

    def test_a_subdomain(self) -> None:
        assert question_name(query("www.news.example.com")) == "www.news.example.com"

    def test_the_name_is_lowercased(self) -> None:
        """DNS is case-insensitive, and a rule must not be dodged by shouting."""
        assert question_name(query("ExAmPlE.CoM")) == "example.com"

    def test_a_single_label(self) -> None:
        assert question_name(query("localhost")) == "localhost"

    def test_the_root(self) -> None:
        header = struct.pack(">HHHHHH", 1, 0x0100, 1, 0, 0, 0)
        packet = header + b"\x00" + struct.pack(">HH", 1, 1)
        assert question_name(packet) == ""

    def test_a_long_name_is_read_whole(self) -> None:
        name = ".".join(["a" * 60] * 4)[:250]
        assert question_name(query(name)) == name.lower()


class TestRejectingRubbish:
    def test_a_truncated_header(self) -> None:
        with pytest.raises(MalformedMessageError):
            question_name(b"\x12\x34")

    def test_an_empty_packet(self) -> None:
        with pytest.raises(MalformedMessageError):
            question_name(b"")

    def test_a_name_that_runs_off_the_end(self) -> None:
        header = struct.pack(">HHHHHH", 1, 0x0100, 1, 0, 0, 0)
        with pytest.raises(MalformedMessageError):
            question_name(header + b"\x09short")

    def test_a_label_longer_than_dns_allows(self) -> None:
        header = struct.pack(">HHHHHH", 1, 0x0100, 1, 0, 0, 0)
        packet = header + bytes([64]) + b"a" * 64 + b"\x00" + struct.pack(">HH", 1, 1)
        with pytest.raises(MalformedMessageError):
            question_name(packet)

    def test_a_compression_pointer_in_a_question_is_refused(self) -> None:
        """A query's question cannot be compressed; one that is, is hostile."""
        header = struct.pack(">HHHHHH", 1, 0x0100, 1, 0, 0, 0)
        with pytest.raises(MalformedMessageError):
            question_name(header + b"\xc0\x0c")

    def test_a_message_with_no_question(self) -> None:
        header = struct.pack(">HHHHHH", 1, 0x0100, 0, 0, 0, 0)
        with pytest.raises(MalformedMessageError):
            question_name(header)

    def test_a_name_that_never_ends(self) -> None:
        header = struct.pack(">HHHHHH", 1, 0x0100, 1, 0, 0, 0)
        body = b"".join(bytes([3]) + b"abc" for _ in range(100))
        with pytest.raises(MalformedMessageError):
            question_name(header + body)


class TestTellingQueriesFromAnswers:
    def test_a_query_is_a_query(self) -> None:
        assert is_query(query("example.com"))

    def test_a_response_is_not(self) -> None:
        assert not is_query(query("example.com", flags=0x8180))

    def test_rubbish_is_not_a_query(self) -> None:
        assert not is_query(b"\x00")


class TestRefusing:
    def test_the_identifier_is_echoed(self) -> None:
        """A client matches the answer to its question by this number."""
        answer = nxdomain_response(query("example.com", ident=0xABCD))
        assert struct.unpack(">H", answer[:2])[0] == 0xABCD

    def test_it_is_marked_as_a_response(self) -> None:
        answer = nxdomain_response(query("example.com"))
        flags = struct.unpack(">H", answer[2:4])[0]
        assert flags & 0x8000, "the QR bit is not set, so clients will ignore it"

    def test_the_code_is_name_error(self) -> None:
        answer = nxdomain_response(query("example.com"))
        flags = struct.unpack(">H", answer[2:4])[0]
        assert flags & 0x000F == 3

    def test_recursion_available_is_advertised(self) -> None:
        answer = nxdomain_response(query("example.com"))
        flags = struct.unpack(">H", answer[2:4])[0]
        assert flags & 0x0080

    def test_the_recursion_desired_bit_comes_back(self) -> None:
        with_rd = nxdomain_response(query("example.com", flags=0x0100))
        without_rd = nxdomain_response(query("example.com", flags=0x0000))

        assert struct.unpack(">H", with_rd[2:4])[0] & 0x0100
        assert not struct.unpack(">H", without_rd[2:4])[0] & 0x0100

    def test_the_question_is_echoed_back(self) -> None:
        """A resolver that drops the question confuses strict clients."""
        original = query("example.com")
        answer = nxdomain_response(original)

        assert struct.unpack(">H", answer[4:6])[0] == 1
        assert answer[12:] == original[12:]

    def test_no_records_are_claimed(self) -> None:
        answer = nxdomain_response(query("example.com"))
        _, _, _, ancount, nscount, arcount = struct.unpack(">HHHHHH", answer[:12])
        assert (ancount, nscount, arcount) == (0, 0, 0)

    def test_refusing_rubbish_raises_rather_than_guessing(self) -> None:
        with pytest.raises(MalformedMessageError):
            nxdomain_response(b"\x01\x02")


class TestReadingAddressesFromAnswers:
    @staticmethod
    def answer(name: str, records: list[tuple[int, bytes]]) -> bytes:
        labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".") if p) + b"\x00"
        header = struct.pack(">HHHHHH", 1, 0x8180, 1, len(records), 0, 0)
        body = labels + struct.pack(">HH", 1, 1)
        for rtype, data in records:
            # A compression pointer back to the question's name, as a real
            # server sends.
            body += b"\xc0\x0c" + struct.pack(">HHIH", rtype, 1, 300, len(data)) + data
        return header + body

    def test_an_ipv4_address_is_read(self) -> None:
        from anchor.blocker.dnswire import parse_addresses

        packet = self.answer("example.com", [(1, bytes([93, 184, 216, 34]))])
        assert parse_addresses(packet) == ["93.184.216.34"]

    def test_an_ipv6_address_is_read(self) -> None:
        from anchor.blocker.dnswire import parse_addresses

        raw = bytes.fromhex("2606280000000000000000000000c0d0")
        assert parse_addresses(self.answer("example.com", [(28, raw)])) == ["2606:2800::c0d0"]

    def test_several_addresses_come_back_in_order(self) -> None:
        from anchor.blocker.dnswire import parse_addresses

        packet = self.answer(
            "example.com",
            [(1, bytes([1, 2, 3, 4])), (1, bytes([5, 6, 7, 8]))],
        )
        assert parse_addresses(packet) == ["1.2.3.4", "5.6.7.8"]

    def test_other_record_types_are_skipped(self) -> None:
        from anchor.blocker.dnswire import parse_addresses

        cname = b"\x03www\x07example\x03com\x00"
        packet = self.answer("example.com", [(5, cname), (1, bytes([9, 9, 9, 9]))])
        assert parse_addresses(packet) == ["9.9.9.9"]

    def test_a_query_has_no_addresses(self) -> None:
        from anchor.blocker.dnswire import parse_addresses

        assert parse_addresses(query("example.com")) == []

    def test_rubbish_yields_nothing_rather_than_raising(self) -> None:
        from anchor.blocker.dnswire import parse_addresses

        for packet in (b"", b"\x00\x01", b"\xff" * 40):
            assert parse_addresses(packet) == []

    def test_a_truncated_record_keeps_what_was_read(self) -> None:
        from anchor.blocker.dnswire import parse_addresses

        packet = self.answer(
            "example.com",
            [(1, bytes([1, 2, 3, 4])), (1, bytes([5, 6, 7, 8]))],
        )
        assert parse_addresses(packet[:-3]) == ["1.2.3.4"]

    def test_a_lying_answer_count_does_not_spin_the_parser(self) -> None:
        from anchor.blocker.dnswire import parse_addresses

        packet = self.answer("example.com", [(1, bytes([1, 2, 3, 4]))])
        lying = packet[:6] + struct.pack(">H", 9999) + packet[8:]

        assert parse_addresses(lying) == ["1.2.3.4"]
