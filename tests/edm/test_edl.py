import re

from service_control_ioc.edm import edl
from service_control_ioc.ioc.mapping import ELEMENT_STATES, OVERALL_STATES


def test_led_has_one_rectangle_per_state() -> None:
    text = edl.led(10, 10, 16, 16, "P:STATE", edl.STATE_COLOURS)
    assert text.count("object activeRectangleClass") == len(ELEMENT_STATES) == 5
    assert re.findall(r'^visMin "(\d)"$', text, re.MULTILINE) == list("01234")
    assert re.findall(r'^visMax "(\d)"$', text, re.MULTILINE) == list("12345")
    assert text.count('visPv "P:STATE"') == 5


def test_colour_tables_cover_every_state() -> None:
    assert sorted(edl.STATE_COLOURS) == list(range(len(ELEMENT_STATES)))
    assert sorted(edl.OVERALL_COLOURS) == list(range(len(OVERALL_STATES)))


def test_update_follows_alarm_colour() -> None:
    text = edl.update(1, 2, 3, 4, "P:X")
    assert "object TextupdateClass" in text
    assert 'controlPv "P:X"' in text
    assert "\nfgAlarm\n" in text


def test_text_escapes_quotes() -> None:
    assert r'"say \"hi\""' in edl.text(0, 0, 50, 20, 'say "hi"')


def test_button_writes_one() -> None:
    text = edl.button(0, 0, 60, 20, "P:START", "Start")
    assert "object activeMessageButtonClass" in text
    assert 'pressValue "1"' in text
    assert 'controlPv "P:START"' in text


def test_screen_wraps_objects() -> None:
    text = edl.screen(900, 100, "T", [edl.exit_button(10, 10)])
    assert text.startswith("4 0 1\nbeginScreenProperties")
    assert "w 900\nh 100\n" in text
    assert 'title "T"' in text
    assert "activeExitButtonClass" in text


# The VM's /opt/QD/epics/edm/colors.list (qd-epics-edm 1.12.105) entries the
# LEDs use. EDM draws an "rgb" colour as the nearest palette entry, so a value
# that is not one is drawn in another colour.
PALETTE = {
    (40606, 40606, 40606),  # static 6 "Title"
    (65535, 52428, 0),  # static 38 "amber-38"
    (0, 65535, 0),  # static 15 "Green LED: On"
    (65535, 0, 0),  # static 20 "Red LED: On"
}


def test_led_colours_are_palette_entries() -> None:
    """Regression (Task 5, seen live): rgb 0 52224 0 was drawn as the palette's
    0 49344 0, so the ready LED was not the colour the screen asked for."""
    used = {*edl.STATE_COLOURS.values(), *edl.OVERALL_COLOURS.values()}
    assert used <= PALETTE, used - PALETTE
