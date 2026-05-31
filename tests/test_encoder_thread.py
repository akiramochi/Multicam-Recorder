"""
Concurrency / lifecycle tests for EncoderThread.

These use fake container/stream/frame objects so they run with no NDI SDK,
DeckLink card, or PyAV/FFmpeg installed — only the standard library.  They cover
the stability-critical behaviour of the encode thread: clean start/stop, strictly
increasing PTS, surviving per-frame exceptions, dropping video (not blocking) when
backed up, and never blocking the caller forever on audio.

Run directly (`python tests/test_encoder_thread.py`) or under pytest.
"""
import os
import sys
import time
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.encoder_thread import EncoderThread


class FakePacket:
    def __init__(self, pts, is_audio=False):
        self.dts = pts
        self.pts = pts
        self.size = 10
        self.is_keyframe = False
        self.is_audio = is_audio


class FakeStream:
    def __init__(self, audio=False):
        self.codec_context = type("C", (), {"time_base": Fraction(1, 30)})()
        self.time_base = Fraction(1, 30)
        self._audio = audio

    def encode(self, frame=None):
        if frame is None:
            return []                       # flush
        return [FakePacket(frame.pts, is_audio=self._audio)]


class FakeContainer:
    def __init__(self):
        self.video = []
        self.audio = []
        self.closed = False

    def mux(self, pkt):
        (self.audio if getattr(pkt, "is_audio", False) else self.video).append(pkt)

    def close(self):
        self.closed = True


class FakeFrame:
    def __init__(self, is_audio=False):
        self.pts = None
        self.is_audio = is_audio


def make_encoder(events, *, slow_mux=0.0, build_raises=False, maxsize=8):
    cont = {}

    def open_container(path, w, h, fps):
        c = FakeContainer()
        cont["c"] = c
        return c, FakeStream(audio=False), FakeStream(audio=True)

    def close_container(c, vs, as_):
        if c is not None:
            c.close()
        return None, None, None

    def build_video(payload):
        if build_raises:
            raise RuntimeError("boom in build_video")
        if slow_mux:
            time.sleep(slow_mux)
        return FakeFrame(is_audio=False)

    def build_audio(payload):
        return FakeFrame(is_audio=True), 1024

    enc = EncoderThread(
        open_container=open_container,
        close_container=close_container,
        build_video=build_video,
        build_audio=build_audio,
        use_nvenc=False,
        on_status=lambda s: events.append(("status", s)),
        on_error=lambda m: events.append(("error", m)),
        on_stopped=lambda: events.append(("stopped",)),
        log_tag="TEST",
        maxsize=maxsize,
    )
    enc.start()
    return enc, cont


def test_normal_lifecycle():
    events = []
    enc, cont = make_encoder(events)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    t0 = time.monotonic()
    for i in range(10):
        enc.submit_video(b"frame", t0 + i / 30.0)
        enc.submit_audio(b"audio")
    enc.end()
    enc.shutdown()
    enc.join(timeout=5)

    assert not enc.is_alive(), "thread did not exit"
    c = cont["c"]
    assert c.closed, "container not closed"
    assert len(c.video) == 10, f"expected 10 video packets, got {len(c.video)}"
    pts = [p.pts for p in c.video]
    assert pts == sorted(pts) and len(set(pts)) == len(pts), \
        f"PTS not strictly increasing/unique: {pts}"
    assert ("status", "Recording") in events
    assert ("stopped",) in events


def test_build_error_keeps_thread_alive():
    events = []
    enc, cont = make_encoder(events, build_raises=True)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    t0 = time.monotonic()
    for i in range(5):
        enc.submit_video(b"frame", t0 + i / 30.0)
    time.sleep(0.2)
    assert enc.is_alive(), "thread died on a per-frame build error"
    enc.end()
    enc.shutdown()
    enc.join(timeout=5)
    assert not enc.is_alive()
    assert ("stopped",) in events


def test_video_drop_does_not_block():
    events = []
    enc, cont = make_encoder(events, slow_mux=0.05, maxsize=4)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    t0 = time.monotonic()
    start = time.monotonic()
    for i in range(200):
        enc.submit_video(b"frame", t0 + i / 30.0)   # must never block
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"submit_video appears to block ({elapsed:.2f}s)"
    enc.end()
    enc.shutdown()
    enc.join(timeout=10)
    assert not enc.is_alive()


def test_audio_does_not_hang_forever():
    events = []
    enc, cont = make_encoder(events, slow_mux=0.5, maxsize=2)
    enc.begin("out.mp4", 1920, 1080, 29.97)
    t0 = time.monotonic()
    for i in range(5):
        enc.submit_video(b"frame", t0 + i / 30.0)
    start = time.monotonic()
    for i in range(5):
        enc.submit_audio(b"audio")
    elapsed = time.monotonic() - start
    assert elapsed < 11.0, f"submit_audio hung ({elapsed:.2f}s)"
    enc.shutdown()
    enc.join(timeout=10)


if __name__ == "__main__":
    test_normal_lifecycle()
    test_build_error_keeps_thread_alive()
    test_video_drop_does_not_block()
    test_audio_does_not_hang_forever()
    print("ALL ENCODER THREAD TESTS PASSED")
