"""Fixed names and paths the blocker uses (SPEC 8.2).

These are constants rather than settings because restoration has to find them
without the journal. If Anchor is removed while its record of what it changed
is damaged, the removal script can still delete a table it knows the name of.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: The single nftables table Anchor owns. Deleting it removes every rule.
NFT_TABLE: Final = "anchor"
NFT_FAMILY: Final = "inet"

#: The firewall mark the resolver puts on its own upstream sockets, so the
#: redirect skips them (Milestone 0 spike 2). Setting it needs CAP_NET_ADMIN,
#: which is why an ordinary process cannot claim the exemption.
ANCHOR_MARK: Final = 0x616E  # "an"

#: Where Anchor's resolver listens. Above 1024 so it needs no extra privilege,
#: and fixed so restoration and `anchor doctor` can find it.
RESOLVER_PORT: Final = 5391
RESOLVER_ADDRESS: Final = "127.0.0.1"

#: systemd-resolved configuration goes under /run, never /etc.
#:
#: This is what makes Anchor fail open (P4). A drop-in in /etc pointing DNS at
#: a resolver that is not running would leave the machine without name
#: resolution until someone found and deleted the file. Under /run it is gone
#: at the next boot, so the worst case is one restart rather than a broken
#: machine.
RESOLVED_RUNTIME_DIR: Final = Path("/run/systemd/resolved.conf.d")
RESOLVED_DROP_IN: Final = RESOLVED_RUNTIME_DIR / "50-anchor.conf"

#: The runtime drop-ins the engine writes to refuse a manual stop (SPEC 5.3).
#: Removal clears them, or the services could not be stopped to be removed.
SYSTEMD_RUNTIME_DIR: Final = Path("/run/systemd/system")
REFUSE_STOP_UNITS: Final = ("anchord.service", "anchor-blockerd.service")
REFUSE_STOP_FILENAME: Final = "anchor-session.conf"

#: Where the record of applied changes lives.
JOURNAL_NAME: Final = "applied.json"
