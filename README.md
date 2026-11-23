# FireRed Ghost

A ghost that lives inside a Pokémon FireRed cartridge.

It boots the real ROM in an emulator, reads the game's memory through the names in `pret/pokefirered`'s
symbol map, walks the actual collision grid with A*, and speaks **through the game's own dialogue box**.
When it gets bored it rewrites the cartridge underneath itself: new types, new moves, new species names, the
player's body swapped for any NPC's, the weather changed from inside the room.

![the ghost](sprites/ghost_banner.png)

```bash
./setup.sh                  # python venv, deps, the decompilation, ROM checks
.venv/bin/python -m ghost.selftest   # 17 live checks against a real emulator
.venv/bin/python run.py               # window + a model-driven ghost
.venv/bin/python run.py --seek        # no LLM: watch it wander and narrate on instinct
```

`OPENROUTER_API_KEY` (env or `~/.config/opencode/secrets.env`) makes it think. Without one, `--seek` still
shows the creature moving, speaking through the textbox and changing the weather — the model only adds
reasoning, not motion.

## What makes it a creature instead of a script

**It walks.** `ghost/nav.py` reads the loaded map's 16-bit block grid (`gMapHeader.mapLayout.map`), the
per-tileset metatile attribute tables, and the map's warp events; then `astar()` finds a real path around
walls, counters, NPCs and elevation mismatches. `go_to x,y` walks it. `explore` picks the nearest exit it
has *never used* and takes it — across connections and doors — so it can leave town on its own. In testing it
went from the bedroom to Route 5's Day-Care in about 40 seconds, visiting 19 maps on the way.

The nasty part is that FireRed only fires a stair warp on the frame a step *completes while the direction is
still held* (`tookStep` → `ProcessPlayerFieldInput` → `TryArrowWarp`), and only in the direction the stairs
run (`stair_warp_ul` needs `LEFT`). The walker models exactly that: it approaches from the right side, steps
onto the tile, and holds until the map changes.

**It speaks.** `ghost/script.py` makes the cartridge run scripts the ghost writes: it fills in the game's
global script context (`sGlobalScriptContext`, `0x03000eb0`), then the overworld's own per-frame call to
`ScriptContext_RunScript()` executes bytecode written into free ROM. That gives the ghost the real dialogue
box in the game's font (`textbox`), the weather system (`weather`), and the soundtrack (`music`). No code
injection, no hot-swap — pure RAM.

**It travels.** `nav.world_warps()` walks `gMapGroups` and builds an atlas of all ~820 warps in the ROM.
`phase()` performs an engine-clean map change through `SaveBlock1->location` + `CB2_LoadMap`, and
`nav.map_grid()` / `nav.landing_tile()` read maps that have never been loaded to pick a walkable landing tile.

**It hacks.** The original machinery is intact and now safer: live type chart relocation with pointer
repair, move/species rewriting, valid party-member generation (correct substructure order, checksum and
encryption), body-swapping onto any 16×32 overworld sheet (not just 9-frame ones), and palette surgery.
`undo` rolls any of it back.

**It has an inner life.** Beyond the model's turns, `ghost/agent.py` generates an autonomic inner monologue
from the meters, and `ghost/seeker.py` gives the ghost a wandering life with no model at all: it notices new
maps, narrates arrivals, speaks through the game, and keeps moving.

## Things this version fixed

| Was | Now |
| --- | --- |
| `disasm/` missing → nothing imported at all | `setup.sh` fetches pret/pokefirered (shallow) |
| no README, setup or `.gitignore` | this file, `setup.sh`, `requirements.txt`, ignored artifacts |
| `resolve()` rebuilt 355/412-name tables per call; renames invisible afterwards | cached per name table, invalidated on rename (`invalidate_names`) |
| `give_pokemon("GHOSTRAT")` raised after renaming Rattata | renames are seen immediately |
| NPC body swap rejected any sheet that wasn't 9 frames (Mom!) | reads the sheet's own frame table and tiles it to 9 |
| metatile behaviours read with a 512-entry primary tileset | 640, per `include/fieldmap.h` |
| `radar()` cost ~500 ms per call (full symbol scan) | one linear sweep, cached per ROM epoch: ~0.2 ms |
| `read_connections()` mis-parsed the connection struct | reads `u8 dir; u32 offset; u8 group; u8 num` |
| tools waited forever on long walks | `NAV_TIMEOUT` + `nav_status`; timers no longer overwrite results |
| the ghost could only teleport to move | `go_to` / `explore` / `phase_to` with real pathfinding |
| no way to verify any of it | `python -m ghost.selftest` — 17 live checks |

## Controls (window)

Arrows move · `Z` A · `X` B · `Enter` START · `Backspace` SELECT · `A`/`S` L/R · `P` pause the ghost ·
`U` undo the last hack · `F12` restore the original ROM · `Esc` quit.

## Layout

```
ghost/core.py         emulator, symbol lookup, charmap, party decoding, ROM hot-swap
ghost/world.py        readable snapshot + hacking primitives (types, moves, species, sprites, palettes)
ghost/nav.py          map grid, collision, behaviours, warps, A*, any-map reading, the warp atlas
ghost/script.py       the game's script engine driven from RAM: textbox, weather, music
ghost/perception.py   object events + radar (who is within 9 tiles, sight lines)
ghost/agent.py        the model loop, tools, meters, inner monologue
ghost/seeker.py       instinct mode: wandering with no model
ghost/selftest.py     live tests against a real ROM
run.py                the window, the shared gamepad, the walker state machine, tool dispatch
```

ROM, decompilation and the live patched cartridge are git-ignored: bring your own legally dumped
FireRed (USA) v1.0, SHA1 `41cb23d8dccc8ebd7c649cd8fbb58eeace6e2fdc`.
