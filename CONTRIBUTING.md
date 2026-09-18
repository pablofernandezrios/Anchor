# Contributing

Thanks for looking at Anchor.

## Ground rules

- **`SPEC.md` is the source of truth.** If a change needs to deviate from it,
  open an issue first and record the outcome as an ADR in `docs/adr/`.
- **Everything in English**: code, comments, commit messages, issues and docs.
  The user interface itself ships in English and Spanish.
- **[Conventional Commits](https://www.conventionalcommits.org/)** for commit
  messages, for example `feat(engine): add ratchet checks`.

## Development

Anchor targets Python 3.12 and above.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
```

## Architecture rules

- **Business logic lives in the engine.** The GUI, the CLI and the agent are
  thin clients that translate actions into IPC messages.
- **Root daemons use the standard library only** (`anchor.protocol`,
  `anchor.engine`, `anchor.blocker`). No pip packages run as root. A test
  enforces this; do not add an exception without an ADR.
- **Write tests first for engine logic**: sessions, ratchet, schedules, breaks
  and time handling. Coverage target for those modules is 80%.
- **Never log visited domains** outside the statistics database.

## Testing that needs privileges

Tests that need root, systemd or a display are marked and skipped elsewhere:

```sh
.venv/bin/pytest -m "not needs_root and not needs_systemd and not needs_display"
```

Anything that touches DNS, nftables, systemd units or browser policies belongs
in a disposable virtual machine, never on your own desktop.
