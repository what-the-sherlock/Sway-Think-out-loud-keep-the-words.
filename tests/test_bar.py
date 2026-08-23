"""The Sway Bar's pure logic.

Per CLAUDE.md, widgets are not unit tested -- the Tk plumbing in
:class:`~sway.bar.SwayBar` is wiring around a third-party event loop. What is
tested here is everything that can be reasoned about without a screen: where
the bar docks, where it can be clicked, what each state renders, and the
colour-key contract that makes the rounded shape possible at all.
"""

from __future__ import annotations

import pytest

from sway.bar import (
    ACTION_LANGUAGE,
    ACTION_MENU,
    ACTION_MIC,
    DEFAULT_MENU,
    SEPARATOR,
    BarState,
    HitRegion,
    MenuItem,
    menu_height,
    place,
    render_bar,
    render_dot,
    render_menu,
)
from sway.bar import _KEY_RGB, _MENU_ROW_H


def actions(regions):
    return [region.action for region in regions]


# -- placement -------------------------------------------------------------

FULL_HD = (0, 0, 1920, 1040)  # a work area, i.e. minus the taskbar


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        ("bottom-center", (890, 982)),
        ("top-center", (890, 14)),
        ("bottom-right", (1766, 982)),
        ("top-right", (1766, 14)),
    ],
)
def test_place_docks_to_the_right_corner(position, expected):
    assert place(position, (140, 44), FULL_HD) == expected


def test_place_respects_a_work_area_offset():
    """A taskbar on the left shifts the origin, not just the size."""
    area = (60, 0, 1920, 1040)
    x, _y = place("bottom-center", (140, 44), area)
    assert x == 60 + (1860 - 140) // 2


def test_place_keeps_the_bar_clear_of_the_taskbar():
    """The reference docks to the edge of the *usable* desktop, not the
    screen -- a bar behind the taskbar is a bar you cannot click."""
    _x, y = place("bottom-center", (140, 44), FULL_HD)
    assert y + 44 <= FULL_HD[3]


# -- the resting dot -------------------------------------------------------


def test_dot_is_small_and_square():
    image, regions = render_dot()
    assert image.width == image.height
    assert image.width <= 30, "the resting state must be easy to ignore"
    assert actions(regions) == [ACTION_MENU]


def test_dot_is_entirely_clickable():
    """A 14px target is fine to see and unpleasant to hit, so the whole box
    responds."""
    image, regions = render_dot()
    assert regions[0].contains(0, 0)
    assert regions[0].contains(image.width, image.height)


# -- the capsule -----------------------------------------------------------


@pytest.mark.parametrize("state", list(BarState))
def test_every_state_renders(state):
    image, regions = render_bar(state, "")
    assert image.height == 44
    assert image.width >= 132
    # The body is always a menu target, whatever else is on it.
    assert ACTION_MENU in actions(regions)


def test_idle_exposes_the_two_controls():
    """Language and microphone are the two things anyone changes mid-flow,
    and they are the only two that earn permanent space."""
    _image, regions = render_bar(BarState.IDLE, "", hint="Hold Right Ctrl")
    assert ACTION_LANGUAGE in actions(regions)
    assert ACTION_MIC in actions(regions)


@pytest.mark.parametrize(
    "state", [BarState.RECORDING, BarState.THINKING, BarState.INJECTING, BarState.DONE]
)
def test_controls_are_withdrawn_mid_dictation(state):
    """During a dictation the buttons would be a target nobody is aiming at,
    and the bar has something more useful to say."""
    _image, regions = render_bar(state, "")
    assert actions(regions) == [ACTION_MENU]


def test_control_regions_do_not_overlap():
    _image, regions = render_bar(BarState.IDLE, "", hint="Hold Right Ctrl")
    buttons = [r for r in regions if r.action != ACTION_MENU]
    assert len(buttons) == 2
    first, second = sorted(buttons, key=lambda r: r.x0)
    assert first.x1 <= second.x0


def test_control_regions_are_inside_the_image():
    image, regions = render_bar(BarState.IDLE, "", hint="Hold Right Ctrl")
    for region in regions:
        assert 0 <= region.x0 < region.x1 <= image.width
        assert 0 <= region.y0 < region.y1 <= image.height


def test_the_bar_grows_to_fit_its_text():
    short, _ = render_bar(BarState.DONE, "3 words")
    long, _ = render_bar(BarState.DONE, "3 words in a much longer confirmation line")
    assert long.width > short.width


def test_a_hint_replaces_the_state_label():
    """Coaching happens in place, at the moment of need, rather than in a
    wizard that takes the screen."""
    plain, _ = render_bar(BarState.IDLE, "")
    hinted, _ = render_bar(BarState.IDLE, "", hint="Next step: click a textbox")
    assert hinted.width > plain.width


def test_hover_changes_the_render():
    plain, _ = render_bar(BarState.IDLE, "", hint="x")
    hovered, _ = render_bar(BarState.IDLE, "", hint="x", hover=ACTION_MIC)
    assert plain.tobytes() != hovered.tobytes()


