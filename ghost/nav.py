"""Where the ghost is, in exact tiles: the loaded map's real block grid, collision, warp points, and paths.

Everything is read from the running game, so it works on vanilla FireRed and on a cartridge the ghost
has already rewritten (warped maps, hacked tilesets, fake walls it painted itself).

    gMapHeader                      0x02036dfc   include/global.fieldmap.h:191
    gMapHeader.mapLayout            -> MapLayout {s32 width, s32 height, u16 *border, u16 *map, Tileset *primary,
                                                   Tileset *secondary, u8 borderW, u8 borderH}
        map[x + y*width]            16-bit block: bits 0-9 metatile id, bits 10-11 collision (0/1/2/3)
    Tileset.metatileAttributes      u32 per metatile: behavior = bits 0-8 (MB_* in constants/metatile_behaviors.h)
    gMapHeader.events               MapEvents {counts, ObjectEventTemplate*, WarpEvent*, CoordEvent*, BgEvent*}
    WarpEvent                       8 bytes: s16 x, s16 y, u8 elevation, u8 warpId, u8 mapNum, u8 mapGroup
    MapConnection                   12 bytes: u8 direction, u32 offset, u8 mapGroup, u8 mapNum

The block grid, warp events and SaveBlock1 pos are all map-local (0..width-1). Only *object event* coords
(the ones radar uses) carry FireRed's 7-tile border, so those are offset by MAP_OFFSET when compared.
"""
from __future__ import annotations

import heapq
import struct
from dataclasses import dataclass, field

MAP_OFFSET = 7
# include/fieldmap.h:8-9 - GetAttributeByMetatileIdAndMapLayout (src/fieldmap.c:432) indexes the primary
# tileset's attribute table for ids < 640 and the secondary's for 640..1023.
NUM_METATILES_PRIMARY, NUM_METATILES_TOTAL = 640, 1024
# GetCollisionAtCoords (src/event_object_movement.c:4830) is the authority: anything but collision 0 is
# impassable, and a tile whose elevation differs from the player's is impassable too. Tall grass, flowers
# and carpets are collision 0; counters, trees, water and NPC-blocked tiles are not.
WALKABLE_COLLISION = (0,)

# Metatile behaviors worth naming (constants/metatile_behaviors.h). Everything not listed is plain ground.
# These are read live from the loaded tilesets, so a tileset the ghost hacks changes navigation immediately.
BEHAVIOR_IDS = {
    0x02: "tall_grass", 0x08: "cave", 0x0A: "running_disallowed", 0x0B: "indoor_encounter",
    0x10: "pond_water", 0x11: "fast_water", 0x12: "deep_water", 0x13: "waterfall",
    0x16: "puddle", 0x17: "shallow_water", 0x1B: "cycling_road_water",
    0x21: "sand", 0x23: "ice", 0x26: "thin_ice", 0x27: "cracked_ice", 0x28: "hot_springs",
    0x30: "impassable_east", 0x31: "impassable_west", 0x32: "impassable_north", 0x33: "impassable_south",
    0x38: "jump_east", 0x39: "jump_west", 0x3A: "jump_north", 0x3B: "jump_south",
    0x40: "ledge_east", 0x41: "ledge_west", 0x42: "ledge_north", 0x43: "ledge_south",
    0x44: "slide_east", 0x45: "slide_west", 0x46: "slide_north", 0x47: "slide_south",
    0x50: "current_east", 0x51: "current_west", 0x52: "current_north", 0x53: "current_south",
    0x54: "spin_right", 0x55: "spin_left", 0x56: "spin_up", 0x57: "spin_down",
    0x60: "cave_door", 0x61: "ladder", 0x62: "arrow_east", 0x63: "arrow_west",
    0x64: "arrow_north", 0x65: "arrow_south", 0x66: "fall", 0x67: "regular_warp",
    0x69: "warp_door", 0x6A: "escalator", 0x6B: "escalator",
    0x6C: "stair_warp_ur", 0x6D: "stair_warp_ul", 0x6E: "stair_warp_dr", 0x6F: "stair_warp_dl",
    0x71: "union_room_warp",
}
# Behaviors you can step ON but that move you somewhere unexpected. A* treats them as normal ground; the
# runtime notices when one of them threw the ghost off its path and re-plans.
CONSEQUENCE = {"jump_east", "jump_west", "jump_north", "jump_south",
               "slide_east", "slide_west", "slide_north", "slide_south",
               "current_east", "current_west", "current_north", "current_south"}
