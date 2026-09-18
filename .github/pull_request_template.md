## What this changes

<!-- A short description of the change and why it is needed. -->

## Specification

<!-- Which part of SPEC.md this implements, for example "SPEC 7.4 ratchet". -->

- Implements:
- Deviates from the spec: no <!-- if yes, link the ADR in docs/adr/ -->

## Testing

<!-- How you verified this. Engine logic needs tests written first. -->

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy` passes
- [ ] `pytest` passes
- [ ] Anything touching DNS, nftables, systemd or browser policies was tested in
      a disposable virtual machine, not on a real desktop

## Checklist

- [ ] Commits follow Conventional Commits
- [ ] `docs/architecture.md` still matches the code
- [ ] `CHANGELOG.md` updated
- [ ] No visited domains are logged outside the statistics database