def test_level_only_shows_while_recording():
    quiet, _ = render_bar(BarState.RECORDING, "", level=0)
    loud, _ = render_bar(BarState.RECORDING, "", level=3)
    assert quiet.tobytes() != loud.tobytes()

    # Nothing else has a level to show, so nothing else changes with it.
    a, _ = render_bar(BarState.THINKING, "", level=0)
    b, _ = render_bar(BarState.THINKING, "", level=3)
    assert a.tobytes() == b.tobytes()


def test_recording_shows_an_elapsed_counter():
    """A progress indicator paired with the count that explains it."""
    a, _ = render_bar(BarState.RECORDING, "", elapsed_s=3)
    b, _ = render_bar(BarState.RECORDING, "", elapsed_s=75)
    assert a.width != b.width  # "0:03" versus "1:15"


def test_an_explicit_detail_wins_over_the_elapsed_counter():
    image, _ = render_bar(BarState.RECORDING, "on hold", elapsed_s=3)
    other, _ = render_bar(BarState.RECORDING, "", elapsed_s=3)
    assert image.tobytes() != other.tobytes()


# -- the colour-key contract -----------------------------------------------
#
# This is what makes the capsule a capsule. Windows' -transparentcolor is an
# exact match, so the corners have to come out of the renderer *exactly* on
# the key colour -- and they will not if the key is blended into the image
# before downsampling.


def test_corners_are_exactly_the_key_colour():
    image, _ = render_bar(BarState.IDLE, "", hint="Hold Right Ctrl")
    pixels = image.load()
    for x, y in ((0, 0), (image.width - 1, 0), (0, image.height - 1),
                 (image.width - 1, image.height - 1)):
        assert pixels[x, y][:3] == _KEY_RGB, f"corner {(x, y)} would not key out"


def test_the_capsule_interior_is_never_the_key_colour():
    """A hole punched through the middle of the bar would be worse than a
    square corner."""
    image, _ = render_bar(BarState.IDLE, "", hint="Hold Right Ctrl")
    pixels = image.load()
    mid_y = image.height // 2
    for x in range(30, image.width - 30):
        assert pixels[x, mid_y][:3] != _KEY_RGB


def test_the_dot_keys_out_most_of_its_box():
    """The resting state is a circle, not a square."""
    image, _ = render_dot()
    pixels = image.load()
    keyed = sum(
        1
        for y in range(image.height)
        for x in range(image.width)
        if pixels[x, y][:3] == _KEY_RGB
    )
    assert keyed / (image.width * image.height) > 0.4


# -- the menu --------------------------------------------------------------


def test_default_menu_matches_the_intended_shape():
    labels = [item.label for item in DEFAULT_MENU if not item.is_separator]
    assert labels == [
        "Hide for 1 hour",
        "Settings",
        "Language",
        "Microphone",
        "Transcript history",
        "Paste last transcript",
        "Copy last transcript",
    ]


def test_the_menu_is_grouped():
    """Five verbs in three groups: dismiss/configure, switch, recover."""
    assert sum(1 for item in DEFAULT_MENU if item.is_separator) == 2


def test_separators_are_not_clickable():
    _image, regions = render_menu(DEFAULT_MENU)
    assert "" not in actions(regions)
    assert len(regions) == sum(1 for item in DEFAULT_MENU if not item.is_separator)


def test_menu_rows_are_in_order_and_do_not_overlap():
    _image, regions = render_menu(DEFAULT_MENU)
    for earlier, later in zip(regions, regions[1:]):
        assert earlier.y1 <= later.y0


def test_menu_height_accounts_for_separators():
    without = menu_height((MenuItem("a", "A"), MenuItem("b", "B")))
    with_sep = menu_height((MenuItem("a", "A"), SEPARATOR, MenuItem("b", "B")))
    assert with_sep > without
    assert without == 2 * _MENU_ROW_H + 16


def test_menu_hover_changes_the_render():
    plain, _ = render_menu(DEFAULT_MENU)
    hovered, _ = render_menu(DEFAULT_MENU, hover="settings")
    assert plain.tobytes() != hovered.tobytes()


def test_menu_corners_key_out():
    image, _ = render_menu(DEFAULT_MENU)
    pixels = image.load()
    assert pixels[0, 0][:3] == _KEY_RGB


def test_the_menu_is_light_where_the_bar_is_dark():
    """Contrast inversion is what makes the panel read as sitting above the
    bar rather than being part of it."""
    menu, _ = render_menu(DEFAULT_MENU)
    bar, _ = render_bar(BarState.IDLE, "", hint="x")
    menu_mid = menu.load()[menu.width // 2, menu.height // 2][:3]
    bar_mid = bar.load()[bar.width // 2, bar.height // 2][:3]
    assert sum(menu_mid) > sum(bar_mid)


def test_an_empty_menu_still_renders():
    image, regions = render_menu(())
    assert image.width > 0
    assert regions == ()


# -- hit regions -----------------------------------------------------------


def test_hit_region_contains_is_inclusive_of_its_edges():
    region = HitRegion(10, 20, 30, 40, "x")
    assert region.contains(10, 20)
    assert region.contains(30, 40)
    assert region.contains(20, 30)
    assert not region.contains(9, 30)
    assert not region.contains(31, 30)