WATER = {"pond_water", "fast_water", "deep_water", "waterfall", "cycling_road_water", "ocean_water"}
# How to fire each warp behaviour (src/field_control_avatar.c:924 IsDirectionalStairWarpMetatileBehavior):
# stair warps only trigger while you HOLD the direction the staircase runs, arrow warps likewise, and
# ordinary doors/stairs need A or a step depending on the tile.
WARP_DIRECTION = {
    "stair_warp_ur": "RIGHT", "stair_warp_dr": "RIGHT",     # *Up/Down*Right stair warp
    "stair_warp_ul": "LEFT", "stair_warp_dl": "LEFT",       # *Up/Down*Left stair warp
    "arrow_east": "RIGHT", "arrow_west": "LEFT", "arrow_north": "UP", "arrow_south": "DOWN",
    "warp_door": "UP",                                       # TryDoorWarp only fires facing north
}
WARP_BEHAVIOR = {"cave_door", "ladder", "arrow_east", "arrow_west", "arrow_north", "arrow_south",
                 "fall", "regular_warp", "warp_door", "escalator", "stair_warp_ur", "stair_warp_ul",
                 "stair_warp_dr", "stair_warp_dl", "union_room_warp"}

@dataclass
class MapInfo:
    width: int
    height: int
    blocks: list[int]                 # raw 16-bit blocks, width*height
    behaviors: dict[int, int] = field(default_factory=dict)   # metatile id -> behavior byte
    warp_tiles: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)  # player coords -> (group, num)

    def block_at(self, x: int, y: int) -> int | None:
        if 0 <= x < self.width and 0 <= y < self.height:
            return self.blocks[y * self.width + x]
        return None

    def collision(self, x: int, y: int) -> int:
        b = self.block_at(x, y)
        return 3 if b is None else (b >> 10) & 3

    def elevation(self, x: int, y: int) -> int:
        b = self.block_at(x, y)
        return 0 if b is None else (b >> 12) & 0xF

    def walkable(self, x: int, y: int, elevation: int = 0) -> bool:
        """Same test the engine runs: collision 0, elevation compatible, and not a directionally-blocked
        tile for this step."""
        if self.collision(x, y) not in WALKABLE_COLLISION:
            return False
        tile_elev = self.elevation(x, y)
        if elevation and tile_elev not in (0, 15) and tile_elev != elevation:
            return False
        return True

    def warp_direction(self, x: int, y: int) -> str | None:
        """Which way to hold to fire the warp on this tile (None if it isn't a warp tile)."""
        return WARP_DIRECTION.get(BEHAVIOR_IDS.get(self.behavior(x, y), ""))

    def step_allowed(self, x: int, y: int, direction: str, elevation: int = 0) -> bool:
        """Walkability plus FireRed's directional blocks (MB_IMPASSABLE_NORTH etc., see
        gDirectionBlockedMetatileFuncs in src/event_object_movement.c:896)."""
        if not self.walkable(x, y, elevation):
            return False
        blocked = BEHAVIOR_IDS.get(self.behavior(x, y), "")
        if blocked.startswith("impassable_"):
            return blocked != f"impassable_{direction.lower()}"
        return True

    def metatile(self, x: int, y: int) -> int:
        b = self.block_at(x, y)
        return -1 if b is None else b & 0x3FF

    def behavior(self, x: int, y: int) -> int:
        return self.behaviors.get(self.metatile(x, y), 0)

    def behavior_name(self, x: int, y: int) -> str:
        return BEHAVIOR_IDS.get(self.behavior(x, y), "")


