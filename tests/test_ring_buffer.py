"""Tests for the allocation-free circular PCM buffer."""

from __future__ import annotations

import threading

import pytest

from sway.audio.ring_buffer import RingBuffer


def _pcm(*values: int) -> bytes:
    """Build little-endian int16 mono PCM from plain sample values."""
    import struct

    return struct.pack(f"<{len(values)}h", *values)


def test_write_then_read_since_start():
    ring = RingBuffer(capacity_frames=10)
    start = ring.mark()
    ring.write(_pcm(1, 2, 3))
    assert ring.read_since(start) == _pcm(1, 2, 3)


def test_read_since_now_is_empty():
    ring = RingBuffer(capacity_frames=10)
    ring.write(_pcm(1, 2, 3))
    now = ring.mark()
    assert ring.read_since(now) == b""


def test_multiple_writes_accumulate_in_order():
    ring = RingBuffer(capacity_frames=10)
    start = ring.mark()
    ring.write(_pcm(1, 2))
    ring.write(_pcm(3, 4))
    ring.write(_pcm(5))
    assert ring.read_since(start) == _pcm(1, 2, 3, 4, 5)


def test_wraps_around_the_buffer():
    ring = RingBuffer(capacity_frames=4)
    ring.write(_pcm(1, 2, 3))  # fills 3/4
    start = ring.mark()
    ring.write(_pcm(4, 5, 6))  # wraps: total_frames now 6, spans across the seam
    assert ring.read_since(start) == _pcm(4, 5, 6)


def test_overrun_drops_oldest_frames_instead_of_raising():
    ring = RingBuffer(capacity_frames=4)
    start = ring.mark()
    ring.write(_pcm(1, 2, 3, 4, 5, 6, 7))  # 7 frames into a 4-frame ring
    # Only the most recent `capacity_frames` are recoverable.
    assert ring.read_since(start) == _pcm(4, 5, 6, 7)


def test_single_write_more_than_double_capacity_does_not_crash():
    # A pathological case (a single write() call bigger than 2x the ring's
    # capacity) that used to hit a slice-length mismatch mid-wrap.
    ring = RingBuffer(capacity_frames=4)
    start = ring.mark()
    ring.write(_pcm(*range(1, 12)))  # 11 frames into a 4-frame ring
    assert ring.read_since(start) == _pcm(8, 9, 10, 11)


def test_partial_trailing_sample_is_dropped_not_raised():
    ring = RingBuffer(capacity_frames=10, sample_width=2, channels=1)
    start = ring.mark()
    ring.write(_pcm(1, 2) + b"\x00")  # 4 whole bytes + 1 stray byte
    assert ring.read_since(start) == _pcm(1, 2)


def test_stereo_frame_size():
    ring = RingBuffer(capacity_frames=10, sample_width=2, channels=2)
    start = ring.mark()
    ring.write(_pcm(1, 2, 3, 4))  # 2 stereo frames
    assert ring.read_since(start) == _pcm(1, 2, 3, 4)


def test_empty_write_is_a_no_op():
    ring = RingBuffer(capacity_frames=10)
    start = ring.mark()
    ring.write(b"")
    assert ring.read_since(start) == b""
    assert ring.mark() == start


def test_mark_is_monotonic():
    ring = RingBuffer(capacity_frames=100)
    marks = [ring.mark()]
    for i in range(5):
        ring.write(_pcm(i))
        marks.append(ring.mark())
    assert marks == sorted(marks)
    assert len(set(marks)) == len(marks)


def test_accepts_bytearray_and_memoryview():
    ring = RingBuffer(capacity_frames=10)
    start = ring.mark()
    ring.write(bytearray(_pcm(1, 2)))
    ring.write(memoryview(_pcm(3, 4)))
    assert ring.read_since(start) == _pcm(1, 2, 3, 4)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"capacity_frames": 0},
        {"capacity_frames": 10, "sample_width": 0},
        {"capacity_frames": 10, "channels": 0},
    ],
)
def test_invalid_construction_is_rejected(kwargs: dict):
    with pytest.raises(ValueError):
        RingBuffer(**kwargs)


def test_concurrent_writers_and_reader_do_not_corrupt_or_crash():
    # A stress test standing in for the real writer (audio callback thread)
    # racing against a reader (hotkey-release thread), the exact scenario
    # RingBuffer's locking exists for.
    ring = RingBuffer(capacity_frames=48_000)  # generous vs. what we write
    writes_per_thread = 200
    writer_count = 4
    start = ring.mark()
    barrier = threading.Barrier(writer_count)

    def writer(tid: int) -> None:
        barrier.wait()
        for i in range(writes_per_thread):
            ring.write(_pcm((tid * writes_per_thread + i) % 32768))

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(writer_count)]
    for t in threads:
        t.start()
    # Read concurrently with the writers; must never raise or hang.
    for _ in range(50):
        ring.read_since(start)
    for t in threads:
        t.join()

    total_written = writer_count * writes_per_thread
    assert ring.mark() - start == total_written
    assert len(ring.read_since(start)) == total_written * 2
