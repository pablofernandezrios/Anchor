"""``anchor``: the command line (SPEC 15).

Full parity with the graphical interface is the rule (P1), so every command
here maps onto the same engine request the interface sends. Output is readable
by default and machine-readable with ``--json``; exit codes are stable and
documented in ``docs/cli.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from anchor import __version__
from anchor.cli.client import EngineClient, EngineUnreachableError
from anchor.cli.durations import DurationError, format_countdown, format_duration, parse_duration
from anchor.engine.paths import Paths
from anchor.protocol.errors import ErrorCode
from anchor.protocol.messages import Response

# Exit codes. Documented in docs/cli.md; do not renumber.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NO_SESSION = 3
EXIT_REFUSED = 4
EXIT_UNAUTHORIZED = 5
EXIT_UNREACHABLE = 6
EXIT_NOT_YET = 7

_EXIT_FOR_CODE: dict[str, int] = {
    ErrorCode.NO_ACTIVE_SESSION: EXIT_NO_SESSION,
    ErrorCode.RATCHET_VIOLATION: EXIT_REFUSED,
    ErrorCode.CANCEL_FORBIDDEN: EXIT_REFUSED,
    ErrorCode.SKIP_FORBIDDEN: EXIT_REFUSED,
    ErrorCode.SKIP_LIMIT_REACHED: EXIT_REFUSED,
    ErrorCode.DURATION_TOO_LONG: EXIT_REFUSED,
    ErrorCode.UNAUTHORIZED: EXIT_UNAUTHORIZED,
    ErrorCode.WAIT_NOT_ELAPSED: EXIT_NOT_YET,
    ErrorCode.PHRASE_MISMATCH: EXIT_NOT_YET,
    ErrorCode.VALVE_NOT_REQUESTED: EXIT_NOT_YET,
}


def _clock(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


class _Commands:
    """``add_subparsers`` with the shared options attached to every command."""

    def __init__(self, actions: Any, common: argparse.ArgumentParser) -> None:
        self._actions = actions
        self._common = common

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        parser: argparse.ArgumentParser = self._actions.add_parser(
            name, parents=[self._common], **kwargs
        )
        return parser

    def add_subparsers(self, **kwargs: Any) -> _Commands:
        return _Commands(self._actions.add_subparsers(**kwargs), self._common)


def _everywhere() -> argparse.ArgumentParser:
    """The options SPEC 15 puts on every command.

    Held apart and given to each subcommand as well as to the top level, so
    that both ``anchor --json status`` and ``anchor status --json`` work.
    People type the second one, and argparse would otherwise answer it with a
    usage error.

    ``SUPPRESS`` is the important part: without it every subcommand would
    carry its own default of False and quietly overwrite a ``--json`` given
    before the command.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print the raw engine reply.",
    )
    # Not in the specification's list, but it would be strange for one global
    # option to work after the command and the other not to.
    common.add_argument(
        "--root",
        default=argparse.SUPPRESS,
        help="Talk to an engine running on a relocated tree.",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _everywhere()
    parser = argparse.ArgumentParser(
        prog="anchor",
        description="Focus sessions that are hard to walk away from.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"anchor {__version__}")

    commands = _Commands(parser.add_subparsers(dest="command", required=True), common)

    commands.add_parser("status", help="Show the active session, if there is one.")

    start = commands.add_parser("start", help="Start a session.")
    start.add_argument("--profile", required=True, help="Which profile to use.")
    start.add_argument(
        "--duration", required=True, help="How long, for example 2h30m. At most 8h at start."
    )
    start.add_argument(
        "--level",
        required=True,
        choices=("soft", "firm", "strict"),
        help="How hard the session is to leave.",
    )
    start.add_argument(
        "--valve",
        choices=("wait", "phrase", "both"),
        help="The emergency exit. Required for Strict, and only for Strict.",
    )

    extend = commands.add_parser("extend", help="Make the active session longer.")
    extend.add_argument("--by", required=True, help="How much longer, for example 30m.")

    cancel = commands.add_parser("cancel", help="Leave a Soft or Firm session early.")
    cancel.add_argument("--phrase", help="The phrase Anchor generated. Firm sessions ask for one.")
    cancel.add_argument("--withdraw", action="store_true", help="Change your mind about leaving.")

    profile = commands.add_parser("profile", help="The named sets of rules sessions use.")
    profile_actions = _Commands(profile.add_subparsers(dest="action", required=True), common)
    profile_actions.add_parser("list", help="List the profiles you have.")

    show = profile_actions.add_parser("show", help="Show what a profile blocks.")
    show.add_argument("name")

    create = profile_actions.add_parser("create", help="Make a new profile.")
    create.add_argument("name")
    create.add_argument(
        "--mode",
        choices=("blocklist", "allowlist"),
        help="Block what is listed, or block everything else.",
    )
    create.add_argument("--domain", action="append", default=[], help="May be repeated.")
    create.add_argument("--app", action="append", default=[], help="May be repeated.")

    edit = profile_actions.add_parser(
        "edit",
        help="Change a profile. During a session only stricter changes are accepted.",
    )
    edit.add_argument("name")
    edit.add_argument("--mode", choices=("blocklist", "allowlist"))
    edit.add_argument("--add-domain", action="append", default=[], help="May be repeated.")
    edit.add_argument("--remove-domain", action="append", default=[], help="May be repeated.")
    edit.add_argument("--add-app", action="append", default=[], help="May be repeated.")
    edit.add_argument("--remove-app", action="append", default=[], help="May be repeated.")
    edit.add_argument(
        "--block-vpn",
        dest="block_vpn",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Block VPN and Tor in Strict sessions. Only changeable before one starts.",
    )

    delete = profile_actions.add_parser("delete", help="Remove a profile.")
    delete.add_argument("name")

    category = commands.add_parser(
        "category", help="The shipped bundles of domains and applications."
    )
    category_actions = _Commands(category.add_subparsers(dest="action", required=True), common)
    category_actions.add_parser("list", help="List the categories installed here.")
    category_show = category_actions.add_parser("show", help="Show what a category covers.")
    category_show.add_argument("name", help="The category's identifier, as `list` prints it.")

    stats = commands.add_parser("stats", help="What Anchor has been doing (SPEC 13).")
    stats.add_argument("--day", dest="range", action="store_const", const="day", help="Today.")
    stats.add_argument(
        "--week", dest="range", action="store_const", const="week", help="This week."
    )
    stats.add_argument(
        "--month", dest="range", action="store_const", const="month", help="This month."
    )
    stats.add_argument(
        "--delete",
        action="store_true",
        help="Delete every statistic. Asks first, and cannot be undone.",
    )

    breaks = commands.add_parser(
        "break", help="The break that is running, if the profile lets you avoid it."
    )
    break_actions = _Commands(breaks.add_subparsers(dest="action", required=True), common)
    break_actions.add_parser("skip", help="Give it up. Flexible profiles only.")
    break_actions.add_parser("postpone", help="Push it back five minutes.")

    valve = commands.add_parser("valve", help="The emergency exit from a Strict session.")
    valve_actions = _Commands(valve.add_subparsers(dest="action", required=True), common)
    valve_actions.add_parser("request", help="Ask to be let out.")
    valve_actions.add_parser("withdraw", help="Take the request back.")
    phrase = valve_actions.add_parser("phrase", help="Type the phrase Anchor generated.")
    phrase.add_argument("text", nargs="?", help="The phrase. Omit it to be prompted.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return _run(argv)
    except BrokenPipeError:
        # `anchor status | head` closes the pipe while we are still writing.
        # Python would otherwise print a traceback at exit, when flushing
        # stdout fails again. Pointing stdout at /dev/null first lets the
        # interpreter shut down quietly, which is what every other command
        # line tool does.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        return 128 + signal.SIGPIPE
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 128 + signal.SIGINT


def parse_args(argv: Sequence[str] | None) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    """Parse, and fill in the options that were not given anywhere.

    The shared options are declared with ``SUPPRESS`` so that a value given
    before the command is not overwritten by a subcommand's default. Nothing
    then sets them when they are absent from both, so it is done here.

    They cannot be given a default with ``set_defaults`` instead: that method
    writes the default onto the action object, and ``parents=`` shares one
    action between the top level and every subcommand, so it would put the
    overwriting default back on all of them. That is the bug this replaced —
    ``anchor --json stats`` quietly printed the human output.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, fallback in (("json", False), ("root", None)):
        if not hasattr(args, name):
            setattr(args, name, fallback)
    return parser, args


def _run(argv: Sequence[str] | None) -> int:
    parser, args = parse_args(argv)

    try:
        type_, payload = _request_for(args)
    except (DurationError, ValueError) as error:
        # argparse exits with the usage code (2) on its own.
        parser.error(str(error))

    paths = Paths.resolve(args.root)
    try:
        with EngineClient(paths.engine_socket) as client:
            response = client.call(type_, payload)
    except EngineUnreachableError as error:
        print(f"anchor: {error}", file=sys.stderr)
        return EXIT_UNREACHABLE

    if args.json:
        print(json.dumps(response.to_dict(), indent=2, ensure_ascii=False))
        return EXIT_OK if response.ok else _exit_code(response)

    if not response.ok:
        print(f"anchor: {response.error.get('message', 'the engine refused')}", file=sys.stderr)
        return _exit_code(response)

    return _render(args, response.result)


def _request_for(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    match args.command:
        case "status":
            return "status.get", {}

        case "start":
            if args.level == "strict" and not args.valve:
                raise ValueError("a Strict session needs --valve, because it cannot be cancelled")
            if args.level != "strict" and args.valve:
                raise ValueError("--valve applies to Strict sessions only")
            payload: dict[str, Any] = {
                "profile": args.profile,
                "duration_seconds": parse_duration(args.duration),
                "level": args.level,
            }
            if args.valve:
                payload["valve"] = args.valve
            return "session.start", payload

        case "extend":
            return "session.extend", {"by_seconds": parse_duration(args.by)}

        case "cancel":
            if args.withdraw:
                return "session.withdraw_cancel", {}
            return "session.cancel", ({"typed": args.phrase} if args.phrase else {})

        case "profile":
            return _profile_request(args)

        case "category":
            # One request for both actions: the list is small, and asking the
            # engine to filter it would be a request that adds nothing.
            return "category.list", {}

        case "break":
            return f"break.{args.action}", {}

        case "stats":
            if args.delete:
                _confirm_deletion()
                return "stats.delete", {}
            return "stats.query", {"range": args.range or "week"}

        case "valve":
            match args.action:
                case "request":
                    return "valve.request", {}
                case "withdraw":
                    return "valve.withdraw", {}
                case "phrase":
                    text = args.text if args.text is not None else _prompt_for_phrase()
                    return "valve.phrase", {"text": text}

    raise ValueError(f"unknown command {args.command!r}")


def _profile_request(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    match args.action:
        case "list":
            return "profile.list", {}
        case "show":
            return "profile.show", {"name": args.name}
        case "delete":
            return "profile.delete", {"name": args.name}
        case "create":
            payload: dict[str, Any] = {"name": args.name}
            if args.mode:
                payload["web_mode"] = args.mode
            if args.domain:
                payload["domains"] = args.domain
            if args.app:
                payload["apps"] = args.app
            return "profile.create", payload
        case "edit":
            changes: dict[str, Any] = {"name": args.name}
            for option, field in (
                ("mode", "web_mode"),
                ("add_domain", "add_domains"),
                ("remove_domain", "remove_domains"),
                ("add_app", "add_apps"),
                ("remove_app", "remove_apps"),
            ):
                value = getattr(args, option)
                if value:
                    changes[field] = value
            if args.block_vpn is not None:
                changes["block_vpn_and_tor"] = args.block_vpn
            if len(changes) == 1:
                raise ValueError("nothing to change; pass at least one option")
            return "profile.edit", changes

    raise ValueError(f"unknown profile action {args.action!r}")


def _confirm_deletion() -> None:
    """Ask before deleting a history that cannot be recovered (SPEC 13).

    One action deletes all statistics, and this is that action, so it says
    plainly what it is about to do. Anchor keeps no copy: there is nothing to
    restore from, by design, because a private record that quietly survives
    its own deletion is not private.
    """
    print("This deletes every statistic Anchor has recorded. It cannot be undone.")
    print("Type 'delete' to go ahead:")
    if sys.stdin.readline().strip().lower() != "delete":
        raise ValueError("nothing was deleted")


def _prompt_for_phrase() -> str:
    """Read the phrase from the terminal (SPEC 7.5).

    The interface disables pasting; a terminal cannot, so the phrase is simply
    read from standard input.
    """
    print("Type the phrase Anchor gave you, then press Enter:")
    return sys.stdin.readline().rstrip("\n")


def _exit_code(response: Response) -> int:
    return _EXIT_FOR_CODE.get(response.code or "", EXIT_ERROR)


def _render(args: argparse.Namespace, result: dict[str, Any]) -> int:
    """Print the answer, and say how the command ended (SPEC 15)."""
    if args.command == "cancel" and result.get("ended"):
        print("Session ended.")
        return EXIT_OK
    if args.command == "valve" and result.get("ended"):
        print("Session ended through the emergency valve. This was recorded.")
        return EXIT_OK
    if args.command == "profile":
        _render_profile(args, result)
        return EXIT_OK
    if args.command == "category":
        return _render_category(args, result)
    if args.command == "stats":
        _render_stats(result)
        return EXIT_OK
    _render_status(result)
    return EXIT_OK


def _render_stats(result: dict[str, Any]) -> None:
    """The statistics page, as far as a terminal can draw it (SPEC 13, 15)."""
    if result.get("deleted"):
        print("Every statistic has been deleted.")
        return

    print(f"{result['first_day']} to {result['last_day']}\n")
    print(f"  Focus                {format_duration(result['focus_seconds'])}")
    print(f"  Sessions completed   {result['sessions_completed']}")
    print(f"  Blocked attempts     {result['blocked_attempts']}")
    print(f"  Ruptures             {result['ruptures']}")

    attempts = result.get("attempts_by_target") or []
    if attempts:
        print("\nBlocked most often")
        for entry in attempts:
            name = entry["target"] + (" (app)" if entry["kind"] == "app" else "")
            print(f"  {name:<40} {entry['count']}")

    breaks = result.get("breaks") or {}
    if breaks:
        print(
            f"\nBreaks: {breaks.get('taken', 0)} taken · "
            f"{breaks.get('postponed', 0)} postponed · {breaks.get('skipped', 0)} skipped"
        )

    ruptures = result.get("ruptures_by_kind") or {}
    if ruptures:
        print(
            f"Ruptures: {ruptures.get('valve', 0)} valve · "
            f"{ruptures.get('skip', 0)} skipped schedule · "
            f"{ruptures.get('tampering', 0)} tampering"
        )


def _render_category(args: argparse.Namespace, result: dict[str, Any]) -> int:
    categories = result.get("categories") or []
    if args.action == "list":
        if not categories:
            print("No categories are installed.")
            return EXIT_OK
        for category in categories:
            print(
                f"{category['id']:<12} {category['name']} "
                f"({len(category['domains'])} domains, {len(category['apps'])} apps)"
            )
        return EXIT_OK

    for category in categories:
        if category["id"] == args.name:
            print(f"{category['name']} ({category['id']})")
            for label, key in (("Domains", "domains"), ("Apps", "apps")):
                values = category.get(key) or []
                print(f"  {label}: {', '.join(values) if values else '-'}")
            return EXIT_OK

    # A name nothing answers to is the user's mistake, and a script that pipes
    # this needs to hear about it in the exit code as well as on stderr.
    print(f"anchor: no category called {args.name!r} is installed", file=sys.stderr)
    return EXIT_ERROR


def _render_profile(args: argparse.Namespace, result: dict[str, Any]) -> None:
    if args.action == "list":
        profiles = result.get("profiles") or []
        print("\n".join(profiles) if profiles else "No profiles yet.")
        return

    if args.action == "delete":
        print(f"Deleted {result.get('deleted')}.")
        return

    profile = result.get("profile") or {}
    print(f"{profile.get('name')} · {profile.get('web_mode')}")

    for label, key in (("Categories", "categories"), ("Domains", "domains"), ("Apps", "apps")):
        values = profile.get(key) or []
        if values:
            print(f"  {label}: {', '.join(values)}")

    breaks = profile.get("breaks") or {}
    if breaks:
        print(
            f"  Breaks: {breaks.get('work_minutes')}/{breaks.get('break_minutes')} · "
            f"{breaks.get('type')} · {breaks.get('hardness')}"
        )
    if not profile.get("block_vpn_and_tor", True):
        print("  VPN and Tor: allowed even in Strict sessions")


def _render_status(result: dict[str, Any]) -> None:
    if not result.get("active"):
        print("No session is running.")
        profiles = result.get("profiles") or []
        if profiles:
            print(f"Profiles: {', '.join(profiles)}")
        print(f"Schedule skips left this week: {result.get('skips_remaining', 0)} of 3")
        return

    remaining = float(result.get("remaining_seconds", 0))
    print(f"{result['profile']} · {str(result['level']).capitalize()}")
    print(f"{format_countdown(remaining)} remaining · ends at {_clock(result['ends_at'])}")
    print(f"Blocked attempts: {result.get('blocked_attempts', 0)}")

    pending = result.get("exit_request")
    if pending:
        wait = float(pending.get("remaining_wait_seconds", 0))
        if wait > 0:
            print(f"\nLeaving in {format_duration(wait)}, unless you withdraw the request.")
        elif pending.get("phrase"):
            print("\nThe wait is over. Type this phrase to leave:\n")
            print(f"  {pending['phrase']}\n")
            print("  anchor valve phrase   (or: anchor cancel --phrase '...')")
        else:
            print("\nThe wait is over; the session is ending.")


if __name__ == "__main__":
    sys.exit(main())