def read_map(game) -> MapInfo:
    """The block grid + metatile behaviors of the map currently loaded."""
    import struct
    header = game.sym("gMapHeader").addr
    layout = game.u32(header)
    width, height = game.u32(layout), game.u32(layout + 4)
    map_ptr = game.u32(layout + 0x0C)
    primary, secondary = game.u32(layout + 0x10), game.u32(layout + 0x14)
    raw = game.rom_read(map_ptr, min(width * height, 1 << 20) * 2)
    blocks = list(struct.unpack(f"<{len(raw) // 2}H", raw))
    info = MapInfo(width, height, blocks)
    for tileset, base in ((primary, 0), (secondary, NUM_METATILES_PRIMARY)):
        if not tileset:
            continue
        attrs_ptr = game.u32(tileset + 0x14)
        if not attrs_ptr:
            continue
        count = NUM_METATILES_PRIMARY if tileset == primary else NUM_METATILES_TOTAL - NUM_METATILES_PRIMARY
        raw_attrs = game.rom_read(attrs_ptr, count * 4)
        for i in range(count):
            attr = struct.unpack_from("<I", raw_attrs, i * 4)[0]
            info.behaviors[base + i] = attr & 0x1FF
    return info


def player_elevation(game) -> int:
    """The elevation the player occupies right now (object event 0x0B)."""
    from .perception import object_events
    player = next((o for o in object_events(game) if o["kind"] == "player"), None)
    return player["elevation"] if player else 0


def read_warp_tiles(game) -> dict[tuple[int, int], tuple[int, int]]:
    """Player-coord -> (mapGroup, mapNum) for warp EVENTS, with the engine's elevation filter applied.

    GetWarpEventAtPosition (src/field_control_avatar.c:1008) matches a warp only when its elevation equals
    the player's OR the warp's elevation is 0. A warp drawn on a wall tile one row up is therefore the real
    door, while the floor below it is decoration - this is why "walking onto the stairs" often does nothing."""
    import struct
    header = game.sym("gMapHeader").addr
    events = game.u32(header + 0x04)
    if not events:
        return {}
    elev = player_elevation(game)
    warp_count = game.u8(events + 1)
    warps_ptr = game.u32(events + 8)
    out = {}
    for i in range(warp_count):
        x, y, w_elev, _wid, map_num, map_group = struct.unpack("<hhBBBB", game.rom_read(warps_ptr + i * 8, 8))
        if (map_group, map_num) == (0, 0):
            continue
        if w_elev != 0 and w_elev != elev:
            continue
        out[(x, y)] = (map_group, map_num)
    return out


def read_warps(game) -> dict[tuple[int, int], tuple[int, int]]:
    """Player-coord -> (mapGroup, mapNum) for every warp tile on the loaded map (no elevation filter)."""
    import struct
    header = game.sym("gMapHeader").addr
    events = game.u32(header + 0x04)
    if not events:
        return {}
    warp_count = game.u8(events + 1)
    warps_ptr = game.u32(events + 8)
    out = {}
    for i in range(warp_count):
        x, y, _elev, _wid, map_num, map_group = struct.unpack("<hhBBBB", game.rom_read(warps_ptr + i * 8, 8))
        if (map_group, map_num) != (0, 0):
            out[(x, y)] = (map_group, map_num)
    return out


