"""Grayscale vision for the ghost.

The game is a 240x160 RGB frame. We hand the LLM a luminance-only version: cheap, distinctive, and legible
at native resolution. Text-only fallback models get an ASCII luminance map instead of the image.

    frame = game.screen()                     # (160, 240, 3) uint8 RGB
    vision.image_content(frame)               # OpenAI content parts: text + image_url (grayscale PNG)
    vision.ascii_map(frame)                   # compact text fallback
"""
from __future__ import annotations

import base64
import io

import numpy as np

RAMP = " .:-=+*#%@"          # dark -> bright


def grayscale(frame: np.ndarray) -> np.ndarray:
    """Luma (Rec.601) as uint8. The GBA's palette is already chunky, so no smoothing."""
    a = np.asarray(frame, dtype=np.uint8)
    if a.ndim == 2:
        return a
    wide = a.astype(np.uint16)
    return ((wide[..., 0] * 77 + wide[..., 1] * 150 + wide[..., 2] * 29) // 256).astype(np.uint8)


def _block_reduce(gray: np.ndarray, block: int) -> np.ndarray:
    h, w = gray.shape
    h -= h % block
    w -= w % block
    if h == 0 or w == 0:
        return gray.astype(np.float32)
    return gray[:h, :w].reshape(h // block, block, w // block, block).mean(axis=(1, 3))


def ascii_map(frame: np.ndarray, cols: int = 60, rows: int = 22) -> str:
    """Luminance ASCII art, aspect-corrected (a text cell is about twice as tall as wide)."""
    g = _block_reduce(grayscale(frame), 2)
    if g.size == 0:
        return ""
    # the 240x160 image is 3:2; a 60x22 char grid at 2:1 cells is close enough
    ys = np.linspace(0, g.shape[0] - 1, rows).astype(int)
    xs = np.linspace(0, g.shape[1] - 1, cols).astype(int)
    small = g[np.ix_(ys, xs)]
    idx = np.clip((small / 256 * len(RAMP)).astype(int), 0, len(RAMP) - 1)
    return "\n".join("".join(RAMP[v] for v in row) for row in idx)


def png_bytes(frame: np.ndarray) -> bytes:
    """Grayscale PNG of the native 240x160 frame, using only stdlib + numpy (PIL is available too)."""
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(grayscale(frame), mode="L").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def image_content(frame: np.ndarray, caption: str = "") -> list[dict]:
    """OpenAI-compatible user message parts: a text observation plus the grayscale frame."""
    b64 = base64.b64encode(png_bytes(frame)).decode("ascii")
    parts: list[dict] = []
    if caption:
        parts.append({"type": "text", "text": caption})
    parts.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64, "detail": "low"}})
    return parts


def image_content_b64(b64png: str, caption: str = "") -> list[dict]:
    """Like image_content, but for a frame that is already a base64 PNG."""
    parts: list[dict] = []
    if caption:
        parts.append({"type": "text", "text": caption})
    parts.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64png, "detail": "low"}})
    return parts


def strip_images(content) -> str:
    """Turn a multimodal message back into plain text for a text-only fallback model."""
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "[grayscale image: see ascii_vision field]") for p in content)


def model_sees_images(model: str) -> bool:
    """Only send image parts to models that advertise image input (checked via OpenRouter's model catalog)."""
    if model in _VISION_CACHE:
        return _VISION_CACHE[model]
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=12) as r:
            data = json.load(r)
        mods = {m["id"]: set(m.get("architecture", {}).get("input_modalities") or [])
                for m in data.get("data", [])}
        sees = "image" in mods.get(model, set())
    except Exception:
        # a free model known to be multimodal; never send images to text-only fallbacks
        sees = "image" in model.lower() or "vision" in model.lower() or ":free" in model
    _VISION_CACHE[model] = sees
    return sees


_VISION_CACHE: dict[str, bool] = {}
