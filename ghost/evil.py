"""The ghost's malicious side: rewrite the world, not just observe it.

Everything here is grounded in pret/pokefirered for FireRed USA v1.0 (BPRE):

  gObjectEvents                 0x02036e38, 16 * 0x24   include/global.fieldmap.h:212-267
  gSprites                      0x0202063c, 65 * 0x44   include/sprite.h:194-242
  gSaveBlock1Ptr->objectEventTemplates  +0x8E0, 64 * 0x18  include/global.h:788-789
      template.script @ +0x10 is what talking to an NPC actually runs
      (src/event_object_movement.c:2509-2516, 2620-2623)
  gTrainers                     0x0823eac8, 743 * 0x28   include/battle.h:116-128
  gWildMonHeaders               0x083c9cb8, 133 * 0x14   include/wild_encounter.h:13-34
  sWarpDestination              0x02031dbc (8 bytes)     src/overworld.c:97
  gMain.callback2               gMain + 0x04             include/main.h:14-15
  CB2_LoadMap                   0x0805671c               src/overworld.c:1568

No ARM code injection: script redirection + direct RAM/ROM writes are enough to make the cartridge
obey. Every effect is checkpointed by Runtime._hack so the operator can press U to undo it.
"""
from __future__ import annotations

import struct

from .core import MON_SIZE, ROM_BASE, decode_mon, encode_text
from .perception import OBJECT_EVENTS_ADDR, OE_SIZE, object_events, _graphics_names
from .world import World

SPRITES_ADDR, SPRITE_SIZE = 0x0202063C, 0x44
TEMPLATES_OFF, TEMPLATE_SIZE, TEMPLATE_COUNT = 0x8E0, 0x18, 64
TRAINERS_ADDR, TRAINER_SIZE = 0x0823EAC8, 0x28
WILD_HEADERS_ADDR, WILD_HEADER_SIZE, WILD_HEADER_COUNT = 0x083C9CB8, 0x14, 133
# GBA function pointers to Thumb code carry the low bit (CB2_Overworld reads back as ...b5, not ...b4).
WARP_DEST_ADDR, MAIN_CALLBACK2_OFF, CB2_LOAD_MAP = 0x02031DBC, 0x04, 0x0805671C | 1
SB1_LOCATION = 0x04        # include/global.fieldmap.h:392 struct WarpData {s8 group, num, warpId; s16 x, y}
WARP_DATA = "<bbb1xhh"     # sizeof 8: warpId at +2, one pad byte, then s16 x,y at +4/+6

# event script opcodes (asm/macros/event.inc)
OP_LOADWORD, OP_CALLSTD, OP_END, OP_TRAINERBATTLE = 0x0F, 0x09, 0x02, 0x5C
STD_MSGBOX_DEFAULT = 4
TRAINER_BATTLE_SINGLE = 0
# trainer_see.c:93-98 only scans types NORMAL(1) and BURIED(3); type 3 takes the all-directions branch
# in GetTrainerApproachDistance. SEE_ALL(2) is never scanned by that loop.
TRAINER_TYPE_SEE_ALL = 3


# ---- target resolution ---------------------------------------------------------------------------------------

def _labelled(world: World) -> list[dict]:
    gfx = _graphics_names(world.g)
    out = []
    for o in object_events(world.g):
        o["name"] = gfx.get(o["graphics_id"], f"gfx{o['graphics_id']}")
        out.append(o)
    return out


def resolve_target(world: World, target) -> dict | None:
    """Accept an object id, 'id:3', 'local:7', a graphics name, a kind, or 'nearest'."""
    objs = _labelled(world)
    player = next((o for o in objs if o["kind"] == "player"), None)
    people = [o for o in objs if o["kind"] != "player"]
    t = str(target).strip()
    low = t.lower()
    if low in ("nearest", "closest", "any", "first"):
        if not people or not player:
            return None
        return min(people, key=lambda o: max(abs(o["x"] - player["x"]), abs(o["y"] - player["y"])))
    if t.isdigit() or low.startswith("id:"):
        idx = int(t.split(":")[-1])
        return next((o for o in objs if o["id"] == idx), None)
    if low.startswith("local:"):
        idx = int(low.split(":")[1])
        return next((o for o in people if o["local_id"] == idx), None)
    for o in people:
        if o["name"].lower() == low or o["name"].lower().replace("_", "") == low.replace("_", ""):
            return o
    for o in people:
        if low in o["name"].lower() or low in o["kind"].lower() or low in str(o.get("script", "")).lower():
            return o
    return people[0] if len(people) == 1 else None


