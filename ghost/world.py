"""Game knowledge on top of ghost.core: a readable world snapshot + the hacking primitives.

Layouts come from pret/pokefirered: include/global.h (SaveBlock1/2), include/pokemon.h (BoxPokemon,
SpeciesInfo, BattleMove), src/battle_main.c (gTypeEffectiveness: [atk, def, mult] triples, 0xFE foresight
separator, 0xFF end).
"""
from __future__ import annotations

import colorsys
import json
import random
import struct

import numpy as np

from .core import (DISASM, PARTY_SIZE, MON_SIZE, ROM_BASE, SUBSTRUCT_ORDERS, Game, decode_mon, decode_text,
                   encode_text)

TYPE_NAME_LEN, MOVE_NAME_LEN, SPECIES_NAME_LEN = 7, 13, 11
N_TYPES, N_MOVES = 18, 355
SB1_POS, SB1_LOC, SB1_MONEY = 0x0000, 0x0004, 0x0290
SB2_NAME, SB2_TID, SB2_KEY = 0x000, 0x00A, 0xF20
MAIN_INBATTLE_BYTE = 0x439           # gMain + 0x439, bit 1 (after oamLoadDisabled:1)
TYPE_FORESIGHT, TYPE_END = 0xFE, 0xFF
MULTS = {0: 0, 0.0: 0, 0.5: 5, 1: 10, 1.0: 10, 2: 20, 2.0: 20}
NATURE_STATS = ["atk", "def", "spe", "spa", "spd"]


def game_chart_file():
    from .core import INTEGRATION
    return INTEGRATION / "type_chart_addr.txt"