def read_connections(game) -> dict[str, tuple[int, int, int]]:
    """direction -> (mapGroup, mapNum, offset) for the loaded map."""
    import struct
    header = game.sym("gMapHeader").addr
    conns = game.u32(header + 0x0C)
    if not conns:
        return {}
    count, ptr = game.u32(conns), game.u32(conns + 4)
    names = {1: "south", 2: "north", 3: "west", 4: "east"}
    out = {}
    for i in range(count):
        raw = game.rom_read(ptr + i * 12, 12)
        direction, offset = raw[0], struct.unpack_from("<I", raw, 4)[0]
        group, num = raw[8], raw[9]
        out[names.get(direction, str(direction))] = (group, num, offset)
    return out


DELTAS = {"UP": (0, -1), "DOWN": (0, 1), "LEFT": (-1, 0), "RIGHT": (1, 0)}


def astar(info: MapInfo, start: tuple[int, int], goal: tuple[int, int],
          blocked: set[tuple[int, int]] | None = None, max_nodes: int = 20000,
          elevation: int = 0) -> list[str] | None:
    """Shortest button list (UP/DOWN/LEFT/RIGHT per tile) between two player-coord tiles.

    `blocked` holds extra tiles (NPCs, the player itself) that may not be crossed. Returns None when the
    goal is unreachable - which usually means a warp or a different map is needed."""
    blocked = blocked or set()
    if start == goal:
        return []
    if not info.walkable(*goal, elevation=elevation):
        # a target on a counter/NPC tile: settle for any walkable neighbour, closest wins
        options = [(goal[0] + dx, goal[1] + dy) for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0))]
        reachable = [o for o in options if info.walkable(*o, elevation=elevation) and o not in blocked]
        if not reachable:
            return None
        goal = min(reachable, key=lambda o: abs(o[0] - start[0]) + abs(o[1] - start[1]))
        if start == goal:
            return []
    def h(p):
        return abs(p[0] - goal[0]) + abs(p[1] - goal[1])
    open_heap = [(h(start), 0, start)]
    gscore = {start: 0}
    came: dict[tuple[int, int], tuple[int, int]] = {}
    nodes = 0
    while open_heap:
        _f, cost, cur = heapq.heappop(open_heap)
        if cur == goal:
            path = []
            while cur != start:
                prev = came[cur]
                dx, dy = cur[0] - prev[0], cur[1] - prev[1]
                path.append({(0, -1): "UP", (0, 1): "DOWN", (-1, 0): "LEFT", (1, 0): "RIGHT"}[(dx, dy)])
                cur = prev
            return path[::-1]
        if cost > gscore.get(cur, 1 << 30):
            continue
        nodes += 1
        if nodes > max_nodes:
            return None
        for direction, (dx, dy) in DELTAS.items():
            nxt = (cur[0] + dx, cur[1] + dy)
            if nxt in blocked or not info.step_allowed(*nxt, direction, elevation):
                continue
            tentative = cost + 1
            if tentative < gscore.get(nxt, 1 << 30):
                gscore[nxt] = tentative
                came[nxt] = cur
                heapq.heappush(open_heap, (tentative + h(nxt), tentative, nxt))
    return None


def describe(info: MapInfo, warp_tiles: dict | None = None) -> str:
    """ASCII picture of the loaded map: '#' solid, '.' floor, 'w' water, '~' ledge/slide/current,
    'D' warp door/stairs, 'B' an existing warp entry. Block coords, row 0 at the top."""
    warp_tiles = warp_tiles or {}
    lines = []
    for y in range(info.height):
        row = []
        for x in range(info.width):
            name = info.behavior_name(x, y)
            if name in WATER:
                row.append("w")
            elif name in CONSEQUENCE:
                row.append("~")
            elif name in WARP_BEHAVIOR or (x, y) in warp_tiles:
                row.append("D")
            elif info.collision(x, y) in WALKABLE_COLLISION:
                row.append(".")
            else:
                row.append("#")
        lines.append("".join(row))
    return "\n".join(lines)


