"""The activity orb.

Two kinds of test live here, and the first is the reason the port exists in
this shape at all.

**Parity.** Upstream Thinking Orbs publishes ``spec/orbs-golden.json`` --
complete dot lists at four frozen timestamps -- precisely so that native
ports can be verified as *numbers* rather than eyeballed. That file is
vendored (trimmed to the four states we implement) as
``fixtures/orbs-golden.json.gz``, and :func:`test_matches_upstream_geometry`
checks every dot of every frame against it. If that passes, the orb on screen
is upstream's animation, not something that resembles it.

**Behaviour.** The rest covers what we added around the engine: the reduced
motion contract, the voice-driven amplitude, and the guarantee that no state
is distinguished by hue alone.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from sway import orb
from sway.orb import OrbVariant, orb_image, preset_size_for

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "orbs-golden.json.gz"


def load_golden() -> dict:
    with gzip.open(GOLDEN_PATH, "rt", encoding="utf-8") as handle:
        return json.load(handle)


GOLDEN = load_golden()
CASES = GOLDEN["cases"]


# -- parity with upstream --------------------------------------------------


def test_the_golden_fixture_covers_every_variant_we_ship():
    """A parity suite that quietly stopped covering a variant is worse than none."""
    covered = {case["state"] for case in CASES}
    assert covered == {str(variant) for variant in OrbVariant}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["key"])
def test_matches_upstream_geometry(case):
    """Every dot of every frozen frame, to upstream's own 1e-4 tolerance."""
    tolerance = GOLDEN["tolerance"]
    mode, speed, opts = orb.resolve_preset(case["state"], case["size"])
    expected = GOLDEN["resolved"][f"{case['state']}-{case['size']}"]

    assert mode == expected["mode"]
    assert speed == pytest.approx(expected["speed"])
    assert dict(opts) == pytest.approx(expected["opts"])

    # The golden `t` is already the scaled clock, so the mode function is
    # called directly rather than through `frame`, which would apply the
    # preset speed a second time.
    dots = orb.MODE_FRAMES[mode](case["size"], case["t"], dict(opts))
    assert len(dots) == case["dotCount"]

    flat = case["dots"]
    for index, dot in enumerate(dots):
        assert dot == pytest.approx(flat[index * 6 : index * 6 + 6], abs=tolerance), (
            f"dot {index} of {case['key']}"
        )


def test_dots_arrive_in_draw_order():
    """Far to near. A port that draws the list in any other order loses the depth."""
    dots = orb.frame(OrbVariant.WORKING, 34, 0.7)
    zs = [dot[2] for dot in dots]
    assert zs == sorted(zs)


def test_no_dot_survives_below_the_cull_alpha():
    for variant in OrbVariant:
        for dot in orb.frame(variant, 34, 1.1):
            assert dot[5] >= 0.02


def test_radii_are_floored_to_the_mode_minimum():
    for variant in OrbVariant:
        mode, _speed, opts = orb.resolve_preset(variant, preset_size_for(variant))
        r_min = dict(opts)["rMin"]
        for dot in orb.frame(variant, 34, 1.1):
            assert dot[3] >= r_min


def test_js_rounding_is_used_for_counts():
    """Python rounds halves to even, JavaScript rounds them up.

    Not pedantry: ``_scale_counts`` rounds every dot count, and the builtin
    would silently produce a different lattice from upstream's on any preset
    whose product lands on a .5 -- which the golden vectors would then catch
    as a whole-frame mismatch rather than as this one line.
    """
    assert orb._js_round(0.5) == 1
    assert orb._js_round(1.5) == 2
    assert orb._js_round(2.5) == 3
    assert orb._js_round(-0.5) == 0


# -- variant selection -----------------------------------------------------


def test_every_variant_has_a_tuned_preset():
    for variant in OrbVariant:
        assert preset_size_for(variant) in (20, 64)


def test_a_frame_is_dense_enough_to_read_at_the_bar_size():
    """The reason the preset is chosen per variant rather than by nearest size.

    At the 20 preset ``working`` collapses to three orbits and reads as a few
    stray dots; the floor here is what stops a future retune from silently
    reintroducing that.
    """
    for variant in OrbVariant:
        assert len(orb.frame(variant, 34, 0.7)) >= 100


# -- motion ----------------------------------------------------------------


@pytest.mark.parametrize("variant", list(OrbVariant))
def test_the_orb_actually_moves(variant):
    early = orb.frame(variant, 34, 0.2)
    later = orb.frame(variant, 34, 1.4)
    assert early != later


@pytest.mark.parametrize("variant", list(OrbVariant))
def test_a_held_phase_is_a_still_image(variant):
    """What reduced motion relies on: the same phase is byte-identical output."""
    first = orb_image(variant, 34, phase_s=orb.REDUCED_MOTION_PHASE)
    second = orb_image(variant, 34, phase_s=orb.REDUCED_MOTION_PHASE)
    assert first.tobytes() == second.tobytes()


