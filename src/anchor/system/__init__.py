"""Things both root daemons need (SPEC 5.1).

The engine and the blocker are separate processes with separate jobs, and
neither should import the other: the engine decides and the blocker executes,
and a dependency in either direction blurs that. What they genuinely share
lives here.

Like the daemons themselves, this is restricted to the standard library
(SPEC 5.4), and ``tests/unit/test_stdlib_only.py`` enforces it.
"""
