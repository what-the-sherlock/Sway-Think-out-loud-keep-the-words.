"""Per-pixel-alpha windows for the overlay (DESIGN.md §8.5).

Tk's ``-transparentcolor`` is a 1-bit colour key: every pixel is either fully
shown or fully cut, so the anti-aliased edges PIL draws come out keyed and
the capsule's corners look stair-stepped, and nothing can carry a soft
shadow. A *layered* window presented with ``UpdateLayeredWindow`` takes a
32-bit premultiplied BGRA bitmap instead, so edges blend into whatever is
behind them.

Hit-testing follows the same alpha: fully transparent pixels pass the mouse
through, exactly as keyed pixels did, so the bar's click-through behaviour
is unchanged. Mouse messages still go to Tk's child window under the
pointer, so every existing binding keeps working.

Windows-only, stdlib ``ctypes`` plus PIL (already a dependency). Every entry
point returns False instead of raising, so the caller can fall back to the
colour key and the overlay still works everywhere.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Any

from PIL import Image, ImageChops

logger = logging.getLogger(__name__)

_GWL_EXSTYLE = -20
_WS_EX_LAYERED = 0x00080000
_ULW_ALPHA = 0x00000002
_AC_SRC_OVER = 0x00
_AC_SRC_ALPHA = 0x01
_BI_RGB = 0
_DIB_RGB_COLORS = 0


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte),
        ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte),
        ("AlphaFormat", ctypes.c_ubyte),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _api() -> tuple[Any, Any] | None:
    if sys.platform != "win32":
        return None
    try:
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    except (AttributeError, OSError):
        return None
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(_BITMAPINFO), wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
    ]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
        wintypes.COLORREF, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
    ]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    return user32, gdi32


def premultiplied_bgra(image: Image.Image) -> bytes:
    """``image`` as top-down premultiplied BGRA, the layout ULW requires.

    Pure (PIL only), so it is testable without a screen: a pixel's colour
    channels are scaled by its own alpha, and a fully transparent pixel
    becomes all zeros.
    """
    rgba = image if image.mode == "RGBA" else image.convert("RGBA")
    r, g, b, a = rgba.split()
    r, g, b = (ImageChops.multiply(channel, a) for channel in (r, g, b))
    return Image.merge("RGBA", (b, g, r, a)).tobytes()


def hwnd_of(window: Any) -> int | None:
    """The top-level HWND of a Tk window (Tk wraps it in a frame)."""
    api = _api()
    if api is None:
        return None
    user32, _ = api
    try:
        # The frame Tk wraps a top-level in is created when the window is
        # first mapped; asking before then returns the inner child window,
        # which can be made "layered" but can never be presented. wm_frame()
        # names the real top-level once it exists.
        window.update_idletasks()
        frame = int(str(window.wm_frame()), 16)
        if frame:
            return frame
        inner = window.winfo_id()
        return int(user32.GetAncestor(wintypes.HWND(inner), 2) or inner)  # GA_ROOT
    except Exception:
        logger.debug("could not resolve a window handle", exc_info=True)
        return None


def dpi_scale(window: Any) -> float:
    """The window's monitor scale (1.0 = 96 DPI). 1.0 when unknowable."""
    api = _api()
    hwnd = hwnd_of(window) if api is not None else None
    if api is None or hwnd is None:
        return 1.0
    user32, _ = api
    try:
        dpi = int(user32.GetDpiForWindow(wintypes.HWND(hwnd)))
    except (AttributeError, OSError):
        return 1.0
    return max(1.0, min(3.0, dpi / 96.0)) if dpi > 0 else 1.0


class LayeredSurface:
    """One Tk top-level presented through ``UpdateLayeredWindow``."""

    def __init__(self) -> None:
        self._hwnd: int | None = None
        self._api = _api()

    @property
    def active(self) -> bool:
        return self._hwnd is not None

    def attach(self, window: Any) -> bool:
        """Make ``window`` a per-pixel-alpha layered window. False on failure.

        Must be called before any ``-alpha`` or ``-transparentcolor`` is set:
        those use SetLayeredWindowAttributes, which locks a window out of
        UpdateLayeredWindow for good.
        """
        if self._api is None:
            return False
        user32, _ = self._api
        hwnd = hwnd_of(window)
        if hwnd is None:
            return False
        try:
            style = user32.GetWindowLongW(wintypes.HWND(hwnd), _GWL_EXSTYLE)
            user32.SetWindowLongW(wintypes.HWND(hwnd), _GWL_EXSTYLE, style | _WS_EX_LAYERED)
        except Exception:
            logger.debug("could not make the window layered", exc_info=True)
            return False
        self._hwnd = hwnd
        return True

    def present(self, image: Image.Image, x: int, y: int, *, alpha: float = 1.0) -> bool:
        """Show ``image`` at screen ``(x, y)``, at overall ``alpha``."""
        if self._api is None or self._hwnd is None:
            return False
        user32, gdi32 = self._api
        width, height = image.size
        if width <= 0 or height <= 0:
            return False
        data = premultiplied_bgra(image)

        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = _BI_RGB

        screen = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(screen)
        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(memory, ctypes.byref(info), _DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
        ok = False
        try:
            if not bitmap or not bits.value:
                return False
            ctypes.memmove(bits, data, len(data))
            previous = gdi32.SelectObject(memory, bitmap)
            blend = _BLENDFUNCTION(_AC_SRC_OVER, 0, max(0, min(255, round(alpha * 255))), _AC_SRC_ALPHA)
            ok = bool(user32.UpdateLayeredWindow(
                wintypes.HWND(self._hwnd), screen,
                ctypes.byref(wintypes.POINT(int(x), int(y))),
                ctypes.byref(wintypes.SIZE(width, height)),
                memory, ctypes.byref(wintypes.POINT(0, 0)),
                0, ctypes.byref(blend), _ULW_ALPHA,
            ))
            if not ok:
                logger.debug(
                    "UpdateLayeredWindow refused a %dx%d frame (error %d)",
                    width, height, ctypes.get_last_error() or ctypes.GetLastError(),
                )
            gdi32.SelectObject(memory, previous)
        except Exception:
            logger.debug("UpdateLayeredWindow failed", exc_info=True)
            ok = False
        finally:
            if bitmap:
                gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory)
            user32.ReleaseDC(None, screen)
        return ok


__all__ = ["LayeredSurface", "dpi_scale", "hwnd_of", "premultiplied_bgra"]
