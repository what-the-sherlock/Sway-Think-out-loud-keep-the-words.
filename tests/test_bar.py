"""The Sway Bar's pure logic.

Per CLAUDE.md, widgets are not unit tested -- the Tk plumbing in
:class:`~sway.bar.SwayBar` is wiring around a third-party event loop. What is
tested here is everything that can be reasoned about without a screen: where
the bar docks, where it can be clicked, what each state renders, and the
colour-key contract that makes the rounded shape possible at all.
"""

from __future__ import annotations

import time

import pytest

from sway.bar import (
    ACTION_COPY,
    ACTION_LANGUAGE,
    ACTION_MENU,
    ACTION_MIC,
    ACTION_PASTE,
    ACTION_RESUME,
    ACTION_SETTINGS,
    DEFAULT_MENU,
    SEPARATOR,
    BarState,
    HintTone,
    HitRegion,
    MenuItem,
    build_menu,
    clamp_to_area,
    measure_rich,
    menu_height,
    natural_width,
    place,
    recovery_for,
    render_bar,
    render_dot,
    render_menu,
    render_tooltip,
    rich_segments,
)
from sway.bar import SwayBar
from sway.bar import (
    _HEIGHT,
    _KEY_RGB,
    _MENU_ROW_H,
    _REST_H,
    _REST_PAD,
    _REST_W,
    _font,
    _font_for,
)


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


def test_the_resting_tray_is_small_and_says_nothing():
    """At rest the bar is an empty outline. A filled shape is a coloured
    pixel competing for attention all day, and there is nothing at rest worth
    competing for."""
    image, regions = render_dot()
    assert image.size == (_REST_W + 2 * _REST_PAD, _REST_H + 2 * _REST_PAD)
    assert image.height <= 24, "the resting state must be easy to ignore"
    assert actions(regions) == [ACTION_MENU]


