# Third-party notices

Sway bundles or derives from the third-party work listed here. Each entry
reproduces the licence it is used under, as those licences require.

Runtime dependencies installed from PyPI (sounddevice, numpy, webrtcvad-wheels,
soundfile, pynput, httpx, pystray, pillow, keyring) are not vendored -- they are
fetched by the package manager and carry their own licences in their own
distributions. This file covers code that ships *inside* this repository.

---

## Thinking Orbs

- **Used in:** `src/sway/orb.py` -- the activity orb the floating bar draws to
  show what the agent is doing. The module is a Python/Pillow port of the
  upstream engine's geometry: the `orbits`, `wave`, `ribbon` and `ring` mode
  painters, the shared projection and depth-shading primitives, and the tuned
  per-state presets.
- **Also used in:** `tests/fixtures/orbs-golden.json.gz` -- upstream's published
  golden vectors (`spec/orbs-golden.json`), trimmed to the four states this port
  implements and rounded to 6 decimal places. `tests/test_orb.py` checks the
  port against them, so the animation on screen is verifiably upstream's
  geometry rather than an imitation of it.
- **Author:** Jakub Antalik
- **Source:** https://github.com/Jakubantalik/thinking-orbs
- **Demo:** https://orbs.jakubantalik.com/
- **Licence:** MIT

### Modifications

The port is geometry-exact at the defaults; two additions sit around it, both
inert unless asked for, so the golden vectors still pass unchanged:

1. An `amplitude` option scaling the `wave` mode's undulation, so the listening
   orb moves with the speaker's measured microphone level. Defaults to `1.0`,
   which is upstream's geometry.
2. A colour ramp replacing upstream's greyscale painter. The depth language is
   unchanged -- near dots bright, far dots sunk -- but the resulting value runs
   from the bar's state accent to its ink colour. Given one colour at both ends
   the ramp collapses back to upstream's greyscale.

Only four of the nine upstream states are ported (`listening`, `working`,
`composing`, `breathing`), because those are the four activities Sway's engine
actually reports.

### Licence text

```
MIT License

Copyright (c) 2026 Jakub Antalik

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