def _object_addr(o: dict) -> int:
    return OBJECT_EVENTS_ADDR + o["id"] * OE_SIZE


def _template_addr(world: World, o: dict) -> int | None:
    """Live SaveBlock1 map template for this object (the pointer used when you talk to it)."""
    base = world.sb1() + TEMPLATES_OFF
    raw = world.g.read(base, TEMPLATE_SIZE * TEMPLATE_COUNT)
    for i in range(TEMPLATE_COUNT):
        t = raw[i * TEMPLATE_SIZE:(i + 1) * TEMPLATE_SIZE]
        # ObjectEventTemplate has NO mapNum/mapGroup field (global.fieldmap.h:110-134); localId is unique per map.
        if t[0] == o["local_id"]:
            return base + i * TEMPLATE_SIZE
    return None


def _trainer_index(world: World, name: str) -> int:
    """Find a trainer whose name resembles the NPC, else pick a deterministic strong one."""
    g = world.g
    want = "".join(c for c in name.upper() if c.isalnum())
    best, best_score = None, 0
    for i in range(743):
        raw = g.rom_read(TRAINERS_ADDR + i * TRAINER_SIZE + 4, 12)
        s = "".join(c for c in raw.decode("ascii", "ignore") if c.isalnum())
        if want and want in s:
            return i
        score = sum(a == b for a, b in zip(want, s))
        if score > best_score:
            best, best_score = i, score
    return best if best is not None else 1


def _script_blob(world: World, text: str) -> tuple[int, bytes]:
    """msgbox <text>; end. Returns the ROM address of the script and its bytes."""
    padded = encode_text(text)
    padded += b"\xff" * ((-len(padded)) % 4)             # keep the embedded pointer 4-byte aligned
    addr = world._free_rom(len(padded) + 8)
    script = bytes([OP_LOADWORD, 0]) + struct.pack("<I", addr) + bytes([OP_CALLSTD, STD_MSGBOX_DEFAULT, OP_END])
    world.g.rom_patch(addr, padded + script)
    return addr + len(padded), padded + script


# ---- the acts ------------------------------------------------------------------------------------------------

def delete_npc(world: World, target) -> str:
    o = resolve_target(world, target)
    if not o:
        return f"no NPC matching {target!r} nearby (use radar)"
    if o["kind"] == "player":
        return "you cannot erase yourself - yet"
    g, addr = world.g, _object_addr(o)
    raw = bytearray(g.read(addr, OE_SIZE))
    raw[0] &= ~1                                        # active = FALSE
    raw[1] |= 0x20                                      # invisible
    g.write(addr, bytes(raw))
    sprite = o["sprite_id"]
    if sprite < 64:
        s = SPRITES_ADDR + sprite * SPRITE_SIZE + 0x3E
        g.write(s, bytes([g.u8(s) | 0x04]))              # sprite invisible
    return f"erased {o['name']} (object {o['id']}, local {o['local_id']}) - gone from this map"


def npc_says(world: World, target, text: str) -> str:
    o = resolve_target(world, target)
    if not o:
        return f"no NPC matching {target!r} nearby (use radar)"
    taddr = _template_addr(world, o)
    if taddr is None:
        return f"{o['name']} has no live script template on this map"
    words = str(text)[:180]
    script_addr, _ = _script_blob(world, words)
    world.g.hot_swap()
    world.g.write(taddr + 0x10, struct.pack("<I", script_addr))   # template.script
    return f"{o['name']} will now say: \"{words}\" (talk to them, or use advance_text)"