def exit_tiles(game, info: MapInfo) -> list[tuple[int, int]]:
    """Player-coord tiles that leave this map: warp tiles plus the edges that touch a connected map."""
    exits = list(read_warp_tiles(game))
    conns = read_connections(game)
    if "north" in conns:
        exits += [(x, 0) for x in range(info.width)]
    if "south" in conns:
        exits += [(x, info.height - 1) for x in range(info.width)]
    if "west" in conns:
        exits += [(0, y) for y in range(info.height)]
    if "east" in conns:
        exits += [(info.width - 1, y) for y in range(info.height)]
    return exits


def nearest_exit(info: MapInfo, start: tuple[int, int], game) -> tuple[list[str], tuple[int, int]] | None:
    """Shortest walk to any tile that leaves the map. Returns (path, exit tile) or None."""
    best = None
    for tile in exit_tiles(game, info):
        path = astar(info, start, tile)
        if path is not None and (best is None or len(path) < len(best[0])):
            best = (path, tile)
    return best


def world_warps(world) -> list[dict]:
    """Every warp in every map, read from the ROM's map headers: the ghost's travel atlas.

    gMapGroups[group][num] is a MapHeader*; MapEvents.warps describes where each one leads. Building this
    once gives exploration a list of destinations that does not depend on walking through doors."""
    g = world.g
    import struct
    out, seen = [], set()
    try:
        groups_ptr = g.sym("gMapGroups").addr
    except KeyError:
        return out
    from .core import ROM_BASE
    last = ROM_BASE + len(g.rom)
    for grp, group in enumerate(world.map_groups):
        # gMapGroups is a table of u32 pointers to per-group tables of MapHeader pointers
        group_table = g.u32(groups_ptr + grp * 4) if grp < 0x40 else 0
        if not (ROM_BASE <= group_table < last):
            continue
        for num, name in enumerate(group):
            if not name:
                continue
            try:
                header = g.u32(group_table + num * 4)
            except (ValueError, IndexError):
                continue
            if not (ROM_BASE <= header < last):
                continue
            events = g.u32(header + 0x04)
            if not (ROM_BASE <= events < last):
                continue
            warp_count = g.u8(events + 1)
            warps_ptr = g.u32(events + 8)
            if not warp_count or not (ROM_BASE <= warps_ptr < last):
                continue
            for i in range(min(warp_count, 16)):
                x, y, _elev, wid, mnum, mgrp = struct.unpack("<hhBBBB", g.rom_read(warps_ptr + i * 8, 8))
                if (mgrp, mnum) == (0, 0) or (mgrp, mnum) == (grp, num):
                    continue
                key = (grp, num, mgrp, mnum)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"from": name, "from_id": f"{grp}.{num}", "x": x, "y": y,
                            "to": world.map_groups[mgrp][mnum] if mgrp < len(world.map_groups) and mnum < len(world.map_groups[mgrp]) else f"{mgrp}.{mnum}",
                            "to_id": f"{mgrp}.{mnum}"})
    return out


# ---- reading a map that is not loaded -------------------------------------------------------------
def header_for(world, map_id) -> int | None:
    """ROM address of a MapHeader by 'group.num' or numeric (group, num) pair."""
    g = world.g
    from .core import ROM_BASE
    if isinstance(map_id, str):
        if "." not in map_id:
            hit = None
            for gi, group in enumerate(world.map_groups):
                for ni, name in enumerate(group):
                    if name.lower() == map_id.lower():
                        hit = (gi, ni)
            if hit is None:
                return None
            grp, num = hit
        else:
            grp, num = (int(v) for v in map_id.split(".")[:2])
    else:
        grp, num = map_id
    try:
        groups = g.sym("gMapGroups").addr
        table = g.u32(groups + grp * 4)
        ptr = g.u32(table + num * 4)
    except (KeyError, ValueError, IndexError):
        return None
    return ptr if ROM_BASE <= ptr < ROM_BASE + len(g.rom) else None