def test_the_reduced_motion_frame_is_not_the_zero_frame():
    """Upstream holds t=0.6, not t=0, so the still frame is mid-gesture."""
    assert orb.REDUCED_MOTION_PHASE > 0
    still = orb_image(OrbVariant.WORKING, 34, phase_s=orb.REDUCED_MOTION_PHASE)
    zero = orb_image(OrbVariant.WORKING, 34, phase_s=0.0)
    assert still.tobytes() != zero.tobytes()


def test_amplitude_changes_the_listening_orb():
    """The voice-driven undulation is real, not decorative."""
    quiet = orb.frame(OrbVariant.LISTENING, 34, 0.7, amplitude=0.55)
    loud = orb.frame(OrbVariant.LISTENING, 34, 0.7, amplitude=1.6)
    assert quiet != loud


def test_amplitude_defaults_to_upstream_geometry():
    """The default must be the neutral value, or the golden vectors are a lie."""
    assert orb.frame(OrbVariant.LISTENING, 34, 0.7) == orb.frame(
        OrbVariant.LISTENING, 34, 0.7, amplitude=1.0
    )


def test_amplitude_leaves_the_other_variants_alone():
    """Only ``wave`` reads it; a stray amplitude must not perturb the rest."""
    for variant in (OrbVariant.WORKING, OrbVariant.COMPOSING, OrbVariant.BREATHING):
        assert orb.frame(variant, 34, 0.7, amplitude=1.9) == orb.frame(variant, 34, 0.7)


# -- rendering -------------------------------------------------------------


@pytest.mark.parametrize("variant", list(OrbVariant))
def test_an_orb_renders_something_visible(variant):
    image = orb_image(variant, 34, supersample=4)
    assert image.size == (136, 136)
    assert image.getbbox() is not None


def test_the_orb_stays_inside_its_box():
    """It is composited into a 34px slot; a dot outside it would be clipped."""
    for variant in OrbVariant:
        image = orb_image(variant, 34, supersample=4)
        left, top, right, bottom = image.getbbox()
        assert left >= 0 and top >= 0
        assert right <= 136 and bottom <= 136


def test_geometry_is_computed_at_the_display_size_not_the_supersampled_one():
    """Supersampling is a drawing concern, not a geometry one.

    Upstream's radii scale sub-linearly with the frame, so a dot takes up a
    larger share of a small orb than of a big one -- that is what keeps a
    20px spinner legible. Computing the frame at the supersampled size would
    therefore hand the 34px slot a 136px orb's proportions: the same number
    of dots, each far too fine, downsampled into a grey smudge.
    """
    plain = orb_image(OrbVariant.WORKING, 34, supersample=1)
    supersampled = orb_image(OrbVariant.WORKING, 34, supersample=4)
    assert plain.size == (34, 34)
    assert supersampled.size == (136, 136)

    small = orb.frame(OrbVariant.WORKING, 34, 0.7)
    native = orb.frame(OrbVariant.WORKING, 136, 0.7)
    # The lattice itself is size-independent: same dots, different proportions.
    assert len(small) == len(native)
    small_share = sum(dot[3] for dot in small) / len(small) / 34
    native_share = sum(dot[3] for dot in native) / len(native) / 136
    assert small_share > native_share


def test_opacity_fades_the_whole_orb():
    faint = orb_image(OrbVariant.BREATHING, 34, supersample=4, opacity=0.3)
    solid = orb_image(OrbVariant.BREATHING, 34, supersample=4, opacity=1.0)
    faint_alpha = sum(faint.getchannel("A").tobytes())
    solid_alpha = sum(solid.getchannel("A").tobytes())
    assert 0 < faint_alpha < solid_alpha


def test_a_fully_transparent_orb_is_empty():
    assert orb_image(OrbVariant.BREATHING, 34, opacity=0.0).getbbox() is None


def test_accent_tints_the_orb():
    amber = orb_image(OrbVariant.LISTENING, 34, accent=(255, 184, 77))
    indigo = orb_image(OrbVariant.LISTENING, 34, accent=(79, 70, 229))
    assert amber.tobytes() != indigo.tobytes()


def test_one_colour_at_both_ends_collapses_the_ramp():
    """The tint is a plain two-stop ramp and nothing more.

    Hand it the same colour twice and every dot must come out exactly that
    colour -- which is also what shows the ramp is a superset of upstream's
    greyscale painter rather than a replacement for it: a grey pair gives
    grey out.
    """
    grey = (180, 180, 180)
    image = orb_image(OrbVariant.BREATHING, 34, accent=grey, ink=grey)
    pixels = image.tobytes()
    seen = {
        pixels[i : i + 3] for i in range(0, len(pixels), 4) if pixels[i + 3]
    }
    assert seen == {bytes(grey)}


def test_bad_sizes_are_refused():
    with pytest.raises(ValueError):
        orb_image(OrbVariant.WORKING, 0)
    with pytest.raises(ValueError):
        orb_image(OrbVariant.WORKING, 34, supersample=0)
