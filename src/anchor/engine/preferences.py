"""The settings a person may change (SPEC 7.2, 13, 14, 15).

Four numbers and a word, which is deliberately few. SPEC 7.2 makes the Firm
wait and the length of the random phrase configurable "never during a
session"; SPEC 13 makes the statistics retention configurable; SPEC 14 makes
the interface's language a choice between English and Spanish. Everything else
about Anchor's behaviour belongs to a profile or a schedule, where it can be
different on a Tuesday.

Two rules run through the whole module.

**Unset is not zero.** A preference nobody has touched is ``None``, and
``None`` means "whatever Anchor's own default is". Storing the default instead
would freeze today's default into a config file and make a better default in a
later version invisible to everyone who ever opened the Settings screen.

**Values arrive as text.** ``config.set`` carries a string, because that is
what a command line and a text box both have. Turning it into a number, and
refusing it when it is not one, happens here rather than in the two clients,
so that ``anchor config set`` and the Settings screen cannot disagree about
what is allowed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields, replace
from typing import Any, Final, Self

from anchor.engine.phrases import DEFAULT_PHRASE_LENGTH
from anchor.engine.sessions import DEFAULT_FIRM_WAIT_SECONDS, SessionPolicy
from anchor.engine.stats import DEFAULT_RETENTION_DAYS
from anchor.protocol.errors import AnchorError, ErrorCode

log = logging.getLogger("anchord")

#: Words people type when they mean a flag.
_TRUE: Final = frozenset({"1", "true", "yes", "on"})
_FALSE: Final = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class Setting:
    """One thing that can be set, and everything that constrains it."""

    key: str
    kind: type
    default: Any
    explain: str
    during_session: bool = True
    """Whether it may be changed while a session is running (SPEC 7.2)."""

    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    allow_empty: bool = False
    """For a choice that may also be nothing at all."""


PREFERENCES: Final[dict[str, Setting]] = {
    setting.key: setting
    for setting in (
        Setting(
            key="firm_wait_seconds",
            kind=int,
            default=DEFAULT_FIRM_WAIT_SECONDS,
            explain="How long a Firm session makes you wait before it lets go.",
            during_session=False,
            minimum=60,
            maximum=24 * 60 * 60,
        ),
        Setting(
            key="phrase_length",
            kind=int,
            default=DEFAULT_PHRASE_LENGTH,
            explain="How many characters the random phrase has.",
            during_session=False,
            minimum=20,
            maximum=1000,
        ),
        Setting(
            key="retention_days",
            kind=int,
            default=DEFAULT_RETENTION_DAYS,
            explain="How long statistics are kept before they are forgotten.",
            minimum=1,
            maximum=3650,
        ),
        Setting(
            key="language",
            kind=str,
            default="",
            explain="The interface language. Empty follows the desktop.",
            choices=("en", "es"),
            allow_empty=True,
        ),
        Setting(
            key="onboarding_done",
            kind=bool,
            default=False,
            explain="Whether the first-run introduction has been completed.",
        ),
    )
}


def _setting(key: str) -> Setting:
    setting = PREFERENCES.get(key)
    if setting is None:
        known = ", ".join(sorted(PREFERENCES))
        raise AnchorError(
            f"there is no setting called {key!r}. Anchor knows: {known}",
            code=ErrorCode.INVALID_CONFIG,
        )
    return setting


def parse_value(key: str, raw: str) -> Any:
    """Turn the text a client sent into a value, or refuse it and say why."""
    setting = _setting(key)
    text = raw.strip()

    if setting.kind is int:
        try:
            value: Any = int(text)
        except ValueError:
            raise AnchorError(
                f"{key} must be a whole number, not {raw!r}", code=ErrorCode.INVALID_CONFIG
            ) from None
    elif setting.kind is bool:
        lowered = text.lower()
        if lowered in _TRUE:
            value = True
        elif lowered in _FALSE:
            value = False
        else:
            raise AnchorError(
                f"{key} must be yes or no, not {raw!r}", code=ErrorCode.INVALID_CONFIG
            )
    else:
        value = text

    return _check(setting, value)


def _check(setting: Setting, value: Any) -> Any:
    """Hold a typed value against the limits, or refuse it."""
    key = setting.key
    if not isinstance(value, setting.kind) or isinstance(value, bool) is not (setting.kind is bool):
        raise AnchorError(f"{key} must be a {setting.kind.__name__}", code=ErrorCode.INVALID_CONFIG)

    if isinstance(value, int) and not isinstance(value, bool):
        if setting.minimum is not None and value < setting.minimum:
            raise AnchorError(
                f"{key} must be at least {setting.minimum}", code=ErrorCode.INVALID_CONFIG
            )
        if setting.maximum is not None and value > setting.maximum:
            raise AnchorError(
                f"{key} must be at most {setting.maximum}", code=ErrorCode.INVALID_CONFIG
            )

    if setting.choices and value not in setting.choices:
        if value == "" and setting.allow_empty:
            return value
        allowed = ", ".join(setting.choices)
        tail = ", or empty to follow the desktop" if setting.allow_empty else ""
        raise AnchorError(f"{key} must be one of: {allowed}{tail}", code=ErrorCode.INVALID_CONFIG)
    return value


@dataclass(frozen=True, slots=True)
class Preferences:
    """What the user has changed. ``None`` means "still the default"."""

    firm_wait_seconds: int | None = None
    phrase_length: int | None = None
    retention_days: int | None = None
    language: str | None = None
    onboarding_done: bool | None = None

    # -- reading and writing ---------------------------------------------

    def set(self, key: str, raw: str) -> Self:
        """This, with one setting changed. Refuses anything it cannot hold."""
        return replace(self, **{key: parse_value(key, raw)})

    def get(self, key: str) -> Any:
        """The effective value of one setting, default included."""
        setting = _setting(key)
        stored = getattr(self, key)
        return setting.default if stored is None else stored

    def to_dict(self) -> dict[str, Any]:
        """Only what was actually set, so defaults stay defaults."""
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if getattr(self, field.name) is not None
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Self:
        """Read stored preferences, forgiving anything that no longer parses.

        A config file that has been hand-edited into nonsense must not stop the
        engine from starting: a machine with no engine is a machine with no
        blocking, and P4 says the blocks are what matter.
        """
        kept: dict[str, Any] = {}
        for key, value in raw.items():
            setting = PREFERENCES.get(key)
            if setting is None:
                log.warning("ignoring unknown setting %r in the configuration", key)
                continue
            try:
                kept[key] = _check(setting, value)
            except AnchorError as error:
                log.warning("ignoring stored setting %r: %s", key, error.message)
        return cls(**kept)

    # -- what the rest of the engine asks for ----------------------------

    def policy(self, base: SessionPolicy) -> SessionPolicy:
        """``base`` with the two knobs SPEC 7.2 allows applied over it."""
        changes: dict[str, int] = {}
        if self.firm_wait_seconds is not None:
            changes["firm_wait_seconds"] = self.firm_wait_seconds
        if self.phrase_length is not None:
            changes["phrase_length"] = self.phrase_length
        return replace(base, **changes) if changes else base

    def retention(self, *, installed: int) -> int:
        """Days of statistics to keep (SPEC 13).

        ``anchor.toml`` sets the machine's default and the user's preference
        wins over it, so that the Settings screen is not quietly overruled by
        a file only root can edit.
        """
        return installed if self.retention_days is None else self.retention_days


def describe(
    preferences: Preferences,
    *,
    policy: SessionPolicy | None = None,
    installed_retention: int = DEFAULT_RETENTION_DAYS,
) -> list[dict[str, Any]]:
    """Every setting, its effective value, and whether it is still the default.

    One shape for ``anchor config get`` and for the Settings screen, so the
    two cannot describe the same number differently.
    """
    base = policy or SessionPolicy()
    defaults: dict[str, Any] = {
        "firm_wait_seconds": base.firm_wait_seconds,
        "phrase_length": base.phrase_length,
        "retention_days": installed_retention,
    }

    described: list[dict[str, Any]] = []
    for key, setting in PREFERENCES.items():
        stored = getattr(preferences, key)
        default = defaults.get(key, setting.default)
        described.append(
            {
                "key": key,
                "value": default if stored is None else stored,
                "is_default": stored is None,
                "explain": setting.explain,
                "during_session": setting.during_session,
            }
        )
    return described