def map_grid(world, map_id) -> MapInfo | None:
    """The block grid + behaviours of ANY map in the ROM, loaded or not. This is how the ghost picks a
    sane place to land before it has ever been there."""
    g = world.g
    import struct
    header = header_for(world, map_id)
    if header is None:
        return None
    layout = g.u32(header)
    from .core import ROM_BASE
    if not (ROM_BASE <= layout < ROM_BASE + len(g.rom)):
        return None
    width, height = g.u32(layout), g.u32(layout + 4)
    map_ptr = g.u32(layout + 0x0C)
    if not (1 <= width <= 256 and 1 <= height <= 256 and ROM_BASE <= map_ptr < ROM_BASE + len(g.rom)):
        return None
    raw = g.rom_read(map_ptr, width * height * 2)
    blocks = list(struct.unpack(f"<{len(raw) // 2}H", raw))
    info = MapInfo(width, height, blocks)
    for tileset in (g.u32(layout + 0x10), g.u32(layout + 0x14)):
        if not (ROM_BASE <= tileset < ROM_BASE + len(g.rom)):
            continue
        attrs = g.u32(tileset + 0x14)
        if not (ROM_BASE <= attrs < ROM_BASE + len(g.rom)):
            continue
        is_secondary = struct.unpack("<B", g.rom_read(tileset + 1, 1))[0]
        base = NUM_METATILES_PRIMARY if is_secondary else 0
        count = (NUM_METATILES_TOTAL - NUM_METATILES_PRIMARY) if is_secondary else NUM_METATILES_PRIMARY
        raw_attrs = g.rom_read(attrs, count * 4)
        for i in range(count):
            info.behaviors[base + i] = struct.unpack_from("<I", raw_attrs, i * 4)[0] & 0x1FF
    return info


def landing_tile(world, map_id) -> tuple[int, int] | None:
    """A walkable tile to arrive on for a map the ghost has never loaded: a warp's own landing spot, or the
    nearest walkable tile to the map's centre."""
    info = map_grid(world, map_id)
    if info is None:
        return None
    warps = {}
    import struct
    header = header_for(world, map_id)
    events = world.g.u32(header + 0x04)
    from .core import ROM_BASE
    if events and ROM_BASE <= events < ROM_BASE + len(world.g.rom):
        count = world.g.u8(events + 1)
        ptr = world.g.u32(events + 8)
        if count and ROM_BASE <= ptr < ROM_BASE + len(world.g.rom):
            for i in range(count):
                x, y = struct.unpack("<hh", world.g.rom_read(ptr + i * 8, 4))
                if info.walkable(x, y):
                    return x, y
    cx, cy = info.width // 2, info.height // 2
    if info.walkable(cx, cy):
        return cx, cy
    best = None
    for y in range(info.height):
        for x in range(info.width):
            if info.walkable(x, y):
                d = abs(x - cx) + abs(y - cy)
                if best is None or d < best[0]:
                    best = (d, (x, y))
    return best[1] if best else None


