"""Anchor: a focus and blocking tool for the Linux desktop.

Layout follows the privilege boundary described in SPEC 5.1:

``anchor.protocol``
    Shared IPC message definitions. Imported by every component.
``anchor.engine``
    ``anchord``. Runs as root. Sole owner of config, state and statistics.
``anchor.blocker``
    ``anchor-blockerd``. Runs as root. DNS, nftables, policies, app enforcement.
``anchor.agent``
    ``anchor-agent``. Runs in the user session. Notifications, breaks, indicator.
``anchor.gui``
    ``anchor-gui``. GTK4 client, no business logic.
``anchor.cli``
    ``anchor``. Thin IPC client, no business logic.

``protocol``, ``engine`` and ``blocker`` are restricted to the Python standard
library (SPEC 5.4) and this is enforced by ``tests/unit/test_stdlib_only.py``.
"""

__version__ = "0.1.0"
