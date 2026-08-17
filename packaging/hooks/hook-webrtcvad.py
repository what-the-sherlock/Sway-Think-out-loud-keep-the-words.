"""Overrides pyinstaller-hooks-contrib's stock hook-webrtcvad.py.

That hook calls ``copy_metadata("webrtcvad")``, assuming the distribution
installed under the ``webrtcvad`` import name is also named ``webrtcvad`` on
PyPI. This project depends on ``webrtcvad-wheels`` instead (see
pyproject.toml for why -- prebuilt wheels for recent Python/Windows), whose
import name is still ``webrtcvad`` but whose distribution metadata is named
``webrtcvad-wheels``, so the stock hook raises ``PackageNotFoundError`` and
aborts the whole build. flow.spec points ``hookspath`` at this directory so
this shadows the broken one; the compiled extension itself
(``_webrtcvad``) is a plain top-level import PyInstaller's analysis
traces on its own, so all this needs to do is not crash.
"""

from PyInstaller.utils.hooks import copy_metadata

try:
    datas = copy_metadata("webrtcvad-wheels")
except Exception:
    datas = []