ROM_OBJ_ADDR = 0x02036D6C      # gSaveBlock1 (static) + 0x6A0 = objectEvents[0]; only used for elevation
def map_grid_rom(world, rom: bytes, map_id) -> MapInfo | None:
    """rom is any ROM image as bytes. Used to read a map the emulator is not currently showing."""
    from .core import ROM_BASE
    g = world.g
    header = None
    if isinstance(map_id, str) and "." in map_id:
        grp, num = (int(v) for v in map_id.split(".")[:2])
    elif isinstance(map_id, str):
        found = None
        for gi, group in enumerate(world.map_groups):
            for ni, name in enumerate(group):
                if name.lower() == map_id.lower():
                    found = (gi, ni)
        if found is None:
            return None
        grp, num = found
    else:
        grp, num = map_id
    def u32(addr):
        off = addr - ROM_BASE
        return int.from_bytes(rom[off:off + 4], "little") if 0 <= off <= len(rom) - 4 else 0
    def u32rom(addr):
        return int.from_bytes(rom[addr:addr + 4], "little")
    groups = g.sym("gMapGroups").addr
    table = u32(groups + grp * 4)
    header = u32(table + num * 4)
    if not (ROM_BASE <= header < ROM_BASE + len(rom)):
        return None
    layout = u32(header)
    if not (ROM_BASE <= layout < ROM_BASE + len(rom)):
        return None
    width, height = u32(layout), u32(layout + 4)
    map_ptr = u32(layout + 0x0C)
    if not (1 <= width <= 256 and 1 <= height <= 256 and ROM_BASE <= map_ptr < ROM_BASE + len(rom)):
        return None
    raw = rom[map_ptr - ROM_BASE: map_ptr - ROM_BASE + width * height * 2]
    blocks = list(struct.unpack(f"<{len(raw) // 2}H", raw))
    info = MapInfo(width, height, blocks)
    for tileset in (u32(layout + 0x10), u32(layout + 0x14)):
        if not (ROM_BASE <= tileset < ROM_BASE + len(rom)):
            continue
        attrs = u32(tileset + 0x14)
        if not (ROM_BASE <= attrs < ROM_BASE + len(rom)):
            continue
        is_secondary = rom[tileset - ROM_BASE + 1]
        base = NUM_METATILES_PRIMARY if is_secondary else 0
        count = (NUM_METATILES_TOTAL - NUM_METATILES_PRIMARY) if is_secondary else NUM_METATILES_PRIMARY
        for i in range(count):
            info.behaviors[base + i] = int.from_bytes(rom[attrs - ROM_BASE + i*4: attrs - ROM_BASE + i*4 + 4], "little") & 0x1FF
    return info


def landing_tile_rom(world, rom: bytes, map_id) -> tuple[int, int] | None:
    info = map_grid_rom(world, rom, map_id)
    if info is None:
        return None
    header = None
    if isinstance(map_id, str):
        for gi, group in enumerate(world.map_groups):
            for ni, name in enumerate(group):
                if name.lower() == map_id.lower():
                    grp, num = gi, ni
        header = _rom_header(world, rom, grp, num)
    else:
        header = _rom_header(world, rom, *map_id)
    from .core import ROM_BASE
    if header:
        events = int.from_bytes(rom[header - ROM_BASE + 4: header - ROM_BASE + 8], "little")
        if ROM_BASE <= events < ROM_BASE + len(rom):
            count = rom[events - ROM_BASE + 1]
            ptr = int.from_bytes(rom[events - ROM_BASE + 8: events - ROM_BASE + 12], "little")
            if count and ROM_BASE <= ptr < ROM_BASE + len(rom):
                for i in range(count):
                    x = int.from_bytes(rom[ptr - ROM_BASE + i*8: ptr - ROM_BASE + i*8 + 2], "little", signed=True)
                    y = int.from_bytes(rom[ptr - ROM_BASE + i*8 + 2: ptr - ROM_BASE + i*8 + 4], "little", signed=True)
                    if info.walkable(x, y):
                        return x, y
    cx, cy = info.width // 2, info.height // 2
    if info.walkable(cx, cy):
        return cx, cy
    best = None
    for y in range(info.height):
        for x in range(info.width):
            if info.walkable(x, y):
                d = abs(x - cx) + abs(y - cy)
                if best is None or d < best[0]:
                    best = (d, (x, y))
    return best[1] if best else None


def _rom_header(world, rom: bytes, grp: int, num: int) -> int | None:
    from .core import ROM_BASE
    groups = world.g.sym("gMapGroups").addr
    def u32(addr):
        off = addr - ROM_BASE
        return int.from_bytes(rom[off:off+4], "little") if 0 <= off <= len(rom) - 4 else 0
    table = u32(groups + grp * 4)
    header = u32(table + num * 4)
    return header if ROM_BASE <= header < ROM_BASE + len(rom) else None
