"""Perception: what the ghost can sense within 9 tiles of itself.

Reads the live gObjectEvents array (16 x 0x24 bytes) using the exact field layout from pret/pokefirered's
include/global.fieldmap.h, plus the map's object templates in SaveBlock1 for script pointers and flags.

    gObjectEvents @ 0x02036e38, 16 * 0x24:
      0x00 bit0 active | 0x01 bit5 invisible
      0x04 spriteId | 0x05 graphicsId | 0x06 movementType | 0x07 trainerType
      0x08 localId | 0x09 mapNum | 0x0A mapGroup | 0x0B elevation
      0x10 currentCoords (s16 x, s16 y) | 0x14 previousCoords
      0x18 facingDirection (low nibble) | 0x19 rangeX/rangeY
      0x1D trainerRange_berryTreeId (sight distance for trainers)

    ObjectEventTemplate @ SaveBlock1+0x8E0, 64 * 0x18:
      0x00 localId | 0x01 graphicsId | 0x04 x | 0x06 y | 0x0C trainerType
      0x10 script pointer | 0x14 flagId
"""
from __future__ import annotations

import struct

from .core import Game

OBJECT_EVENTS_ADDR, OBJECT_EVENTS_COUNT, OE_SIZE = 0x02036E38, 16, 0x24
SB1_OBJECT_TEMPLATES = 0x8E0
TEMPLATE_SIZE, TEMPLATE_COUNT = 0x18, 64
# constants/global.h:109-117 -> DIR_NONE 0, SOUTH 1, NORTH 2, WEST 3, EAST 4
FACE = {0: "none", 1: "down", 2: "up", 3: "left", 4: "right"}
TRAINER_KIND = {0: "npc", 1: "trainer", 2: "trainer_all_seeing", 3: "trainer_buried"}
RADIUS = 9


def object_events(game: Game) -> list[dict]:
    """Every active object event in the loaded map, including the player."""
    out = []
    base = OBJECT_EVENTS_ADDR
    raw_all = game.read(base, OBJECT_EVENTS_COUNT * OE_SIZE)
    for i in range(OBJECT_EVENTS_COUNT):
        raw = raw_all[i * OE_SIZE:(i + 1) * OE_SIZE]
        if not (raw[0] & 1):                    # active bit
            continue
        x, y = struct.unpack_from("<hh", raw, 0x10)
        px, py = struct.unpack_from("<hh", raw, 0x14)
        is_player = bool(raw[2] & 1)            # byte 0x02 bit0 = isPlayer
        kind = TRAINER_KIND.get(raw[7], f"trainer_{raw[7]}")
        out.append({
            "id": i, "local_id": raw[8], "map": f"{raw[10]}.{raw[9]}",
            "map_num": raw[9], "map_group": raw[10],
            "x": x, "y": y, "prev": [px, py], "facing": FACE.get(raw[0x18] & 15, "?"),
            "graphics_id": raw[5], "sprite_id": raw[4], "movement": raw[6],
            "trainer_type": raw[7], "kind": "player" if is_player else kind,
            "sight": raw[0x1D], "invisible": bool(raw[1] & 0x20),
            "elevation": raw[0x0B] & 15,
        })
    return out


