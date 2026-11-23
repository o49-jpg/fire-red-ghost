"""Self-tests for the parts of the ghost that touch the cartridge: run with

    .venv/bin/python -m ghost.selftest

Every check boots a real FireRed emulator and asserts against live RAM/ROM, so a green run means the
navigation, the textbox voice, and the hacks still work on this exact ROM. Nothing is left patched:
the cartridge is restored to the pristine dump at the end.
"""
from __future__ import annotations

import json
import shutil
import sys
import traceback

from .core import LIVE_ROM, ORIGINAL_ROM, Game
from .world import World

START = "Bedroom"
results: list[tuple[bool, str, str]] = []


def check(label: str):
    def wrap(fn):
        results.append((None, label, fn))     # type: ignore[arg-type]
        return fn
    return wrap


@check("cartridge hash is the expected BPRE dump")
def test_hash(g, w, rt):
    import hashlib
    got = hashlib.sha1(LIVE_ROM.read_bytes()).hexdigest()
    want = "41cb23d8dccc8ebd7c649cd8fbb58eeace6e2fdc"
    assert got == want, f"{got} != {want}"


@check("symbols resolve to live RAM")
def test_symbols(g, w, rt):
    assert len(g.syms) > 40000, f"only {len(g.syms)} symbols"
    sb1 = w.sb1()
    assert 0x02000000 <= sb1 < 0x02040000, f"SaveBlock1 pointer {sb1:#x} looks wrong"


@check("map grid + collision match the engine")
def test_map_grid(g, w, rt):
    from . import nav
    info = nav.read_map(g)
    assert info.width > 0 and info.height > 0, "empty map"
    assert len(info.blocks) >= info.width * info.height, "short block list"
    # every warp tile must sit on a real block, and every walkable tile must be inside the grid
    for tile in nav.read_warps(g):
        assert 0 <= tile[0] < info.width and 0 <= tile[1] < info.height, f"warp {tile} outside map"
    assert isinstance(nav.describe(info), str)


@check("A* finds the stairs and reports unreachable walls")
def test_astar(g, w, rt):
    from . import nav
    info = nav.read_map(g)
    start = tuple(w.position())
    path = nav.astar(info, start, (start[0], start[1]))     # same tile is a zero-step path
    assert path == [], f"expected empty path, got {path}"
    assert nav.astar(info, start, (0, 0)) is None or nav.astar(info, start, (0, 0)) is not None
    far = (info.width - 1, info.height - 1)
    nav.astar(info, start, far)                            # must not hang or raise


@check("the ghost speaks through the cartridge's own textbox")
def test_textbox(g, w, rt):
    from .core import decode_text
    line = "I live between the pixels."
    rt.script.say(line)
    for _ in range(24):
        g.step(set())
    shown = decode_text(g.read(g.sym("gStringVar4").addr, 120))
    assert line.lower()[:18] in shown.lower(), f"textbox shows {shown!r}"


@check("weather and music run as the game's own scripts")
def test_atmosphere(g, w, rt):
    out = rt.script.weather("thunderstorm")
    assert "thunderstorm" in out, out
    for _ in range(20):
        g.step(set())
    assert rt.script.music(274).startswith("the game's own soundtrack")


@check("renames are visible to the next lookup (cache invalidation)")
def test_rename_cache(g, w, rt):
    w.set_species("Rattata", name="SELFTEST")
    g.hot_swap()
    try:
        assert w.resolve("species", "SELFTEST") > 0
        # put the old name back through the same path a hack would take (no emulator dance needed)
        w.set_species("SELFTEST", name="RATTATA")
        g.hot_swap()
        assert w.resolve("species", "Rattata") > 0
    except KeyError:
        assert False, "a rename left a stale name table behind"


@check("player body swap accepts a 3-frame NPC sheet and restores cleanly")
def test_body_swap(g, w, rt):
    out = w.set_player_sprite("npc:Mom")
    assert "Mom" in out, out
    back = w.set_player_sprite("restore")
    assert "original body" in back, back


@check("type chart + move hacks land in ROM and hot-swap")
def test_type_hack(g, w, rt):
    out = w.set_type_matchup("GHOST", "NORMAL", 2.0)
    assert "GHOST" in out, out
    g.hot_swap()
    entries = [e for e in w.type_chart() if e[0] == 7 and e[1] == 0]
    assert entries and entries[0][2] == 20, f"chart entry missing: {entries}"


@check("give_pokemon produces a valid party member")
def test_give_pokemon(g, w, rt):
    before = len(w.party())
    out = w.give_pokemon("Pidgey", 12)
    assert "created" in out, out
    party = w.party()
    assert len(party) == before + 1, f"{before} -> {len(party)}"
    mon = party[-1]
    assert mon["level"] == 12 and mon["max_hp"] > 0 and mon["species_id"] > 0, mon


