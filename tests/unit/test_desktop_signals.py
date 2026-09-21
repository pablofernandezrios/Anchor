"""Which signals the panel is sent, and under which names (SPEC 14.1).

The D-Bus layer cannot be run here — there is no session bus and no
PyGObject — so the one part of it that has already been wrong is kept out of
the untestable half. The label stopped counting down on a real desktop because
the signal carried the wrong name; these tests are what would have caught it.
"""

from __future__ import annotations

import dataclasses

from anchor.agent.desktop import ITEM_XML, SIGNAL_TYPES, signals_for, status_word
from anchor.agent.indicator import IndicatorModel, IndicatorView

RUNNING = IndicatorView(visible=True, icon="alarm-symbolic", label="2:14", tooltip="Study")


def names(before: IndicatorView, after: IndicatorView) -> list[str]:
    return [signal for signal, _ in signals_for(before, after)]


class TestTheLabelSignal:
    def test_it_is_named_after_its_property(self) -> None:
        """The panel takes "XAyatanaNew" off the signal to get the property.

        A signal called NewLabel asks it to re-read a property called Label,
        which does not exist in this interface, so nothing happens and the
        countdown sits still — which is exactly what the first run on a real
        desktop showed.
        """
        changed = dataclasses.replace(RUNNING, label="2:13")

        assert names(RUNNING, changed) == ["XAyatanaNewLabel"]

    def test_it_carries_the_label_and_the_guide(self) -> None:
        changed = dataclasses.replace(RUNNING, label="1:00")

        assert signals_for(RUNNING, changed) == [("XAyatanaNewLabel", ("1:00", RUNNING.guide))]

    def test_the_interface_declares_it(self) -> None:
        """A signal that is not in the introspection XML cannot be emitted."""
        assert "XAyatanaNewLabel" in ITEM_XML
        assert '<signal name="NewLabel"' not in ITEM_XML

    def test_every_signal_that_can_be_sent_has_a_declared_type(self) -> None:
        for signal in SIGNAL_TYPES:
            assert f'name="{signal}"' in ITEM_XML


class TestOnlyWhatChanged:
    def test_an_unchanged_view_sends_nothing(self) -> None:
        """A panel told that everything changed re-reads everything, every second."""
        assert signals_for(RUNNING, RUNNING) == []

    def test_a_new_icon_is_announced(self) -> None:
        changed = dataclasses.replace(RUNNING, icon="dialog-question-symbolic")

        assert names(RUNNING, changed) == ["NewIcon"]

    def test_a_new_tooltip_is_announced(self) -> None:
        changed = dataclasses.replace(RUNNING, tooltip="Study · 1:00 left")

        assert names(RUNNING, changed) == ["NewToolTip"]

    def test_appearing_and_disappearing_are_status_changes(self) -> None:
        hidden = IndicatorView(visible=False)

        assert "NewStatus" in names(hidden, RUNNING)
        assert "NewStatus" in names(RUNNING, hidden)

    def test_the_status_word_is_the_one_the_panel_expects(self) -> None:
        assert status_word(RUNNING) == "Active"
        assert status_word(IndicatorView(visible=False)) == "Passive"

    def test_a_session_starting_announces_everything_that_moved(self) -> None:
        assert set(names(IndicatorView(visible=False), RUNNING)) == {
            "XAyatanaNewLabel",
            "NewToolTip",
            "NewStatus",
        }


class TestAMinuteOfTicking:
    def test_each_new_minute_is_one_signal(self) -> None:
        """Once a minute, not once a second: the label is minutes (SPEC 14.1)."""
        model = IndicatorModel()
        sent: list[str] = []
        previous = IndicatorView()

        for second in range(180):
            model.update_status(
                {
                    "active": True,
                    "profile": "Study",
                    "level": "firm",
                    "phase": "working",
                    "ends_at": 0.0,
                    "remaining_seconds": 3600 - second,
                    "blocked_attempts": 0,
                    "app_blocks": 0,
                }
            )
            view = model.view
            sent.extend(signal for signal, _ in signals_for(previous, view))
            previous = view

        labels = [signal for signal in sent if signal == "XAyatanaNewLabel"]
        # 1:00 at the start, then 0:59 and 0:58 as each minute boundary passes.
        assert len(labels) == 3, sent