def test_the_resting_tray_reads_as_empty_without_being_click_through():
    """The tray has to *look* like an outline and still *behave* like a
    target.

    An earlier version of this test asserted the interior was exactly the key
    colour, which is the same thing as asserting the bar cannot be clicked --
    Windows hit-tests a colour-keyed window by its transparency, so those
    pixels went to the window behind. The interior is now a very dark scrim:
    nothing to look at on a dark desktop, but solid to the mouse.
    """
    image, _ = render_dot()
    pixels = image.load()
    centre_row = [
        pixels[x, image.height // 2][:3]
        for x in range(image.width // 3, 2 * image.width // 3)
    ]
    assert all(pixel != _KEY_RGB for pixel in centre_row), "would be click-through"
    assert all(sum(pixel) < 90 for pixel in centre_row), "too solid to read as empty"


def test_the_resting_tray_still_draws_an_outline():
    image, _ = render_dot()
    pixels = image.load()
    column = [pixels[image.width // 2, y][:3] for y in range(image.height)]
    assert any(pixel != _KEY_RGB for pixel in column), "no outline at all"


def test_the_resting_outline_reads_on_a_light_desktop_and_a_dark_one():
    """It floats over a desktop we do not control. A single hairline can only
    contrast with backgrounds it happens to differ from, so the stroke is a
    dark tone just outside a light one."""
    image, _ = render_dot()
    pixels = image.load()
    column = [
        pixels[image.width // 2, y][:3]
        for y in range(image.height)
        if pixels[image.width // 2, y][:3] != _KEY_RGB
    ]
    assert column, "no stroke"
    assert max(sum(p) for p in column) > 330, "nothing light enough for a dark desktop"
    assert min(sum(p) for p in column) < 130, "nothing dark enough for a light desktop"


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


def test_the_resting_tray_is_a_rounded_shape_not_a_rectangle():
    """The corners have to key out, or the bar is a box.

    Deliberately checks the corners rather than a share of the whole window:
    the tray now fills most of a tight box by design, so a percentage
    threshold would only measure the padding.
    """
    image, _ = render_dot()
    pixels = image.load()
    for x, y in (
        (0, 0),
        (image.width - 1, 0),
        (0, image.height - 1),
        (image.width - 1, image.height - 1),
    ):
        assert pixels[x, y][:3] == _KEY_RGB, f"corner {(x, y)} would not key out"


# -- the menu --------------------------------------------------------------


def test_default_menu_matches_the_intended_shape():
    labels = [item.label for item in DEFAULT_MENU if not item.is_separator]
    assert labels == [
        "Pause dictation",
        "Hide for 1 hour",
        "Settings",
        "Language",
        "Microphone",
        "Transcript history",
        "Paste last transcript",
        "Copy last transcript",
    ]


def test_the_menu_is_grouped():
    """Six verbs in three groups: arm/dismiss/configure, switch, recover."""
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


# -- the morph -------------------------------------------------------------
#
# The dot and the capsule are one renderer, so the interesting cases are the
# frames in between: they are what the user actually watches, and they are
# where a layout written for only the two endpoints falls apart.


def test_expand_runs_from_the_resting_tray_to_the_capsule():
    rest, _ = render_bar(BarState.IDLE, "", expand=0.0)
    full, _ = render_bar(BarState.IDLE, "", expand=1.0)
    assert rest.size == (_REST_W + 2 * _REST_PAD, _REST_H + 2 * _REST_PAD)
    assert full.height == _HEIGHT
    assert full.width == natural_width(BarState.IDLE, "")


def test_render_dot_is_the_collapsed_capsule():
    """One renderer, so the two names cannot drift apart visually."""
    named, named_regions = render_dot()
    collapsed, collapsed_regions = render_bar(BarState.IDLE, "", expand=0.0)
    assert named.tobytes() == collapsed.tobytes()
    assert actions(named_regions) == actions(collapsed_regions)


@pytest.mark.parametrize("expand", [0.6, 0.75, 0.9, 1.0])
def test_the_morph_never_punches_a_hole_in_itself(expand):
    """Once the surface has wiped in, the key colour anywhere inside the body
    would key out to a transparent slit straight through the bar. Checked
    across the frames the user watches, not only at the settled end.

    Below ~0.5 the interior is *meant* to be key -- that is the resting tray
    being see-through -- so those frames are covered by
    test_the_resting_tray_is_see_through instead."""
    image, _ = render_bar(BarState.IDLE, "", expand=expand)
    pixels = image.load()
    # Strictly inside the rounded body: from the left cap's centre to the
    # right cap's centre, across the middle third. Everything outside that is
    # allowed -- and required -- to be key.
    half = image.height / 2
    for y in range(round(image.height * 0.35), round(image.height * 0.65) + 1):
        for x in range(round(half), round(image.width - half) + 1):
            assert pixels[x, y][:3] != _KEY_RGB, f"hole at {(x, y)}, expand={expand}"


@pytest.mark.parametrize("expand", [0.0, 0.3, 0.6, 1.0])
def test_the_morph_always_keys_out_its_corners(expand):
    image, _ = render_bar(BarState.IDLE, "", expand=expand)
    pixels = image.load()
    assert pixels[0, 0][:3] == _KEY_RGB
    assert pixels[image.width - 1, image.height - 1][:3] == _KEY_RGB


def test_the_capsule_top_is_lighter_than_its_body():
    """The sheen must *add* light. Drawn with ImageDraw's fill it replaced
    the opaque surface with a nearly transparent one instead, which showed
    the desktop through the top half of the bar."""
    image, _ = render_bar(BarState.IDLE, "")
    pixels = image.load()
    x = image.width // 2
    top = sum(pixels[x, 6][:3])
    bottom = sum(pixels[x, image.height - 8][:3])
    assert top > bottom


def test_the_morph_grows_monotonically():
    widths = [render_bar(BarState.IDLE, "", expand=i / 20)[0].width for i in range(21)]
    heights = [render_bar(BarState.IDLE, "", expand=i / 20)[0].height for i in range(21)]
    assert all(b >= a for a, b in zip(widths, widths[1:]))
    assert all(b >= a for a, b in zip(heights, heights[1:]))


def test_controls_are_not_hit_testable_while_moving():
    """A target the pointer cannot land on must not claim to be one."""
    _image, moving = render_bar(BarState.IDLE, "", expand=0.8)
    assert actions(moving) == [ACTION_MENU]
    _image, settled = render_bar(BarState.IDLE, "", expand=1.0)
    assert ACTION_MIC in actions(settled)


# -- press, hover and the dot ---------------------------------------------


def test_press_changes_the_render():
    plain, _ = render_bar(BarState.IDLE, "", hover=ACTION_MIC, hover_t=1.0)
    pressed, _ = render_bar(
        BarState.IDLE, "", hover=ACTION_MIC, hover_t=1.0, press=ACTION_MIC, press_t=1.0
    )
    assert plain.tobytes() != pressed.tobytes()


def test_a_press_on_one_control_leaves_the_other_alone():
    a, _ = render_bar(BarState.IDLE, "", press=ACTION_MIC, press_t=1.0)
    b, _ = render_bar(BarState.IDLE, "", press=ACTION_LANGUAGE, press_t=1.0)
    assert a.tobytes() != b.tobytes()


def test_the_dot_acknowledges_a_hover_without_resizing_its_window():
    """Growing the window on hover would move the bar under the pointer."""
    plain, _ = render_dot()
    hovered, _ = render_dot(grow=2.0, ring=1.0)
    assert plain.size == hovered.size
    assert plain.tobytes() != hovered.tobytes()


def test_a_hidden_tray_is_dimmer_but_still_there():
    normal, _ = render_dot()
    dimmed, _ = render_dot(opacity=0.42)
    assert normal.tobytes() != dimmed.tobytes()

    def ink(image):
        pixels = image.load()
        return [
            pixels[image.width // 2, y][:3]
            for y in range(image.height)
            if pixels[image.width // 2, y][:3] != _KEY_RGB
        ]

    assert ink(dimmed), "hidden means quiet, not gone"
    # Dimmer means the light half of the stroke is closer to the background.
    assert max(sum(p) for p in ink(dimmed)) < max(sum(p) for p in ink(normal))


def test_paused_is_quieter_than_idle_at_rest():
    """At 16 pixels tall there is no room for a second silhouette, so paused
    dims the tray instead. The state is spelled out in words -- and offers
    Resume -- the moment the pointer arrives, which is the tier that can
    carry it."""
    idle, _ = render_dot()
    paused, _ = render_dot(hollow=True)
    assert idle.tobytes() != paused.tobytes()

    def brightest(image):
        pixels = image.load()
        return max(
            sum(pixels[image.width // 2, y][:3])
            for y in range(image.height)
            if pixels[image.width // 2, y][:3] != _KEY_RGB
        )

    assert brightest(paused) < brightest(idle)


def test_paused_expands_to_say_so_in_words():
    _image, regions = render_bar(BarState.PAUSED, "")
    assert ACTION_RESUME in actions(regions)


# -- recovery --------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "action"),
    [
        ("paste_blocked", ACTION_COPY),
        ("clipboard", ACTION_PASTE),
        ("audio", ACTION_MIC),
        ("auth", ACTION_SETTINGS),
        ("no_key", ACTION_SETTINGS),
    ],
)
def test_every_failure_with_a_way_out_offers_it(kind, action):
    assert recovery_for(BarState.ERROR, kind) == (
        recovery_for(BarState.ERROR, kind)[0],
        action,
    )
    _image, regions = render_bar(BarState.ERROR, "x", kind=kind)
    assert action in actions(regions)


def test_rate_limiting_offers_nothing_because_there_is_nothing_to_do():
    """A button that cannot help is worse than no button."""
    assert recovery_for(BarState.ERROR, "rate_limited") is None
    _image, regions = render_bar(BarState.ERROR, "rate limited", kind="rate_limited")
    assert actions(regions) == [ACTION_MENU]


def test_recovery_is_withheld_when_there_is_nothing_to_recover():
    """This is how the old menu produced its own error: it offered Copy with
    nothing on the clipboard and then complained."""
    assert recovery_for(BarState.ERROR, "paste_blocked", can_paste=False) is None
    assert recovery_for(BarState.ERROR, "audio", can_paste=False) is not None


def test_no_speech_points_at_the_microphone():
    assert recovery_for(BarState.DONE, "no_speech") == ("Mic", ACTION_MIC)


def test_paused_always_offers_resume():
    assert recovery_for(BarState.PAUSED, "") == ("Resume", ACTION_RESUME)
    _image, regions = render_bar(BarState.PAUSED, "")
    assert ACTION_RESUME in actions(regions)


def test_an_unknown_kind_is_not_an_error():
    assert recovery_for(BarState.ERROR, "something-new") is None
    assert recovery_for(BarState.IDLE, "paste_blocked") is None


def test_a_recovery_button_widens_the_capsule():
    plain = natural_width(BarState.ERROR, "failed")
    with_action = natural_width(BarState.ERROR, "failed", kind="paste_blocked")
    assert with_action > plain


# -- keycaps ---------------------------------------------------------------


def test_rich_segments_splits_keycaps_out():
    assert rich_segments("hold [[Right Ctrl]] now") == (
        (False, "hold "),
        (True, "Right Ctrl"),
        (False, " now"),
    )


def test_rich_segments_handles_the_plain_and_empty_cases():
    assert rich_segments("") == ()
    assert rich_segments("just text") == ((False, "just text"),)
    assert rich_segments("[[Ctrl]]") == ((True, "Ctrl"),)


def test_a_keycap_costs_more_width_than_its_letters():
    """It is a drawn key, not a word, so it has to be measured as one or the
    label runs off the end of the capsule."""
    assert measure_rich("hold [[Ctrl]]") > measure_rich("hold Ctrl")


def test_a_keycap_renders_differently_from_its_own_text():
    plain, _ = render_bar(BarState.COACH, "hold Right Ctrl")
    capped, _ = render_bar(BarState.COACH, "hold [[Right Ctrl]]")
    assert plain.tobytes() != capped.tobytes()


def test_a_warn_toned_hint_renders_differently():
    info, _ = render_bar(BarState.IDLE, "", hint="x", tone=HintTone.INFO)
    warn, _ = render_bar(BarState.IDLE, "", hint="x", tone=HintTone.WARN)
    assert info.tobytes() != warn.tobytes()


# -- the menu, as it actually reads ---------------------------------------


def test_pause_becomes_resume_when_the_agent_is_off():
    paused = build_menu(listening=False)
    labels = [item.label for item in paused]
    assert "Resume dictation" in labels
    assert "Pause dictation" not in labels


def test_hide_becomes_unhide_while_hidden():
    """There was previously no way to un-hide before the hour was up."""
    hidden = build_menu(hidden_for_s=57 * 60)
    row = next(item for item in hidden if item.action == "unhide")
    assert "57m" in row.label
    brief = next(i for i in build_menu(hidden_for_s=20) if i.action == "unhide")
    assert "under a minute" in brief.label


def test_recovery_rows_are_disabled_rather_than_failing():
    empty = build_menu(can_paste=False)
    rows = [item for item in empty if item.action in (ACTION_PASTE, ACTION_COPY)]
    assert len(rows) == 2
    assert all(not row.enabled for row in rows)
    assert all(not row.selectable for row in rows)


def test_a_disabled_row_cannot_be_clicked():
    _image, regions = render_menu(build_menu(can_paste=False))
    assert ACTION_PASTE not in actions(regions)
    assert ACTION_COPY not in actions(regions)


def test_a_full_menu_offers_every_row():
    _image, regions = render_menu(build_menu(can_paste=True))
    assert ACTION_PASTE in actions(regions)
    assert ACTION_COPY in actions(regions)


def test_build_menu_leaves_a_healthy_menu_alone():
    assert build_menu(listening=True, can_paste=True, hidden_for_s=0.0) == DEFAULT_MENU


def test_a_disabled_row_still_renders_its_label():
    plain, _ = render_menu((MenuItem("x", "Paste"),))
    disabled, _ = render_menu((MenuItem("x", "Paste", enabled=False),))
    assert plain.size == disabled.size
    assert plain.tobytes() != disabled.tobytes()


# -- popovers --------------------------------------------------------------


def test_a_checked_row_is_marked():
    plain, _ = render_menu((MenuItem("en", "English"),))
    checked, _ = render_menu((MenuItem("en", "English", checked=True),))
    assert plain.tobytes() != checked.tobytes()


def test_a_footer_is_a_caption_not_a_row():
    items = (MenuItem("a", "A"), MenuItem("", label="Applies after a restart", footer=True))
    _image, regions = render_menu(items)
    assert actions(regions) == ["a"]
    assert menu_height(items) > menu_height((MenuItem("a", "A"),))


def test_a_footer_is_shorter_than_a_row():
    footer = menu_height((MenuItem("", label="note", footer=True),))
    row = menu_height((MenuItem("a", "A"),))
    assert footer < row


def test_keyboard_focus_reads_differently_from_hover():
    """Otherwise a user who arrowed down cannot tell whether Enter acts on
    the row they arrowed to or the one the pointer is resting on."""
    hovered, _ = render_menu(DEFAULT_MENU, hover=ACTION_SETTINGS)
    focused, _ = render_menu(DEFAULT_MENU, focus=ACTION_SETTINGS)
    assert hovered.tobytes() != focused.tobytes()


def test_a_popover_can_be_narrower_than_the_menu():
    narrow, _ = render_menu((MenuItem("a", "A"),), width=200)
    assert narrow.width == 200


def test_a_row_detail_changes_the_render():
    plain, _ = render_menu((MenuItem("a", "Headset"),))
    detailed, _ = render_menu((MenuItem("a", "Headset", detail="default"),))
    assert plain.tobytes() != detailed.tobytes()


# -- panel placement -------------------------------------------------------


def test_a_panel_is_kept_on_screen():
    """A popover anchored under a control at the screen edge must not open
    half off it -- the rows that fall outside are the ones being reached
    for."""
    x, y = clamp_to_area(1900, -40, (268, 400), FULL_HD)
    assert 0 <= x <= 1920 - 268
    assert y >= 0
    assert y + 400 <= 1040


def test_a_panel_that_already_fits_is_left_where_it_is():
    assert clamp_to_area(500, 500, (268, 300), FULL_HD) == (500, 500)


def test_clamping_a_panel_larger_than_the_screen_still_returns_it_on_screen():
    x, y = clamp_to_area(0, 0, (4000, 4000), FULL_HD)
    assert (x, y) == (8, 8)


# -- tooltips --------------------------------------------------------------


def test_a_tooltip_is_dark_like_the_bar_not_light_like_the_menu():
    """It belongs to the control it describes, not to the panel layer."""
    tooltip = render_tooltip("Microphone")
    menu, _ = render_menu(DEFAULT_MENU)
    tip_mid = tooltip.load()[tooltip.width // 2, tooltip.height // 2][:3]
    menu_mid = menu.load()[menu.width // 2, menu.height // 2][:3]
    assert sum(tip_mid) < sum(menu_mid)


def test_a_tooltip_grows_with_its_text():
    assert render_tooltip("Microphone").width > render_tooltip("Mic").width


def test_tooltip_corners_key_out():
    tooltip = render_tooltip("Microphone")
    assert tooltip.load()[0, 0][:3] == _KEY_RGB


# -- width targets ---------------------------------------------------------


def test_natural_width_is_what_the_settled_capsule_measures():
    """The tween animates toward this number, so a mismatch is a bar that
    never quite arrives."""
    for state, detail, kind in (
        (BarState.IDLE, "", ""),
        (BarState.RECORDING, "", ""),
        (BarState.DONE, "12 words", ""),
        (BarState.ERROR, "Couldn't paste here", "paste_blocked"),
        (BarState.PAUSED, "", ""),
    ):
        image, _ = render_bar(state, detail, kind=kind)
        assert image.width == natural_width(state, detail, kind=kind), state


def test_natural_width_accounts_for_the_elapsed_counter():
    assert natural_width(BarState.RECORDING, "", elapsed_s=605) > natural_width(
        BarState.RECORDING, "", elapsed_s=1
    )


# -- script coverage -------------------------------------------------------
#
# PIL has no font fallback: a glyph the chosen face lacks renders as .notdef,
# a hollow box. The language popover lists languages by their own name, so
# without a per-script face every non-Latin row -- the rows a user most needs
# to recognise -- is tofu.


def test_latin_uses_the_ui_font():
    assert _font_for("English", 14) is _font(14, bold=False)
    assert _font_for("Português", 14) is _font(14, bold=False)


@pytest.mark.parametrize("label", ["हिन्दी", "日本語", "中文", "한국어"])
def test_a_non_latin_label_gets_a_face_that_can_draw_it(label):
    assert _font_for(label, 14) is not _font(14, bold=False)


def test_a_non_latin_row_is_measured_with_the_face_it_is_drawn_with():
    """Measuring with one face and painting with another lays the row out for
    the wrong width, which is how a label ends up clipped."""
    assert measure_rich("日本語") > 0
    narrow, _ = render_menu((MenuItem("a", "中文"),))
    wide, _ = render_menu((MenuItem("a", "中文中文中文"),))
    assert narrow.tobytes() != wide.tobytes()


def test_a_non_latin_label_actually_puts_ink_on_the_panel():
    blank, _ = render_menu((MenuItem("a", ""),))
    hindi, _ = render_menu((MenuItem("a", "हिन्दी"),))
    assert blank.size == hindi.size
    assert blank.tobytes() != hindi.tobytes()


# -- dismissing a panel by clicking elsewhere ------------------------------
#
# A menu that survives a click somewhere else is a menu you have to dismiss
# before getting on with your work, which is backwards: clicking the thing
# you actually want *is* the dismissal everywhere else on the desktop.
#
# The bar must never take focus (paste targets the focused window), so this
# cannot use a Tk grab or a focus-out event. It samples the mouse instead --
# and _mouse_press_point is the only part that touches the OS, so the
# decision around it is testable without a mouse.


class FakePanel:
    def __init__(self, rect):
        self.rect = rect
        self.closed = False

    def contains(self, x, y):
        left, top, right, bottom = self.rect
        return left <= x <= right and top <= y <= bottom

    def close(self):
        self.closed = True


def _dismissable_bar(press_at, *, bar_rect=(500, 900, 640, 944), opened_ago=10.0):
    bar = SwayBar.__new__(SwayBar)
    bar._menu = FakePanel((520, 700, 788, 890))
    bar._popover = None
    bar._popover_anchor = ""
    bar._panel_opened_at = time.monotonic() - opened_ago
    bar._mouse_press_point = lambda: press_at
    bar._point_in_bar = lambda x, y: (
        bar_rect[0] <= x <= bar_rect[2] and bar_rect[1] <= y <= bar_rect[3]
    )
    return bar


def test_a_click_elsewhere_dismisses_the_panel():
    bar = _dismissable_bar((120, 120))
    menu = bar._menu
    bar._dismiss_on_outside_click()
    assert bar._menu is None
    assert menu.closed


def test_a_click_inside_the_panel_is_left_to_the_panel():
    bar = _dismissable_bar((600, 750))
    bar._dismiss_on_outside_click()
    assert bar._menu is not None


def test_a_click_on_the_bar_is_left_to_the_bar():
    """The bar toggles its own menu shut; dismissing here as well would close
    and immediately reopen it."""
    bar = _dismissable_bar((560, 920))
    bar._dismiss_on_outside_click()
    assert bar._menu is not None


def test_no_click_changes_nothing():
    bar = _dismissable_bar(None)
    bar._dismiss_on_outside_click()
    assert bar._menu is not None


def test_a_panel_is_not_killed_by_the_click_that_opened_it():
    """GetAsyncKeyState latches "pressed since last call", so the opening
    click can still be waiting to be read on the first tick afterwards."""
    bar = _dismissable_bar((120, 120), opened_ago=0.0)
    bar._dismiss_on_outside_click()
    assert bar._menu is not None, "dismissed itself inside the grace period"


def test_the_grace_period_does_expire():
    bar = _dismissable_bar((120, 120), opened_ago=0.4)
    bar._dismiss_on_outside_click()
    assert bar._menu is None


def test_nothing_happens_when_no_panel_is_open():
    bar = _dismissable_bar((120, 120))
    bar._menu = None
    bar._dismiss_on_outside_click()  # must not raise


def test_a_popover_is_dismissed_the_same_way():
    bar = _dismissable_bar((120, 120))
    bar._menu = None
    bar._popover = FakePanel((520, 700, 760, 860))
    bar._popover_anchor = ACTION_MIC
    popover = bar._popover
    bar._dismiss_on_outside_click()
    assert bar._popover is None
    assert popover.closed
    assert bar._popover_anchor == ""


def test_dismissing_dispatches_no_action():
    """Clicking away is not choosing a row."""
    dispatched = []
    bar = _dismissable_bar((120, 120))
    bar._on_action = dispatched.append
    bar._dismiss_on_outside_click()
    assert dispatched == []


# -- the resting tray has to be clickable ----------------------------------
#
# Windows hit-tests a colour-keyed window by its transparency, not by its
# window rect: a pixel that keys out passes the mouse *through* to whatever
# is behind it. So "see-through" and "clickable" are the same property here,
# and an outline with a genuinely empty middle has a hit target the width of
# its own stroke. Measured on the real thing, the tray's centre eight rows
# were landing in the browser behind it.


def test_the_resting_tray_has_no_click_through_holes():
    image, _ = render_dot()
    pixels = image.load()
    # Inside the tray's own silhouette, allowing for the rounded ends.
    holes = [
        (x, y)
        for y in range(_REST_PAD + 2, image.height - _REST_PAD - 2)
        for x in range(_REST_PAD + 9, image.width - _REST_PAD - 9)
        if pixels[x, y][:3] == _KEY_RGB
    ]
    assert not holes, f"{len(holes)} pixels would pass the mouse through, e.g. {holes[:4]}"


def test_the_resting_tray_is_still_visually_quiet():
    """Hittable, but only just visible: on a dark desktop the interior has to
    read as nothing at all."""
    image, _ = render_dot()
    interior = image.load()[image.width // 2, image.height // 2][:3]
    assert interior != _KEY_RGB, "not hittable"
    assert sum(interior) < 90, "too solid to read as an empty tray"


def test_a_hidden_tray_is_still_clickable():
    """Hidden dims the tray; it must not dim it into being click-through, or
    there is no way left to reach the menu and un-hide it."""
    image, _ = render_dot(opacity=0.42)
    pixels = image.load()
    row = image.height // 2
    interior = [
        pixels[x, row][:3]
        for x in range(_REST_PAD + 9, image.width - _REST_PAD - 9)
    ]
    assert all(pixel != _KEY_RGB for pixel in interior)


@pytest.mark.parametrize("expand", [0.0, 0.2, 0.4, 0.6, 0.8, 1.0])
def test_the_bar_is_clickable_at_every_point_of_the_morph(expand):
    """A target that is solid at both ends and porous in between is worse
    than one that is porous throughout, because it fails only sometimes."""
    image, _ = render_bar(BarState.IDLE, "", expand=expand)
    pixels = image.load()
    half = image.height / 2
    holes = [
        (x, y)
        for y in range(round(image.height * 0.35), round(image.height * 0.65) + 1)
        for x in range(round(half), round(image.width - half) + 1)
        if pixels[x, y][:3] == _KEY_RGB
    ]
    assert not holes, f"expand={expand}: {len(holes)} click-through pixels"


# -- the activity orb ------------------------------------------------------
#
# The orb replaced both the logo mark and the corner badge glyph. The badge
# was what guaranteed a state was never carried by colour alone, so that
# guarantee has to be re-proved here against the thing that took its job.


#: The orb's slot: the left end of the capsule, before any text starts.
ORB_BOX = (0, 0, 46, _HEIGHT)


def orb_patch(state, **kwargs):
    """The orb's corner of the capsule, in greyscale.

    Greyscale on purpose: it is how a test asks "would these two still be
    distinguishable to someone who cannot use the hue?". Cropped, because
    otherwise the state *label* would make every pair differ and the
    assertion would prove nothing.
    """
    image, _ = render_bar(state, "", **kwargs)
    return image.convert("L").crop(ORB_BOX).tobytes()


@pytest.mark.parametrize(
    "state",
    [
        BarState.IDLE,
        BarState.RECORDING,
        BarState.THINKING,
        BarState.INJECTING,
        BarState.DONE,
        BarState.ERROR,
        BarState.PAUSED,
    ],
)
def test_every_state_draws_something_in_the_orb_slot(state):
    image, _ = render_bar(state, "")
    assert image.crop(ORB_BOX).getbbox() is not None


def test_no_two_states_are_told_apart_by_colour_alone():
    """The badge used to carry this. Now the orb's motion and the outcome
    glyphs do, and it has to hold for every pair, not on average."""
    states = [
        BarState.IDLE,
        BarState.RECORDING,
        BarState.THINKING,
        BarState.INJECTING,
        BarState.DONE,
        BarState.ERROR,
    ]
    patches = {state: orb_patch(state, orb_phase_s=0.7) for state in states}
    for i, first in enumerate(states):
        for second in states[i + 1 :]:
            assert patches[first] != patches[second], f"{first} and {second} match in greyscale"


@pytest.mark.parametrize(
    "state", [BarState.RECORDING, BarState.THINKING, BarState.INJECTING, BarState.IDLE]
)
def test_the_orb_animates_with_its_phase(state):
    assert orb_patch(state, orb_phase_s=0.1) != orb_patch(state, orb_phase_s=1.3)


@pytest.mark.parametrize(
    "state", [BarState.RECORDING, BarState.THINKING, BarState.INJECTING, BarState.IDLE]
)
def test_reduced_motion_holds_one_frame(state):
    """The whole contract: the phase stops reaching the renderer."""
    assert orb_patch(state, orb_phase_s=0.1, reduced_motion=True) == orb_patch(
        state, orb_phase_s=1.3, reduced_motion=True
    )


def test_a_paused_bar_holds_still_even_with_motion_allowed():
    """Paused is disarmed. An orb animating about work it has been told not
    to do is the bar contradicting its own label."""
    assert orb_patch(BarState.PAUSED, orb_phase_s=0.1) == orb_patch(
        BarState.PAUSED, orb_phase_s=1.3
    )


def test_the_listening_orb_moves_with_the_voice():
    quiet = orb_patch(BarState.RECORDING, orb_phase_s=0.7, level_t=(0.0, 0.0, 0.0))
    loud = orb_patch(BarState.RECORDING, orb_phase_s=0.7, level_t=(1.0, 1.0, 1.0))
    assert quiet != loud


def test_only_recording_reads_the_level():
    """A level left over from the last dictation must not perturb the rest."""
    for state in (BarState.THINKING, BarState.INJECTING, BarState.IDLE):
        assert orb_patch(state, orb_phase_s=0.7, level_t=(1.0, 1.0, 1.0)) == orb_patch(
            state, orb_phase_s=0.7, level_t=(0.0, 0.0, 0.0)
        )


def test_the_resting_dot_draws_no_orb():
    """Below the content gate there is no room for one, and drawing it anyway
    is what would make the resting bar repaint forever."""
    image, _ = render_bar(BarState.IDLE, "", expand=0.0, orb_phase_s=0.1)
    later, _ = render_bar(BarState.IDLE, "", expand=0.0, orb_phase_s=1.3)
    assert image.tobytes() == later.tobytes()


def test_the_orb_did_not_move_the_layout():
    """The orb took the avatar's slot, not a new one.

    The capsule's width and every hit region are derived from the same
    constants the mark used, so a state that fit before still fits and a
    control the pointer was aiming at has not shifted under it.
    """
    for state in list(BarState):
        before, regions = render_bar(state, "", orb_phase_s=0.0)
        after, later_regions = render_bar(state, "", orb_phase_s=1.3)
        assert before.size == after.size
        assert regions == later_regions