def _graphics_names(game: Game) -> dict[int, str]:
    """graphicsId -> the symbol name of its GraphicsInfo (e.g. gObjectEventGraphicsInfo_RedNormal)."""
    tbl = game.sym("gObjectEventGraphicsInfoPointers")
    names = {}
    for i in range(tbl.size // 4):
        ptr = game.u32(tbl.addr + i * 4)
        if not ptr:
            continue
        sym = game.symbol_at(ptr)
        # symbol_at returns "name+offset"; keep only the base name
        name = sym.split("+")[0]
        if name.startswith("gObjectEventGraphicsInfo_"):
            names[i] = name.replace("gObjectEventGraphicsInfo_", "")
    return names


def templates(game: Game) -> list[dict]:
    """The 64 object-event templates for the loaded map (script pointers, flags, trainer type)."""
    sb1 = game.u32(game.sym("gSaveBlock1Ptr").addr) if game.syms else None
    if sb1 is None:
        return []
    base = sb1 + SB1_OBJECT_TEMPLATES
    raw_all = game.read(base, TEMPLATE_COUNT * TEMPLATE_SIZE)
    out = []
    for i in range(TEMPLATE_COUNT):
        raw = raw_all[i * TEMPLATE_SIZE:(i + 1) * TEMPLATE_SIZE]
        if not any(raw):
            continue
        out.append({"index": i, "local_id": raw[0], "graphics_id": raw[1], "kind": raw[2],
                    "x": struct.unpack_from("<h", raw, 4)[0], "y": struct.unpack_from("<h", raw, 6)[0],
                    "trainer_type": struct.unpack_from("<H", raw, 0x0C)[0],
                    "script": struct.unpack_from("<I", raw, 0x10)[0],
                    "script_addr": raw[0x10:0x14].hex(),
                    "flag": struct.unpack_from("<H", raw, 0x14)[0]})
    return out


def _offset_label(dx: int, dy: int) -> str:
    if dx == 0 and dy == 0:
        return "here"
    name = []
    if dy < 0:
        name.append("N")
    elif dy > 0:
        name.append("S")
    if dx < 0:
        name.append("W")
    elif dx > 0:
        name.append("E")
    base = "".join(name) or "?"
    knight = (abs(dx), abs(dy)) in ((1, 2), (2, 1))
    return f"{base}{' knight' if knight else ''}"


def radar(world, radius: int = RADIUS) -> dict:
    """Everything within a 9-tile square (Chebyshev) around the player, knight offsets highlighted."""
    game = world.g
    names = _graphics_names(game)
    objects = object_events(game)
    # object coords include FireRed's 7-tile map border; SaveBlock1 pos does not. Use ONE system or every
    # NPC looks 7 tiles away. The player's own object event is the source of truth for live movement.
    player = next((o for o in objects if o["kind"] == "player"), None)
    px, py = (player["x"], player["y"]) if player else world.position()
    tmpl = templates(game)
    by_local = {t["local_id"]: t for t in tmpl}

    seen = []
    for o in objects:
        dx, dy = o["x"] - px, o["y"] - py
        cheb = max(abs(dx), abs(dy))
        knight = (abs(dx), abs(dy)) in ((1, 2), (2, 1))
        if cheb > radius and not knight:
            continue
        gfx = names.get(o["graphics_id"], f"gfx{o['graphics_id']}")
        item = any(k in gfx.upper() for k in ("ITEMBALL", "BALL", "POKEBALL", "ITEM"))
        kind = o["kind"] if o["kind"] == "player" else ("item" if item else o["kind"])
        t = by_local.get(o["local_id"])
        entry = {**o, "name": gfx, "kind": kind, "dx": dx, "dy": dy, "dist": cheb,
                 "dir": _offset_label(dx, dy), "knight": knight,
                 "in_sight": bool(o["sight"] and cheb <= min(o["sight"], radius))}
        if t:
            entry["script"] = f"{t['script']:#010x}"
            entry["flag"] = t["flag"]
        seen.append(entry)
    seen.sort(key=lambda e: (e["dist"], e["kind"] != "player"))

    # a compact text grid: P=player T=trainer N=npc i=item k=knight-offset
    grid = []
    for dy in range(-radius, radius + 1):
        row = ""
        for dx in range(-radius, radius + 1):
            hits = [e for e in seen if e["dx"] == dx and e["dy"] == dy]
            if dx == 0 and dy == 0:
                row += "P"
            elif not hits:
                row += "."
            else:
                k = hits[0]["kind"]
                row += {"trainer": "T", "trainer_all_seeing": "T", "trainer_buried": "T",
                        "item": "i", "player": "P"}.get(k, "N")
        grid.append(row)

    return {"player": [px, py], "map": world.location()[2], "radius": radius,
            "objects": seen, "grid": grid,
            "counts": {k: sum(1 for e in seen if e["kind"] == k)
                       for k in ("player", "npc", "trainer", "trainer_all_seeing", "item")}}