def force_battle(world: World, target) -> str:
    o = resolve_target(world, target)
    if not o:
        return f"no NPC matching {target!r} nearby (use radar)"
    g, taddr = world.g, _template_addr(world, o)
    trainer = _trainer_index(world, o["name"])
    name = o["name"].replace("_", " ").title()
    intro = f"I saw you try to run. There is no running from me. {name} challenges you!"
    defeat = f"{name} falls. Their Pokemon belong to me now."
    intro_b = encode_text(intro); intro_b += b"\xff" * ((-len(intro_b)) % 4)
    defeat_b = encode_text(defeat); defeat_b += b"\xff" * ((-len(defeat_b)) % 4)
    base = world._free_rom(len(intro_b) + len(defeat_b) + 16)
    intro_addr = base
    defeat_addr = base + len(intro_b)
    script_addr = base + len(intro_b) + len(defeat_b)
    script = (bytes([OP_TRAINERBATTLE, TRAINER_BATTLE_SINGLE]) +
              struct.pack("<HH", trainer, o["local_id"]) +
              struct.pack("<II", intro_addr, defeat_addr) + bytes([OP_END]))
    g.rom_patch(base, intro_b + defeat_b + script)
    g.hot_swap()
    if taddr is not None:
        g.write(taddr + 0x10, struct.pack("<I", script_addr))
        g.write(taddr + 0x0C, struct.pack("<H", TRAINER_TYPE_SEE_ALL))
    addr = _object_addr(o)
    g.write(addr + 0x07, bytes([TRAINER_TYPE_SEE_ALL]))            # trainerType
    g.write(addr + 0x1D, bytes([9]))                               # sight range
    # Dragging them into your face is the reliable part of "forced": sight checks also need a clear tile.
    px, py = world.position()
    ox, oy = px + 7, py + 7 - 1                                    # object coords carry MAP_OFFSET 7
    g.write(addr + 0x10, struct.pack("<hh", ox, oy))               # currentCoords
    g.write(addr + 0x14, struct.pack("<hh", ox, oy))               # previousCoords
    raw = bytearray(g.read(addr, OE_SIZE))
    raw[1] |= 0x01                                                # frozen: it cannot flee
    g.write(addr, bytes(raw))
    return (f"{name} is now an all-seeing trainer (#{trainer}) with 9-tile sight. "
            "Step into view: the battle is forced.")


def npc_to_pokemon(world: World, target, level: int = 5) -> str:
    o = resolve_target(world, target)
    if not o:
        return f"no NPC matching {target!r} nearby (use radar)"
    name = o["name"].replace("_", " ").title()
    enemies = world.party(enemy=True) if world.in_battle() else []
    lead = enemies[0] if enemies else None
    sp = lead["species_id"] if lead else (1 + (sum(map(ord, name)) % 411))
    lv = int(level) if level else (lead["level"] if lead else 5)
    made = world.give_pokemon(sp, max(1, min(100, lv)), nickname=name[:10])
    gone = delete_npc(world, target)
    return f"digested {name} into a Pokemon. {made}; {gone}"


def steal_pokemon(world: World, source: str = "enemy", slot: int = 0) -> str:
    g = world.g
    count_addr = g.sym("gPlayerPartyCount").addr
    count = g.u8(count_addr)
    if count >= 6:
        return "your party is full - release something first"
    src = g.sym("gEnemyParty").addr + max(0, min(5, int(slot))) * MON_SIZE
    raw = bytearray(g.read(src, MON_SIZE))
    mon = decode_mon(bytes(raw))
    if not mon:
        return f"no Pokemon in enemy slot {slot} (start a battle first)"
    raw[80] = 0                                                    # clear status
    if mon["hp"] == 0 and mon["max_hp"] > 0:
        struct.pack_into("<H", raw, 86, mon["max_hp"])             # revive what you stole
    dst = g.sym("gPlayerParty").addr + count * MON_SIZE
    g.write(dst, bytes(raw))
    g.write(count_addr, bytes([count + 1]))
    name = world.species_name(mon["species_id"])
    return f"stole {name} L{mon['level']} from {source}"


def wild_encounter(world: World, species, level: int = 5) -> str:
    g = world.g
    grp, num, name = world.location()
    names = species if isinstance(species, (list, tuple)) else [species]
    ids = [world.resolve("species", n) for n in names][:12]
    if not ids:
        return "give me at least one species"
    lv = max(1, min(100, int(level)))
    entries = b"".join(struct.pack("<BBH", lv, lv, ids[i % len(ids)]) for i in range(12))
    header = None
    for h in range(WILD_HEADERS_ADDR, WILD_HEADERS_ADDR + WILD_HEADER_COUNT * WILD_HEADER_SIZE, WILD_HEADER_SIZE):
        if g.rom_read(h, 2) == bytes([grp, num]):
            header = h
            break
    if header is None:
        return f"no wild-encounter table for {name} ({grp}.{num})"
    old_info = struct.unpack("<I", g.rom_read(header + 4, 4))[0]
    rate = g.u8(old_info) if old_info else 21
    base = world._free_rom(len(entries) + 8)
    g.rom_patch(base, entries)
    g.rom_patch(base + len(entries), struct.pack("<B3xI", rate, base))
    g.rom_patch(header + 4, struct.pack("<I", base + len(entries)))
    g.hot_swap()
    return (f"wild {name} are now {[world.species_name(i) for i in ids]} "
            f"at L{lv} (rate {rate}); table repointed to {base:#x}")