class World:
    def __init__(self, game: Game):
        self.g = game
        mg = json.loads((DISASM / "data" / "maps" / "map_groups.json").read_text())
        self.map_groups = [mg[name] for name in mg["group_order"]]
        # a relocated type chart lives in free ROM; its address must survive restarts (hacks persist on the ROM)
        self.chart_file = game_chart_file()
        self.relocated_chart: int | None = (int(self.chart_file.read_text(), 16)
                                            if self.chart_file.exists() else None)
        self.persistent_palette: dict | None = None

    # ---- names from ROM tables (so hacks show up here too) ----------------------------------------------------
    def _name_table(self, sym: str, width: int, count: int) -> list[str]:
        base = self.g.sym(sym).addr
        return [decode_text(self.g.rom_read(base + i * width, width)) for i in range(count)]

    def type_names(self): return self._name_table("gTypeNames", TYPE_NAME_LEN, N_TYPES)
    def move_name(self, i): return decode_text(self.g.rom_read(self.g.sym("gMoveNames").addr + i * MOVE_NAME_LEN, MOVE_NAME_LEN))
    def species_name(self, i): return decode_text(self.g.rom_read(self.g.sym("gSpeciesNames").addr + i * SPECIES_NAME_LEN, SPECIES_NAME_LEN))

    def resolve(self, kind: str, ref) -> int:
        """Name or number -> id, for 'type' | 'move' | 'species'."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
            return int(ref)
        want = str(ref).strip().upper().replace(" ", "")
        if kind == "type":
            names = self.type_names()
        elif kind == "move":
            names = [self.move_name(i) for i in range(N_MOVES)]
        else:
            names = [self.species_name(i) for i in range(412)]
        for i, n in enumerate(names):
            if n.upper().replace(" ", "") == want:
                return i
        raise KeyError(f"no {kind} named {ref!r}")

    # ---- snapshot ---------------------------------------------------------------------------------------------
    def sb1(self): return self.g.u32(self.g.sym("gSaveBlock1Ptr").addr)
    def sb2(self): return self.g.u32(self.g.sym("gSaveBlock2Ptr").addr)

    def location(self) -> tuple[int, int, str]:
        b = self.sb1()
        grp, num = self.g.u8(b + SB1_LOC), self.g.u8(b + SB1_LOC + 1)
        try:
            name = self.map_groups[grp][num]
        except IndexError:
            name = f"map {grp}.{num}"
        return grp, num, name

    def position(self) -> tuple[int, int]:
        b = self.sb1()
        return self.g.u16(b + SB1_POS), self.g.u16(b + SB1_POS + 2)

    def money(self) -> int:
        return self.g.u32(self.sb1() + SB1_MONEY) ^ self.g.u32(self.sb2() + SB2_KEY)

    def in_battle(self) -> bool:
        return bool(self.g.u8(self.g.sym("gMain").addr + MAIN_INBATTLE_BYTE) & 0x02)

    def party(self, enemy: bool = False) -> list[dict]:
        base = self.g.sym("gEnemyParty" if enemy else "gPlayerParty").addr
        n = PARTY_SIZE if enemy else min(PARTY_SIZE, self.g.u8(self.g.sym("gPlayerPartyCount").addr))
        out = []
        for i in range(n):
            m = decode_mon(self.g.read(base + i * MON_SIZE, MON_SIZE))
            if m:
                m["species"] = self.species_name(m["species_id"])
                m["move_names"] = [self.move_name(x) for x in m["moves"] if x]
                out.append(m)
        return out

    def snapshot_light(self) -> dict:
        """Cheap per-second status for the HUD (no ROM name-table reads)."""
        _g, _n, name = self.location()
        return {"map": name, "pos": list(self.position()), "money": self.money(), "in_battle": self.in_battle()}

    def snapshot(self) -> dict:
        grp, num, name = self.location()
        x, y = self.position()
        battle = self.in_battle()
        snap = {
            "player": decode_text(self.g.read(self.sb2() + SB2_NAME, 8)),
            "map": name, "map_id": f"{grp}.{num}", "pos": [x, y], "money": self.money(),
            "in_battle": battle,
            "party": [f"{m['nickname']} ({m['species']}) L{m['level']} {m['hp']}/{m['max_hp']}HP "
                      f"moves {m['move_names']}" for m in self.party()],
            "screen_text": decode_text(self.g.read(self.g.sym("gStringVar4").addr, 200)).replace("\n", " ")[:160],
            "types": self.type_names(),
        }
        if battle:
            snap["enemy"] = [f"{m['species']} L{m['level']} {m['hp']}/{m['max_hp']}HP" for m in self.party(enemy=True)][:1]
        return snap

    # ---- hacks: text / types --------------------------------------------------------------------------------
    def set_type_name(self, type_ref, name: str) -> str:
        t = self.resolve("type", type_ref)
        old = self.type_names()[t]
        self.g.rom_patch(self.g.sym("gTypeNames").addr + t * TYPE_NAME_LEN, encode_text(name.upper()[:6], TYPE_NAME_LEN))
        return f"type {t} {old} -> {name.upper()[:6]}"

    def chart_addr(self) -> int:
        return self.relocated_chart or self.g.sym("gTypeEffectiveness").addr

    def type_chart(self) -> list[tuple[int, int, int]]:
        a, out = self.chart_addr(), []
        while True:
            atk = self.g.rom_read(a, 1)[0]
            if atk == TYPE_END:
                return out
            d, mul = self.g.rom_read(a + 1, 2)
            out.append((atk, d, mul))
            a += 3

    def _free_rom(self, size: int) -> int:
        """End-of-ROM 0xFF padding, aligned; never overlaps a symbol."""
        rom = self.g.rom
        last_sym_end = max(s.addr + s.size for s in self.g.syms.values() if s.addr >= ROM_BASE) - ROM_BASE
        start = (last_sym_end + 0x1000) & ~0xFFF
        while start + size < len(rom):
            if rom[start:start + size] == b"\xff" * size:
                return ROM_BASE + start
            start += 0x1000
        raise RuntimeError("no free ROM space")

    def set_type_matchup(self, attacker, defender, multiplier: float) -> str:
        """Add/replace one chart entry. The table is fixed-size in ROM, so it's rebuilt in free space and every
        pointer to it (literal pools in the battle code) is repointed."""
        atk, dfn = self.resolve("type", attacker), self.resolve("type", defender)
        if multiplier not in MULTS:
            raise ValueError("multiplier must be 0, 0.5, 1 or 2")
        mul = MULTS[multiplier]
        entries = self.type_chart()
        fore = next((i for i, e in enumerate(entries) if e[0] == TYPE_FORESIGHT), len(entries))
        main, tail = [e for e in entries[:fore] if not (e[0] == atk and e[1] == dfn)], entries[fore:]
        if mul != 10:
            main.append((atk, dfn, mul))
        blob = b"".join(bytes(e) for e in main + tail) + bytes([TYPE_END, TYPE_END, TYPE_END])
        old = self.chart_addr()
        new = self.relocated_chart or self._free_rom(0x800)
        self.g.rom_patch(new, blob)
        refs = 0
        if new != old:
            needle = struct.pack("<I", old)
            rom = self.g.rom
            i = rom.find(needle)
            while i != -1:
                if i % 4 == 0:
                    self.g.rom_patch(ROM_BASE + i, struct.pack("<I", new))
                    refs += 1
                i = rom.find(needle, i + 1)
            self.relocated_chart = new
            self.chart_file.write_text(f"{new:#x}\n")
        names = self.type_names()
        return (f"{names[atk]} -> {names[dfn]} = x{multiplier}" +
                (f" (chart relocated to {new:#x}, {refs} pointer(s) repointed)" if refs else ""))

    # ---- hacks: moves / species -----------------------------------------------------------------------------
    def set_move(self, move_ref, name=None, power=None, type=None, accuracy=None, pp=None, priority=None,
                 effect=None) -> str:
        m = self.resolve("move", move_ref)
        base = self.g.sym("gBattleMoves").addr + m * 12
        cur = bytearray(self.g.rom_read(base, 12))
        changes = []
        for off, val, label in ((1, power, "power"), (3, accuracy, "accuracy"), (4, pp, "pp"), (0, effect, "effect")):
            if val is not None:
                cur[off] = max(0, min(255, int(val))); changes.append(f"{label} {val}")
        if type is not None:
            cur[2] = self.resolve("type", type); changes.append(f"type {self.type_names()[cur[2]]}")
        if priority is not None:
            cur[7] = int(priority) & 0xFF; changes.append(f"priority {priority}")
        self.g.rom_patch(base, bytes(cur))
        old = self.move_name(m)
        if name:
            self.g.rom_patch(self.g.sym("gMoveNames").addr + m * MOVE_NAME_LEN, encode_text(name.upper()[:12], MOVE_NAME_LEN))
            changes.append(f"renamed {name.upper()[:12]}")
        return f"move {m} {old}: " + ", ".join(changes)

    def set_species(self, species_ref, name=None, types=None, stats=None) -> str:
        s = self.resolve("species", species_ref)
        base = self.g.sym("gSpeciesInfo").addr + s * 28
        cur = bytearray(self.g.rom_read(base, 28))
        changes = []
        if stats:
            for i, k in enumerate(["hp", "atk", "def", "spe", "spa", "spd"]):
                if k in stats:
                    cur[i] = max(1, min(255, int(stats[k])))
            changes.append(f"stats {list(cur[:6])}")
        if types:
            t = [self.resolve("type", x) for x in types][:2]
            cur[6], cur[7] = t[0], t[-1]
            changes.append(f"types {[self.type_names()[x] for x in (cur[6], cur[7])]}")
        self.g.rom_patch(base, bytes(cur))
        old = self.species_name(s)
        if name:
            self.g.rom_patch(self.g.sym("gSpeciesNames").addr + s * SPECIES_NAME_LEN, encode_text(name.upper()[:10], SPECIES_NAME_LEN))
            changes.append(f"renamed {name.upper()[:10]}")
        return f"species {s} {old}: " + ", ".join(changes)

    # ---- hacks: create a Pokémon (valid checksum + encryption) ----------------------------------------------
    def _learnset(self, species: int, level: int) -> list[int]:
        ptr = self.g.u32(self.g.sym("gLevelUpLearnsets").addr + species * 4)
        moves, a = [], ptr
        while True:
            v = struct.unpack("<H", self.g.rom_read(a, 2))[0]
            if v == 0xFFFF:
                break
            if (v >> 9) <= level and (v & 0x1FF) not in moves:
                moves.append(v & 0x1FF)
            a += 2
        return moves[-4:] or [33]          # TACKLE

    def give_pokemon(self, species_ref, level: int = 5, moves: list | None = None, nickname: str | None = None) -> str:
        count_addr = self.g.sym("gPlayerPartyCount").addr
        count = self.g.u8(count_addr)
        if count >= PARTY_SIZE:
            return "party is full"
        sp = self.resolve("species", species_ref)
        level = max(1, min(100, int(level)))
        info = self.g.rom_read(self.g.sym("gSpeciesInfo").addr + sp * 28, 28)
        base = dict(zip(["hp", "atk", "def", "spe", "spa", "spd"], info[:6]))
        growth = info[0x13]
        exp = self.g.u32(self.g.sym("gExperienceTables").addr + (growth * 101 + level) * 4)
        mv = [self.resolve("move", m) for m in moves][:4] if moves else self._learnset(sp, level)
        pps = [self.g.rom_read(self.g.sym("gBattleMoves").addr + m * 12 + 4, 1)[0] for m in mv]
        mv += [0] * (4 - len(mv)); pps += [0] * (4 - len(pps))
        personality = random.getrandbits(32)
        sb2 = self.sb2()
        otid = self.g.u32(sb2 + SB2_TID)
        ivs = {k: random.randint(0, 31) for k in base}
        nature = personality % 25
        up, down = NATURE_STATS[nature // 5], NATURE_STATS[nature % 5]
        stats = {"hp": (2 * base["hp"] + ivs["hp"]) * level // 100 + level + 10}
        for k in NATURE_STATS:
            v = (2 * base[k] + ivs[k]) * level // 100 + 5
            if up != down:
                v = v * 11 // 10 if k == up else v * 9 // 10 if k == down else v
            stats[k] = v
        G = struct.pack("<HHIBBH", sp, 0, exp, 0, 70, 0)
        A = struct.pack("<HHHHBBBB", *mv, *pps)
        E = bytes(12)
        iv_word = (ivs["hp"] | ivs["atk"] << 5 | ivs["def"] << 10 | ivs["spe"] << 15 | ivs["spa"] << 20 | ivs["spd"] << 25)
        M = struct.pack("<BBHII", 0, 0, level | (4 << 7) | (4 << 11), iv_word, 0)   # metLevel, FireRed, Poké Ball
        sub = {"G": G, "A": A, "E": E, "M": M}
        plain = b"".join(sub[k] for k in SUBSTRUCT_ORDERS[personality % 24])
        checksum = sum(struct.unpack("<24H", plain)) & 0xFFFF
        key = personality ^ otid
        enc = b"".join(struct.pack("<I", struct.unpack_from("<I", plain, i)[0] ^ key) for i in range(0, 48, 4))
        nick = encode_text((nickname or self.species_name(sp)).upper()[:10], 10)
        box = (struct.pack("<II", personality, otid) + nick + bytes([2, 0x02]) +
               self.g.read(sb2 + SB2_NAME, 7) + bytes([0]) + struct.pack("<HH", checksum, 0) + enc)
        party = box + struct.pack("<IBBHHHHHHH", 0, level, 0xFF, stats["hp"], stats["hp"], stats["atk"],
                                  stats["def"], stats["spe"], stats["spa"], stats["spd"])
        assert len(party) == MON_SIZE
        self.g.write(self.g.sym("gPlayerParty").addr + count * MON_SIZE, party)
        self.g.write(count_addr, bytes([count + 1]))
        return f"created {self.species_name(sp)} L{level} in party slot {count + 1} (moves {[self.move_name(m) for m in mv if m]})"

    # ---- hacks: the player's own body (overworld sprite) ----------------------------------------------------
    # Red's walking sheet: 9 frames of 16x32 px, 4bpp, 2x4 tiles each (0x100 bytes/frame) + one 16-colour palette.
    # (sheet, first frame) that hold a 9-frame walk-style cycle; running = RedSurfRun frames 3-11
    # (src/data/object_events/object_event_pic_tables.h, sPicTable_RedNormal entries 9-17)
    PLAYER_PICS = (("gObjectEventPic_RedNormal", 0), ("gObjectEventPic_RedSurfRun", 3))
    FRAME_BYTES, FRAMES = 0x100, 9

    def _player_pal(self) -> int:
        return self.g.sym("gObjectEventPal_Player").addr

    def _npc_sprite(self, name: str) -> tuple[bytes, bytes]:
        """(pic 0x900 bytes, palette 32 bytes) of another 16x32 overworld character."""
        info = self.g.sym(f"gObjectEventGraphicsInfo_{name}").addr
        pal_tag = struct.unpack("<H", self.g.rom_read(info + 2, 2))[0]
        width, height = struct.unpack("<hh", self.g.rom_read(info + 8, 4))
        if (width, height) != (16, 32):
            raise ValueError(f"{name} is {width}x{height}; only 16x32 people fit your body")
        images = self.g.u32(info + 0x1C)
        first = self.g.u32(images)                               # SpriteFrameImage[0].data
        owner = next((s for s in self.g.syms.values() if s.addr == first and s.size), None)
        if owner is None or owner.size < self.FRAME_BYTES * self.FRAMES:
            raise ValueError(f"{name}'s sprite sheet isn't a standard 9-frame walker")
        pic = self.g.rom_read(first, self.FRAME_BYTES * self.FRAMES)
        table = self.g.sym("sObjectEventSpritePalettes")
        for i in range(table.size // 8):
            ptr, tag = struct.unpack("<IH", self.g.rom_read(table.addr + i * 8, 6))
            if tag == pal_tag and ptr:
                return pic, self.g.rom_read(ptr, 32)
        raise ValueError(f"no palette found for {name} (tag {pal_tag:#x})")

    @staticmethod
    def _png_sprite(path) -> tuple[bytes, bytes]:
        from PIL import Image
        img = Image.open(path).convert("RGBA")
        if img.size == (16, 32):
            frames = [img] * 9
        elif img.size == (144, 32):
            frames = [img.crop((i * 16, 0, i * 16 + 16, 32)) for i in range(9)]
        else:
            raise ValueError(f"{path.name} is {img.size[0]}x{img.size[1]}: need 16x32 or a 144x32 sheet of 9 frames")
        sheet = Image.new("RGBA", (144, 32))
        for i, f in enumerate(frames):
            sheet.paste(f, (i * 16, 0))
        alpha = np.array(sheet)[..., 3] < 128
        q = sheet.convert("RGB").quantize(colors=15)
        idx = np.array(q, np.uint8) + 1                          # palette index 0 = transparent
        idx[alpha] = 0
        pal_rgb = (q.getpalette() or [])[:45] + [0] * 45
        pal = [0x7C1F] + [((pal_rgb[i * 3] >> 3) | (pal_rgb[i * 3 + 1] >> 3) << 5 | (pal_rgb[i * 3 + 2] >> 3) << 10)
                          for i in range(15)]
        pic = bytearray()
        for f in range(9):
            for ty in range(4):                                  # 2x4 tiles, row-major (1D OBJ mapping)
                for tx in range(2):
                    for y in range(8):
                        row = idx[ty * 8 + y, f * 16 + tx * 8: f * 16 + tx * 8 + 8]
                        pic += bytes((int(row[i]) & 15) | (int(row[i + 1]) & 15) << 4 for i in range(0, 8, 2))
        return bytes(pic), struct.pack("<16H", *pal)

    def _live_player_palette(self, old: bytes, new: bytes) -> int:
        """Swap the player's palette in the palette buffers right now (no map reload needed)."""
        hits = 0
        for sym in ("gPlttBufferUnfaded", "gPlttBufferFaded"):
            base = self.g.sym(sym).addr + 0x200                  # OBJ palettes
            for slot in range(16):
                a = base + slot * 32
                if self.g.read(a + 2, 30) == old[2:]:
                    self.g.write(a, new); hits += 1
        return hits

    def set_player_sprite(self, source: str, hue: float = 0.0) -> str:
        from .core import ORIGINAL_ROM, ROOT
        old_pal = self.g.rom_read(self._player_pal(), 32)
        src = source.strip()
        if src == "restore":
            orig = ORIGINAL_ROM.read_bytes()
            pic = orig[self.g.sym(self.PLAYER_PICS[0][0]).addr - ROM_BASE:][:0x900]
            pal = orig[self._player_pal() - ROM_BASE:][:32]
            label = "your original body"
        elif src == "glitch":
            rng = random.Random()
            pic = bytearray(self.g.rom_read(self.g.sym(self.PLAYER_PICS[0][0]).addr, 0x900))
            for _ in range(len(pic) // 5):                       # corrupt ~20% of the nibbles
                i = rng.randrange(len(pic))
                pic[i] = (pic[i] & 0xF0) | rng.randrange(1, 16) if rng.random() < 0.5 else (pic[i] & 0x0F) | rng.randrange(1, 16) << 4
            pal = struct.pack("<16H", 0x7C1F, *[rng.getrandbits(15) for _ in range(15)])
            pic, label = bytes(pic), "corrupted self"
        elif src.startswith("png:"):
            path = ROOT / "sprites" / src[4:]
            pic, pal = self._png_sprite(path)
            label = f"sprite {path.name}"
        else:
            name = src[4:] if src.startswith("npc:") else src
            pic, pal = self._npc_sprite(name)
            label = f"the body of {name}"
        if hue:
            cols = list(struct.unpack("<16H", pal))
            for i in range(1, 16):
                c = cols[i]
                h, l, s = colorsys.rgb_to_hls((c & 31) / 31, ((c >> 5) & 31) / 31, ((c >> 10) & 31) / 31)
                r, g, b = colorsys.hls_to_rgb((h + hue) % 1, l, s)
                cols[i] = round(r * 31) | round(g * 31) << 5 | round(b * 31) << 10
            pal = struct.pack("<16H", *cols)
        orig = ORIGINAL_ROM.read_bytes() if src == "restore" else None
        for sym, first in self.PLAYER_PICS:
            s = self.g.syms.get(sym)
            at = s.addr + first * self.FRAME_BYTES if s else 0
            if s and at + len(pic) <= s.addr + s.size:
                # restore puts each sheet back from its own original bytes (walking != running frames)
                data = orig[at - ROM_BASE:at - ROM_BASE + len(pic)] if orig else pic
                self.g.rom_patch(at, data)
        self.g.rom_patch(self._player_pal(), pal)
        live = self._live_player_palette(old_pal, pal)
        return f"you now wear {label}" + (f" (palette live in {live} slot(s))" if live else " (colours apply on next map)")

    # ---- hacks: palettes ------------------------------------------------------------------------------------
    def recolor(self, target: str = "all", hue: float = 0.0, saturation: float = 1.0, brightness: float = 1.0,
                invert: bool = False, persistent: bool = False) -> str:
        """Live palette hack on the palette buffers (BG = colours 0-255, OBJ/sprites = 256-511)."""
        lo, hi = {"bg": (0, 256), "obj": (256, 512), "sprites": (256, 512)}.get(target, (0, 512))
        for sym in ("gPlttBufferUnfaded", "gPlttBufferFaded"):
            a = self.g.sym(sym).addr
            raw = bytearray(self.g.read(a, 1024))
            for i in range(lo, hi):
                c = struct.unpack_from("<H", raw, i * 2)[0]
                r, g, b = (c & 31) / 31, ((c >> 5) & 31) / 31, ((c >> 10) & 31) / 31
                h, l, s = colorsys.rgb_to_hls(r, g, b)
                r, g, b = colorsys.hls_to_rgb((h + hue) % 1, min(1, l * brightness), min(1, s * saturation))
                if invert:
                    r, g, b = 1 - r, 1 - g, 1 - b
                struct.pack_into("<H", raw, i * 2, (round(r * 31) | round(g * 31) << 5 | round(b * 31) << 10) & 0x7FFF)
            self.g.write(a, bytes(raw))
        self.persistent_palette = dict(target=target, hue=hue, saturation=saturation, brightness=brightness,
                                       invert=invert) if persistent else None
        return f"recolored {target}: hue {hue:+.2f} sat x{saturation} bright x{brightness}{' inverted' if invert else ''}"