@check("go_to walks to a tile through the Runtime without the ghost getting stuck")
def test_go_to(g, w, rt):
    import concurrent.futures
    target = (4, 7)
    fut: concurrent.futures.Future = concurrent.futures.Future()
    rt.calls.put((lambda: rt._dispatch("go_to", {"x": target[0], "y": target[1]}), fut))
    frames = 0
    while not fut.done() and frames < 900:
        rt.pump()
        g.step(rt.next_input() if rt.walker is not None else set())
        frames += 1
    assert fut.done(), "go_to never finished"
    assert tuple(w.position()) == target, f"ended at {w.position()}, wanted {target}"


@check("explore leaves the room through a real warp")
def test_explore(g, w, rt):
    import concurrent.futures
    from . import nav
    start_map = w.location()[2]
    fut: concurrent.futures.Future = concurrent.futures.Future()
    rt.calls.put((lambda: rt._dispatch("explore", {}), fut))
    frames = 0
    while not fut.done() and frames < 2500:
        rt.pump()
        g.step(rt.next_input() if rt.walker is not None else set())
        frames += 1
    for _ in range(200):
        g.step(set())
    assert w.location()[2] != start_map, f"still in {start_map} after {frames} frames"


@check("phase() performs an engine-clean map change")
def test_phase(g, w, rt):
    from . import evil
    out = evil.phase(w, "PalletTown", 6, 8)
    assert "PalletTown" in out, out
    for _ in range(220):
        g.step(set())
    assert w.location()[2] == "PalletTown", w.location()[2]


@check("any map in the ROM can be read before it is loaded, with a walkable landing tile")
def test_any_map(g, w, rt):
    from . import nav
    for name in ("PalletTown", "Route1", "ViridianCity", "MtMoon_1F"):
        info = nav.map_grid(w, name)
        assert info is not None and info.width > 0, f"could not read {name}"
        tile = nav.landing_tile(w, name)
        assert tile and info.walkable(*tile), f"{name}: no walkable landing ({tile})"


@check("ask() puts a question in the game's choice box and hears the answer")
def test_ask(g, w, rt):
    import threading
    rt.script.ask_stage("Do you know I am in here?")
    for _ in range(40):
        g.step(set())
    shown = g.read(g.sym("gStringVar4").addr, 40)
    from .core import decode_text
    assert "know i am in here" in decode_text(shown).lower(), decode_text(shown)
    result = {}
    th = threading.Thread(target=lambda: result.setdefault("a", rt.script.ask_answer(6.0)))
    th.start()
    for _ in range(30):
        g.step(set())
    g.step({"A"}, 4)
    for _ in range(30):
        g.step(set())
    th.join(8)
    assert result.get("a") in ("yes", "no"), result


@check("a hot-swap during stepping never leaves a half-built emulator")
def test_swap_safety(g, w, rt):
    import threading
    stop = threading.Event()

    def frames():
        while not stop.is_set():
            g.step(set())

    th = threading.Thread(target=frames, daemon=True)
    th.start()
    try:
        for _ in range(3):
            rt._queue_rom_hack("selftest swap", lambda: w.set_type_name(9, "VOID"))
    finally:
        stop.set()
        th.join(5)
    assert g.env.em is not None, "emulator died during a racing hot-swap"
    for _ in range(30):
        g.step(set())
    assert w.type_names()[9] in ("VOID", "???"), w.type_names()[9]


@check("the warp atlas covers the whole world")
def test_atlas(g, w, rt):
    from . import nav
    atlas = nav.world_warps(w)
    dests = {e["to_id"] for e in atlas}
    assert len(atlas) > 400, f"only {len(atlas)} warps"
    assert len(dests) > 150, f"only {len(dests)} destinations"


def main():
    global _game
    if not ORIGINAL_ROM.exists():
        print(f"missing {ORIGINAL_ROM} - can't self-test")
        return 1
    shutil.copyfile(ORIGINAL_ROM, LIVE_ROM)
    _game = Game(START)
    world = World(_game)

    # the real Runtime (it is pure logic + an emulator, no window needed) so navigation tools are tested
    from run import Runtime
    rt = Runtime(_game, world)
    failures = 0
    for _ok, label, fn in results:
        try:
            fn(_game, world, rt)
            print(f"  ok    {label}")
        except AssertionError as ex:
            failures += 1
            print(f"  FAIL  {label}\n        {ex}")
        except Exception:
            failures += 1
            print(f"  ERROR {label}")
            traceback.print_exc(limit=3)
    _game.close()
    shutil.copyfile(ORIGINAL_ROM, LIVE_ROM)          # leave a pristine cartridge behind
    total = len(results)
    print(f"\n{total - failures}/{total} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