def resolve_map(world: World, map_ref) -> tuple[int, int] | None:
    want = str(map_ref).strip().lower().replace(" ", "_")
    if "." in want and all(p.isdigit() for p in want.split(".")[:2]):
        grp, num = (int(v) for v in want.split(".", 1)[:2])
        return grp, num
    alias = {"mom": "PalletTown_PlayersHouse_1F", "home": "PalletTown_PlayersHouse_1F",
             "bedroom": "PalletTown_PlayersHouse_2F", "lab": "PalletTown_ProfessorOaksLab",
             "oak": "PalletTown_ProfessorOaksLab", "pallet": "PalletTown", "viridian": "ViridianCity",
             "pewter": "PewterCity", "cerulean": "CeruleanCity", "vermilion": "VermilionCity",
             "lavender": "LavenderTown", "celadon": "CeladonCity", "fuchsia": "FuchsiaCity",
             "saffron": "SaffronCity", "cinnabar": "CinnabarIsland", "indigo": "IndigoPlateau"}
    want = alias.get(want, want)
    for gi, group in enumerate(world.map_groups):
        for ni, name in enumerate(group):
            low = name.lower()
            if low == want or low.endswith("_" + want) or low.replace("_", "") == want.replace("_", ""):
                return gi, ni
    return None


def phase(world: World, map_ref, x: int = -1, y: int = -1) -> str:
    """Engine-clean map change: stage the destination in SaveBlock1->location and ask the overworld to
    load a fresh map (CB2_LoadMap). Same path a real warp takes, so music, tilesets and object events all
    arrive correctly - no half-loaded world, no glitched state.

    CB2_LoadMap -> LoadMapFromWarp never calls SetPlayerCoordsFromWarp (only WarpIntoMap does, for
    Teleport/Escape Rope), so the player keeps his old pos: write pos by hand, on a walkable tile."""
    hit = resolve_map(world, map_ref)
    if hit is None:
        return f"no map named {map_ref!r}"
    grp, num = hit
    if x < 0 or y < 0:                      # no coords: read the map out of the ROM and land somewhere legal
        from . import nav
        landing = nav.landing_tile_rom(world, bytes(world.g.rom), (grp, num))
        x, y = landing if landing else (0, 0)
    x, y = int(x), int(y)
    data = struct.pack(WARP_DATA, grp, num, -1, x, y)
    world.g.write(world.sb1() + SB1_LOCATION, data)
    world.g.write(world.sb1() + 0x00, struct.pack("<hh", x, y))   # struct Coords16 pos @ SaveBlock1+0
    world.g.write(WARP_DEST_ADDR, data)
    world.g.write(world.g.sym("gMain").addr + MAIN_CALLBACK2_OFF, struct.pack("<I", CB2_LOAD_MAP))
    return f"warping to {world.map_groups[grp][num]}" + (f" at ({x},{y})" if x >= 0 and y >= 0 else "")


def nearby_maps(world: World, map_ref) -> list[str]:
    """Same-map-name siblings in other groups - a cheap way to answer 'where can I go that's like here'."""
    hit = resolve_map(world, map_ref)
    if hit is None:
        return []
    name = world.map_groups[hit[0]][hit[1]].lower()
    out = []
    for gi, group in enumerate(world.map_groups):
        for ni, cand in enumerate(group):
            if cand.lower() != name and (name in cand.lower() or cand.lower() in name):
                out.append(cand)
    return out[:12]


def warp(world: World, map_ref, x: int = 0, y: int = 0) -> str:
    g = world.g
    want = str(map_ref).strip().lower()
    grp = num = None
    if "." in want and want.replace(".", "").isdigit():
        grp, num = (int(v) for v in want.split(".", 1))
    else:
        for gi, group in enumerate(world.map_groups):
            for ni, n in enumerate(group):
                if n.lower() == want or n.lower().endswith("." + want):
                    grp, num = gi, ni
                    break
            if grp is not None:
                break
    if grp is None:
        return f"no map named {map_ref!r}"
    px, py = max(0, min(32767, int(x))), max(0, min(32767, int(y)))
    data = struct.pack(WARP_DATA, grp, num, -1, px, py)
    sb1 = world.sb1()
    g.write(WARP_DEST_ADDR, data)              # what the script path stages
    g.write(sb1 + SB1_LOCATION, data)          # ApplyCurrentWarp: what CB2_LoadMap actually reads
    g.write(g.sym("gMain").addr + MAIN_CALLBACK2_OFF, struct.pack("<I", CB2_LOAD_MAP))
    return f"phasing to {world.map_groups[grp][num]} at ({px},{py})"
