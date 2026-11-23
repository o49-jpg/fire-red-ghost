"""FireRed (USA v1.0, BPRE) emulator + symbol-aware memory, text, Pokémon decoding, and live ROM patching.

Everything is addressed by the names in pret/pokefirered's pokefirered.sym (exact match for this ROM, SHA1
41cb23d8...), so the AI can say read("gPlayerParty") instead of guessing addresses.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import stable_retro as retro

ROOT = Path(__file__).resolve().parent.parent
GAME = "PokemonFireRed-Gba"
INTEGRATION = ROOT / "integrations" / GAME
ORIGINAL_ROM = ROOT / "roms" / "firered.gba"
LIVE_ROM = INTEGRATION / "rom.gba"          # what the emulator boots; patches land here
SYM_PATH = ROOT / "pokefirered.sym"
DISASM = ROOT / "disasm"
ROM_BASE = 0x08000000

# --- symbols -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Sym:
    name: str
    addr: int
    size: int

def load_symbols(path: Path = SYM_PATH) -> dict[str, Sym]:
    syms: dict[str, Sym] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        addr, _scope, size, name = parts
        if name.startswith(".") or name.startswith("$"):
            continue
        s = Sym(name, int(addr, 16), int(size, 16))
        if name not in syms or (syms[name].size == 0 and s.size):
            syms[name] = s
    return syms

# --- text (charmap) --------------------------------------------------------------------------------------------
def load_charmap() -> tuple[dict[int, str], dict[str, int]]:
    dec: dict[int, str] = {}
    enc: dict[str, int] = {}
    pat = re.compile(r"^'(.+?)'\s*=\s*([0-9A-Fa-f]{2})\s*$")
    for line in (DISASM / "charmap.txt").read_text(encoding="utf-8").splitlines():
        m = pat.match(line.split("@")[0].strip())
        if not m:
            continue
        ch, code = m.group(1), int(m.group(2), 16)
        if ch.startswith("\\"):
            ch = {"\\n": "\n", "\\l": "\n", "\\p": "\n\n"}.get(ch, "")
        dec.setdefault(code, ch)
        if len(ch) == 1:
            enc.setdefault(ch, code)
    return dec, enc

CHAR_DEC, CHAR_ENC = load_charmap()
EOS = 0xFF

def decode_text(raw: bytes) -> str:
    out = []
    for b in raw:
        if b == EOS:
            break
        out.append(CHAR_DEC.get(b, "?"))
    return "".join(out)

def encode_text(s: str, length: int | None = None) -> bytes:
    raw = bytes(CHAR_ENC.get(c, CHAR_ENC.get("?", 0xAC)) for c in s) + bytes([EOS])
    if length is not None:
        raw = raw[:length].ljust(length, bytes([EOS]))
    return raw

# --- the machine ---------------------------------------------------------------------------------------------
class Game:
    """One emulator. RAM by symbol name, ROM patching with a live hot-swap."""

    REGIONS = (0x02000000, 0x03000000, 0x04000000, 0x05000000, 0x06000000, 0x07000000, 0x0E000000)

    def __init__(self, state: str | None = "Bedroom"):
        retro.data.Integrations.add_custom_path(str(ROOT / "integrations"))
        self.syms = load_symbols()
        if not LIVE_ROM.exists():
            LIVE_ROM.write_bytes(ORIGINAL_ROM.read_bytes())
        self.rom = bytearray(LIVE_ROM.read_bytes())
        self.rom_dirty: list[tuple[int, int]] = []       # (offset, length) since last hot-swap
        self.state_name = state
        self._make(state)

    def _make(self, state, raw_state: bytes | None = None):
        (INTEGRATION / "rom.sha").write_text(hashlib.sha1(LIVE_ROM.read_bytes()).hexdigest() + "\n")
        st = retro.State.NONE if state is None else state
        # render_mode=None: stable-retro otherwise draws every frame into its own pyglet window (~1.7 ms/frame)
        self.env = retro.make(GAME, state=st, inttype=retro.data.Integrations.ALL,
                              use_restricted_actions=retro.Actions.ALL, render_mode=None)
        self.obs, _ = self.env.reset()
        if raw_state is not None:
            self.env.em.set_state(raw_state)
        self.buttons = self.env.buttons
        self.mem = self.env.unwrapped.data.memory

    # frames
    def step(self, pressed: set[str] = frozenset(), frames: int = 1):
        a = np.zeros(len(self.buttons), np.int8)
        for b in pressed:
            if b in self.buttons:
                a[self.buttons.index(b)] = 1
        for _ in range(frames):
            self.obs, *_ = self.env.step(a)
        return self.obs

    def screen(self) -> np.ndarray:
        return self.obs

    def save_state(self) -> bytes:
        return self.env.em.get_state()

    def load_state(self, raw: bytes):
        self.env.em.set_state(raw)

    # memory
    def _block(self, addr: int) -> tuple[bytes, int]:
        for base in self.REGIONS:
            blk = self.mem.blocks.get(base)
            if blk is not None and base <= addr < base + len(blk):
                return blk, addr - base
        raise ValueError(f"address {addr:#010x} isn't in emulated RAM (ROM lives at 0x08xxxxxx: use rom_read)")

    def read(self, addr: int, n: int) -> bytes:
        if addr >= ROM_BASE and addr < ROM_BASE + len(self.rom):
            return self.rom_read(addr, n)
        blk, off = self._block(addr)
        return bytes(blk[off:off + n])

    def write(self, addr: int, data: bytes):
        if addr >= ROM_BASE:
            return self.rom_patch(addr, data)
        for i, b in enumerate(data):                # memory.assign is per value; bytes keep it simple and exact
            self.mem.assign(addr + i, "|u1", b)

    def u8(self, a): return self.read(a, 1)[0]
    def u16(self, a): return struct.unpack("<H", self.read(a, 2))[0]
    def u32(self, a): return struct.unpack("<I", self.read(a, 4))[0]

    def sym(self, name: str) -> Sym:
        if name not in self.syms:
            close = [n for n in self.syms if name.lower() in n.lower()][:8]
            raise KeyError(f"no symbol {name!r}" + (f"; close: {close}" if close else ""))
        return self.syms[name]

    def find_symbols(self, pattern: str, limit: int = 30) -> list[Sym]:
        rx = re.compile(pattern, re.I)
        return [s for s in self.syms.values() if rx.search(s.name)][:limit]

    def symbol_at(self, addr: int) -> str:
        best = None
        for s in self.syms.values():
            if s.addr <= addr < s.addr + max(1, s.size) and (best is None or s.size < best.size):
                best = s
        return f"{best.name}+{addr - best.addr:#x}" if best else f"{addr:#010x}"

    # ROM
    def rom_read(self, addr: int, n: int) -> bytes:
        off = addr - ROM_BASE
        return bytes(self.rom[off:off + n])

    def rom_patch(self, addr: int, data: bytes):
        off = addr - ROM_BASE
        if not (0 <= off and off + len(data) <= len(self.rom)):
            raise ValueError("ROM patch out of range")
        self.rom[off:off + len(data)] = data
        self.rom_dirty.append((off, len(data)))

    def hot_swap(self) -> int:
        """Apply pending ROM patches: snapshot the running game, write the patched ROM, rebuild the emulator,
        restore the snapshot. RAM/CPU state is untouched; the game simply keeps running on the new ROM."""
        if not self.rom_dirty:
            return 0
        n = len(self.rom_dirty)
        snap = self.save_state()
        self.env.close()
        LIVE_ROM.write_bytes(bytes(self.rom))
        self._make(self.state_name, raw_state=snap)
        self.rom_dirty.clear()
        return n

    def rom_restore(self):
        """Undo every ROM hack (back to the cartridge) and hot-swap."""
        self.rom = bytearray(ORIGINAL_ROM.read_bytes())
        self.rom_dirty.append((0, len(self.rom)))
        return self.hot_swap()

    def close(self):
        self.env.close()


# --- Pokémon decoding (party structs are partly encrypted; layout from include/pokemon.h) ----------------------
SUBSTRUCT_ORDERS = ["GAEM", "GAME", "GEAM", "GEMA", "GMAE", "GMEA", "AGEM", "AGME", "AEGM", "AEMG", "AMGE", "AMEG",
                    "EGAM", "EGMA", "EAGM", "EAMG", "EMGA", "EMAG", "MGAE", "MGEA", "MAGE", "MAEG", "MEGA", "MEAG"]
PARTY_SIZE, MON_SIZE = 6, 100

def decode_mon(raw: bytes) -> dict | None:
    personality, otid = struct.unpack_from("<II", raw, 0)
    if personality == 0 and otid == 0:
        return None
    key = personality ^ otid
    enc = raw[32:80]
    dec = bytearray()
    for i in range(0, 48, 4):
        dec += struct.pack("<I", struct.unpack_from("<I", enc, i)[0] ^ key)
    order = SUBSTRUCT_ORDERS[personality % 24]
    sub = {k: dec[i * 12:(i + 1) * 12] for i, k in enumerate(order)}
    species, item, exp = struct.unpack_from("<HHI", sub["G"], 0)
    moves = struct.unpack_from("<HHHH", sub["A"], 0)
    pp = tuple(sub["A"][8:12])
    level = raw[84]
    hp, max_hp, atk, dfn, spe, spa, spd = struct.unpack_from("<HHHHHHH", raw, 86)
    return {"species_id": species, "item_id": item, "exp": exp, "moves": moves, "pp": pp, "level": level,
            "hp": hp, "max_hp": max_hp, "atk": atk, "def": dfn, "spe": spe, "spa": spa, "spd": spd,
            "nickname": decode_text(raw[8:18]), "personality": personality}
