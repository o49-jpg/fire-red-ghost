"""Hack feedback: screen glitches, synthesized SFX, and the ghost's processed espeak-ng voice."""
from __future__ import annotations

import io
import os
import queue
import subprocess
import threading
import wave

import numpy as np

# ---------------------------------------------------------------------------------------------------------------
# visuals: operate on the 240x160 RGB frame before it's scaled up
class Glitch:
    def __init__(self):
        self.frames_left = 0
        self.total = 1
        self.intensity = 0.0
        self.rng = np.random.default_rng()

    def trigger(self, intensity: float, frames: int):
        """intensity 0-1: RAM poke ~0.3, ROM hot-swap ~0.9."""
        if intensity >= self.intensity * (self.frames_left / max(1, self.total)):
            self.intensity, self.frames_left, self.total = min(1.0, intensity), frames, frames

    def apply(self, frame: np.ndarray) -> np.ndarray:
        if self.frames_left <= 0:
            return frame
        k = self.intensity * (self.frames_left / self.total) ** 0.6
        self.frames_left -= 1
        f = frame.copy()
        h, w, _ = f.shape
        r = self.rng
        # 1. torn scanlines: horizontal slices shoved sideways
        for _ in range(int(3 + 25 * k)):
            y = r.integers(0, h); t = r.integers(1, 1 + int(2 + 14 * k))
            f[y:y + t] = np.roll(f[y:y + t], r.integers(-int(4 + 50 * k), int(5 + 50 * k)), axis=1)
        # 2. RGB channel split
        s = int(1 + 8 * k)
        f[..., 0] = np.roll(f[..., 0], s, axis=1)
        f[..., 2] = np.roll(f[..., 2], -s, axis=1)
        # 3. corrupted tiles: random 8x8 blocks copied from elsewhere / colour-crushed
        for _ in range(int(20 * k)):
            y, x = r.integers(0, h // 8) * 8, r.integers(0, w // 8) * 8
            sy, sx = r.integers(0, h // 8) * 8, r.integers(0, w // 8) * 8
            f[y:y + 8, x:x + 8] = f[sy:sy + 8, sx:sx + 8] if r.random() < 0.6 else (f[y:y + 8, x:x + 8] ^ 0xE0)
        # 4. inverted scanline bands on big hacks
        if k > 0.5 and r.random() < 0.5:
            y, t = r.integers(0, h - 6), r.integers(2, 6)
            f[y:y + t] = 255 - f[y:y + t]
        return f

# ---------------------------------------------------------------------------------------------------------------
# audio: everything is synthesized at the mixer rate, int16 stereo
def _stereo(x: np.ndarray, gain: float = 0.35) -> np.ndarray:
    x = np.clip(x * gain, -1, 1)
    s = (x * 32767).astype(np.int16)
    return np.ascontiguousarray(np.stack([s, s], axis=1))

def sfx_poke(rate: int) -> np.ndarray:
    """Short data-chirp: a few random square-wave blips."""
    out = []
    for f in np.random.default_rng().choice([880, 1320, 1760, 2640, 3520], 5):
        t = np.arange(int(rate * 0.025)) / rate
        out.append(np.sign(np.sin(2 * np.pi * f * t)) * np.linspace(1, 0.3, t.size))
    return _stereo(np.concatenate(out), 0.25)

def sfx_hack(rate: int, seconds: float = 0.9) -> np.ndarray:
    """Big hack: falling modem sweep + bit-crushed noise bursts + a sub thump."""
    n = int(rate * seconds)
    t = np.arange(n) / rate
    sweep = np.sign(np.sin(2 * np.pi * np.cumsum(np.linspace(2400, 120, n)) / rate))
    noise = np.random.default_rng().uniform(-1, 1, n)
    gate = (np.sin(2 * np.pi * 23 * t) > 0.3).astype(float)
    crushed = np.round(noise * 3) / 3 * gate
    thump = np.sin(2 * np.pi * 55 * t) * np.exp(-t * 6)
    x = 0.45 * sweep * np.exp(-t * 1.5) + 0.35 * crushed + 0.6 * thump
    return _stereo(x, 0.45)

def sfx_boot(rate: int) -> np.ndarray:
    t = np.arange(int(rate * 0.6)) / rate
    x = np.sin(2 * np.pi * (220 + 660 * t) * t) * np.exp(-t * 3) + 0.2 * np.sign(np.sin(2 * np.pi * 7 * t))
    return _stereo(x, 0.3)

class Voice:
    """espeak-ng -> wav -> ring-mod + bit-crush -> mixer channel. Runs on its own thread, one line at a time."""

    def __init__(self, rate: int, play, voice: str | None = None):
        self.rate, self.play = rate, play
        self.voice = voice or os.environ.get("GHOST_VOICE", "en-us+m3")
        self.q: queue.Queue = queue.Queue()
        self.speaking = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, text: str) -> threading.Event:
        """Queue a line; the returned Event is set once it has finished playing."""
        done = threading.Event()
        text = " ".join(text.split())[:400]
        if text:
            self.q.put((text, done))
        else:
            done.set()
        return done

    def _render(self, text: str) -> np.ndarray | None:
        try:
            wav = subprocess.run(["espeak-ng", "-v", self.voice, "-s", "165", "-p", "28", "--stdout", text],
                                 capture_output=True, timeout=20).stdout
            with wave.open(io.BytesIO(wav)) as w:
                src_rate = w.getframerate()
                x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        except (OSError, subprocess.TimeoutExpired, wave.Error, EOFError):
            return None
        n = int(len(x) * self.rate / src_rate)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)       # resample to mixer rate
        t = np.arange(n) / self.rate
        x = 0.75 * x + 0.35 * x * np.sin(2 * np.pi * 70 * t)                       # ring mod: metallic
        x = np.round(x * 24) / 24                                                  # bit-crush
        stutter = np.random.default_rng().random() < 0.5 and n > self.rate         # occasional data stutter
        if stutter:
            a = int(self.rate * 0.08)
            cut = np.random.default_rng().integers(0, n - a)
            x = np.concatenate([x[:cut + a], x[cut:cut + a], x[cut:]])
        return _stereo(x, 0.9)

    def _run(self):
        while True:
            text, done = self.q.get()
            self.speaking.set()
            try:
                pcm = self._render(text)
                if pcm is not None:
                    self.play(pcm)            # blocks for the clip's length
            finally:
                self.speaking.clear()
                done.set()


# ---------------------------------------------------------------------------------------------------------------
# grayscale vision helpers, re-exported so the runtime only imports one ghost module
def vision_png(frame):
    from . import vision
    return vision.png_bytes(frame)


def vision_ascii(frame):
    from . import vision
    return vision.ascii_map(frame, 56, 20)
