# systemd units

Three units, matching the components in SPEC 5.1.

| Unit | Scope | Runs as |
|---|---|---|
| `anchord.service` | system | root |
| `anchor-blockerd.service` | system | root |
| `anchor-agent.service` | user | the owner |

Both root units use `Restart=always` with `RestartSec=1`, as SPEC 5.3 requires,
so that a crash during an active block leaves the block standing and systemd
brings the daemon back (P4). `StartLimitIntervalSec=0` is deliberate: the
default start limit would let systemd give up after a handful of rapid
failures, which is precisely the case where the block must not disappear.

## The refusal drop-in

While a session is active the engine writes

```
/run/systemd/system/anchord.service.d/anchor-session.conf
/run/systemd/system/anchor-blockerd.service.d/anchor-session.conf
```

each containing `RefuseManualStop=yes`, and reloads systemd. The drop-ins are
removed when the session ends. They live under `/run`, so a reboot clears them
and the engine writes them again if a session is still running (SPEC 5.3).

Writing this at runtime rather than shipping it in the unit is what lets
`systemctl stop` work normally outside sessions, which uninstalling depends on
(SPEC 7.6). Milestone 0 validates that a runtime drop-in is honoured; see
`docs/spikes/`.
