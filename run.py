#!/usr/bin/env python3
"""FireRed Ghost: an AI that lives in the cartridge, plays, gets bored, and hacks the game.

    .venv/bin/python run.py                # live window + ghost
    .venv/bin/python run.py --no-ghost     # just play (keyboard), hacks via nothing
    .venv/bin/python run.py --mute

Keys: arrows/Z(A)/X(B)/Enter(START)/Backspace(SELECT)/A,S (L,R) let YOU play too (the ghost and you share the pad)
      P pause/resume the ghost · U undo last hack · F12 restore the original ROM · F5 save state slot · Esc quit
Env: GHOST_MODEL (default free model), GHOST_FALLBACK (cheap paid fallback), GHOST_VOICE (espeak voice).
"""
from __future__ import annotations

import argparse
import json
import queue
import struct
import threading
import time
from collections import deque
from concurrent.futures import Future

import numpy as np
import pygame

from ghost import evil, fx, perception
from ghost.agent import Agent
from ghost.core import Game, decode_text, encode_text
from ghost.world import World

SCALE = 3
PANEL_W = 420
FPS = 60
TILE_FRAMES = 16               # frames to walk one tile
KEYS = {pygame.K_UP: "UP", pygame.K_DOWN: "DOWN", pygame.K_LEFT: "LEFT", pygame.K_RIGHT: "RIGHT",
        pygame.K_z: "A", pygame.K_x: "B", pygame.K_RETURN: "START", pygame.K_BACKSPACE: "SELECT",
        pygame.K_a: "L", pygame.K_s: "R"}
TAGS = {"tool": (150, 200, 255), "thought": (255, 230, 140), "err": (255, 120, 120), "hack": (255, 110, 220),
        "info": (200, 200, 200)}


class Runtime:
    """Everything the ghost does to the game runs on the main thread, between frames."""

    def __init__(self, game: Game, world: World):
        self.g, self.w = game, world
        self.calls: queue.Queue = queue.Queue()           # (fn, future)
        self.inputs: deque = deque()                      # per-frame button sets queued by press/walk
        self.mover: dict | None = None                    # stateful collision-aware hold()
        self.input_done: list = []                        # (frames_remaining_marker, future)
        self.undo_stack: deque = deque(maxlen=12)         # (label, savestate, rom bytes)
        self.stop = threading.Event()
        self.say = lambda text: threading.Event()
        self.on_hack = lambda kind, label: None

    def stopping(self):
        return self.stop.is_set()

    # called from the agent thread
    def tool(self, name: str, args: dict, timeout: float = 30) -> str:
        fut: Future = Future()
        self.calls.put((lambda: self._dispatch(name, args), fut))
        result = fut.result(timeout=timeout)
        if isinstance(result, Future):                    # input tools: wait until their frames have played
            result = result.result(timeout=timeout)
        return result

    # main thread
    def pump(self):
        while not self.calls.empty():
            fn, fut = self.calls.get_nowait()
            try:
                fut.set_result(fn())
            except Exception as ex:
                fut.set_result(f"error: {type(ex).__name__}: {ex}")

    def next_input(self) -> set:
        m = self.mover
        if m is not None:
            m["frames"] += 1
            if m["frames"] % TILE_FRAMES == 0:            # one tile-step finished: check where we actually landed
                pos = tuple(self.w.position())
                if pos == m["last_pos"]:
                    m["collisions"] += 1
                    m["events"].append(f"blocked at {list(pos)} going {m['direction'].lower()}")
                    if m["stop_if_blocked"]:
                        self.mover = None
                        m["future"].set_result(json.dumps({"to": list(pos), "tiles": m["tiles"] - m["left"],
                                                           "collisions": m["collisions"], "events": m["events"],
                                                           "stopped": "blocked"}))
                        return set()
                else:
                    m["events"].append(f"moved to {list(pos)}")
                    m["left"] -= 1
                m["last_pos"] = pos
                if m["left"] <= 0:
                    self.mover = None
                    m["future"].set_result(json.dumps({"to": list(pos), "tiles": m["tiles"],
                                                       "collisions": m["collisions"], "events": m["events"],
                                                       "stopped": "arrived"}))
                    return {m["direction"]}
            return {m["direction"]}
        if not self.inputs:
            return set()
        buttons, fut, after = self.inputs.popleft()
        if fut is not None:
            fut.set_result(after() if after else "ok")
        return buttons

    def _queue_input(self, frames: list[set], after=None) -> Future:
        fut: Future = Future()
        for i, b in enumerate(frames):
            last = i == len(frames) - 1
            self.inputs.append((b, fut if last else None, after if last else None))
        if not frames:
            fut.set_result("ok")
        return fut

    def _checkpoint(self, label: str):
        self.undo_stack.append((label, self.g.save_state(), bytes(self.g.rom)))

    def _hack(self, label: str, fn, big=False):
        self._checkpoint(label)
        out = fn()
        self.on_hack("rom" if big else "ram", out if isinstance(out, str) else label)
        return out

    def _target(self, target: str, offset: int = 0) -> int:
        t = str(target).strip()
        base = int(t, 16) if t.lower().startswith("0x") else self.g.sym(t).addr
        return base + int(offset or 0)

    def _dispatch(self, name: str, a: dict):
        g, w = self.g, self.w
        if name == "look":
            return json.dumps(w.snapshot())
        if name == "frame":
            import base64
            f = g.screen()
            return json.dumps({"png": base64.b64encode(fx.vision_png(f)).decode("ascii"),
                               "ascii": fx.vision_ascii(f)})
        if name == "press":
            frames = max(1, min(60, int(a.get("frames", 6))))
            times = max(1, min(10, int(a.get("times", 1))))
            seq = []
            for _ in range(times):
                seq += [set(a.get("buttons", []))] * frames + [set()] * 8
            return self._queue_input(seq, after=lambda: "pressed")
        if name == "walk":
            d = a.get("direction", "up").upper()
            tiles = max(1, min(12, int(a.get("tiles", 1))))
            start = w.position()
            return self._queue_input([{d}] * (TILE_FRAMES * tiles) + [set()] * 2,
                                     after=lambda: f"from {list(start)} to {list(w.position())}"
                                                   + ("" if w.position() != start else " (blocked)"))
        if name == "wait":
            return self._queue_input([set()] * max(1, min(600, int(a.get("frames", 30)))), after=lambda: "waited")
        if name == "hold":
            d = a.get("direction", "up").upper()
            if d not in ("UP", "DOWN", "LEFT", "RIGHT"):
                return "bad direction"
            if self.mover is not None:
                return "already holding a direction"
            fut: Future = Future()
            self.mover = {"direction": d, "tiles": max(1, min(40, int(a.get("tiles", 4)))),
                          "left": max(1, min(40, int(a.get("tiles", 4)))), "frames": 0,
                          "last_pos": tuple(w.position()), "collisions": 0, "events": [],
                          "stop_if_blocked": bool(a.get("stop_if_blocked", True)), "future": fut}
            return fut
        if name == "radar":
            return json.dumps(perception.radar(w, int(a.get("radius", 9))))
        if name == "advance_text":
            times = max(1, min(30, int(a.get("times", 3))))
            seq: list[set] = []
            for _ in range(times):
                seq += [{"A"}] * 2 + [set()] * 5
            return self._queue_input(seq, after=lambda: f"pressed A {times} time(s) through the dialogue")
        if name == "delete_npc":
            return self._hack("delete npc", lambda: evil.delete_npc(w, a.get("target", "nearest")))
        if name == "npc_says":
            return self._hack("rewrite npc", lambda: evil.npc_says(w, a.get("target", "nearest"), a.get("text", "...")))
        if name == "force_battle":
            return self._hack("force battle", lambda: evil.force_battle(w, a.get("target", "nearest")))
        if name == "npc_to_pokemon":
            return self._hack("digest npc", lambda: evil.npc_to_pokemon(w, a.get("target", "nearest"),
                                                                       int(a.get("level", 0))))
        if name == "steal_pokemon":
            return self._hack("steal pokemon", lambda: evil.steal_pokemon(w, a.get("source", "enemy"),
                                                                          int(a.get("slot", 0))))
        if name == "wild_encounter":
            return self._hack("rewrite wild encounters", lambda: evil.wild_encounter(
                w, a.get("species", []), int(a.get("level", 5))))
        if name == "warp":
            return self._hack("warp", lambda: evil.warp(w, a.get("map", "PalletTown"),
                                                        int(a.get("x", 0)), int(a.get("y", 0))))
        if name == "find_symbols":
            return "\n".join(f"{s.name} @ {s.addr:#010x} size {s.size:#x}" for s in g.find_symbols(a.get("pattern", ""))) or "none"
        if name == "peek":
            addr = self._target(a["target"], a.get("offset", 0))
            n = max(1, min(256, int(a.get("length", 32))))
            raw = g.read(addr, n)
            return f"{g.symbol_at(addr)} ({addr:#010x}): {raw.hex(' ')}\ntext: {decode_text(raw)!r}"
        if name == "poke":
            addr = self._target(a["target"], a.get("offset", 0))
            if a.get("hex"):
                data = bytes.fromhex(a["hex"].replace(" ", ""))
            elif a.get("text") is not None:
                data = encode_text(a["text"])
            else:
                width = int(a.get("width", 1))
                data = int(a.get("value", 0)).to_bytes(width, "little", signed=int(a.get("value", 0)) < 0)
            return self._hack(f"poke {a['target']}", lambda: (g.write(addr, data),
                              f"wrote {len(data)} byte(s) at {g.symbol_at(addr)}")[1], big=addr >= 0x08000000)
        if name == "rom_patch":
            addr = self._target(a["target"], a.get("offset", 0))
            data = bytes.fromhex(a["hex"].replace(" ", "")) if a.get("hex") else encode_text(a.get("text", ""))
            g.rom_patch(addr, data)
            return f"queued {len(data)} byte(s) at {g.symbol_at(addr)}; call apply_rom_patches"
        if name == "apply_rom_patches":
            return self._hack("ROM hot-swap", lambda: f"hot-swapped {g.hot_swap()} patch(es) into the running game", big=True)
        if name == "set_type_name":
            return self._queue_rom_hack("type name", lambda: w.set_type_name(a["type"], a["name"]))
        if name == "set_type_matchup":
            return self._queue_rom_hack("type chart", lambda: w.set_type_matchup(a["attacker"], a["defender"], float(a["multiplier"])))
        if name == "set_move":
            kw = {k: a[k] for k in ("name", "power", "type", "accuracy", "pp", "priority", "effect") if k in a}
            return self._queue_rom_hack("move", lambda: w.set_move(a["move"], **kw))
        if name == "set_species":
            return self._queue_rom_hack("species", lambda: w.set_species(a["species"], a.get("name"), a.get("types"), a.get("stats")))
        if name == "give_pokemon":
            return self._hack("give pokemon", lambda: w.give_pokemon(a["species"], int(a.get("level", 5)), a.get("moves"), a.get("nickname")))
        if name == "recolor":
            kw = {k: a[k] for k in ("target", "hue", "saturation", "brightness", "invert", "persistent") if k in a}
            return self._hack("recolor", lambda: w.recolor(**kw))
        if name == "set_player_sprite":
            return self._queue_rom_hack("player sprite", lambda: w.set_player_sprite(a["source"], float(a.get("hue", 0) or 0)))
        if name == "undo":
            return self.undo()
        return f"unknown tool {name}"

    def _queue_rom_hack(self, label: str, fn) -> str:
        """High-level ROM hacks apply immediately (patch + hot-swap) so the ghost sees the result right away."""
        self._checkpoint(label)
        out = fn()
        n = self.g.hot_swap()
        self.on_hack("rom", out)
        return f"{out} (live, {n} patch(es) hot-swapped)"

    def undo(self) -> str:
        if not self.undo_stack:
            return "nothing to undo"
        label, state, rom = self.undo_stack.pop()
        if rom != bytes(self.g.rom):
            self.g.rom = bytearray(rom)
            self.g.rom_dirty.append((0, len(rom)))
            self.g.hot_swap()
        self.g.load_state(state)
        self.on_hack("ram", f"undid: {label}")
        return f"rolled back '{label}'"


def draw_panel(panel, agent, font, bold, log_lines, status):
    panel.fill((10, 10, 16))
    x, y = 10, 8
    panel.blit(bold.render("FIRERED GHOST" if agent else "FIRERED (no ghost)", True, (230, 230, 255)), (x, y)); y += 24
    if agent:
        m = agent.meters
        for label, val, col in (("enjoy", m.enjoyment, (120, 230, 140)), ("bored", m.boredom, (255, 140, 90))):
            panel.blit(font.render(f"{label} {val:3.0f}", True, col), (x, y))
            pygame.draw.rect(panel, (40, 40, 50), (100, y + 3, 300, 10))
            pygame.draw.rect(panel, col, (100, y + 3, int(3 * val), 10))
            y += 18
        st = "PAUSED" if not agent.enabled.is_set() else (agent.last_model or "thinking...")
        panel.blit(font.render(f"{st}  f{agent.calls['free']} p{agent.calls['paid']} hacks {m.hack_count}"
                               f" know {m.knowledge} evil {m.cruelty()}",
                               True, (150, 150, 170)), (x, y)); y += 20
    panel.blit(font.render(status, True, (200, 220, 255)), (x, y)); y += 22
    pygame.draw.line(panel, (50, 50, 70), (8, y), (PANEL_W - 8, y)); y += 6
    lines = []
    for _ts, text, tag in list(log_lines)[-40:]:
        cur = ""
        for wd in text.split():
            if len(cur) + len(wd) + 1 > 52:
                lines.append((cur, tag)); cur = "  "
            cur += wd + " "
        lines.append((cur, tag))
    for text, tag in lines[-int((panel.get_height() - y) / 16):]:
        panel.blit(font.render(text, True, TAGS.get(tag, (200, 200, 200))), (x, y)); y += 16


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="Bedroom")
    ap.add_argument("--no-ghost", action="store_true")
    ap.add_argument("--mute", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="factory-reset the cartridge (undo every past hack) and archive the notebook: a new ghost")
    ap.add_argument("--bored", action="store_true", help="wake the ghost already bored out of its mind (straight to hacking)")
    ap.add_argument("--frames", type=int, default=0, help="quit after N frames (testing)")
    args = ap.parse_args()

    if args.fresh:
        import shutil
        from ghost.core import LIVE_ROM, ORIGINAL_ROM
        from ghost.agent import NOTEBOOK
        from ghost.world import game_chart_file
        shutil.copyfile(ORIGINAL_ROM, LIVE_ROM)
        game_chart_file().unlink(missing_ok=True)
        if NOTEBOOK.exists():
            NOTEBOOK.rename(NOTEBOOK.with_name(f"notebook-{time.strftime('%Y%m%d-%H%M%S')}.md"))
        print("fresh cartridge: original ROM restored, old notebook archived")
    game = Game(args.state)
    world = World(game)
    rt = Runtime(game, world)

    pygame.init()
    rate = 32768
    try:
        rate = int(game.env.em.get_audio_rate()) or rate
    except Exception:
        pass
    audio_ok = not args.mute
    if audio_ok:
        try:
            pygame.mixer.init(frequency=rate, size=-16, channels=2, buffer=1024)
            pygame.mixer.set_num_channels(4)
            ch_game, ch_sfx, ch_voice = pygame.mixer.Channel(0), pygame.mixer.Channel(1), pygame.mixer.Channel(2)
        except pygame.error:
            audio_ok = False

    def play(ch, pcm, block=False):
        if not audio_ok:
            return
        snd = pygame.sndarray.make_sound(pcm)
        ch.play(snd)
        if block:
            time.sleep(snd.get_length())

    screen = pygame.display.set_mode((240 * SCALE + PANEL_W, 160 * SCALE))
    pygame.display.set_caption("FireRed Ghost")
    font = pygame.font.SysFont("monospace", 14)
    bold = pygame.font.SysFont("monospace", 16, bold=True)
    clock = pygame.time.Clock()
    glitch = fx.Glitch()
    log_lines: deque = deque(maxlen=60)
    caption = ["", 0.0]

    def log(text: str, tag: str = "info"):
        print(f"[{tag}] {text}", flush=True)
        log_lines.append((time.strftime("%H:%M:%S"), text, tag))

    voice = fx.Voice(rate, lambda pcm: play(ch_voice, pcm, block=True)) if audio_ok else None

    def say(text: str) -> threading.Event:
        log(f"SAY: {text}", "thought")
        caption[0], caption[1] = text, time.monotonic() + 4 + len(text) / 18
        if voice:
            return voice.say(text)
        subprocess_say(text)
        done = threading.Event()                          # no audio: pace narration by reading time
        threading.Timer(min(4.0, 0.8 + len(text) / 16), done.set).start()
        return done

    def subprocess_say(text):
        if args.mute:
            return
        import subprocess
        subprocess.Popen(["espeak-ng", "-v", "en-us+m3", "-s", "165", "-p", "28", text],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def on_hack(kind: str, label: str):
        log(f"HACK: {label}", "hack")
        if kind == "rom":
            glitch.trigger(0.95, 70)
            if audio_ok:
                play(ch_sfx, fx.sfx_hack(rate))
        else:
            glitch.trigger(0.35, 18)
            if audio_ok:
                play(ch_sfx, fx.sfx_poke(rate))

    rt.say, rt.on_hack = say, on_hack
    agent = None
    if not args.no_ghost:
        agent = Agent(rt, log)
        if args.bored:
            agent.meters.boredom, agent.meters.enjoyment = 95.0, 10.0
        agent.start()
        if audio_ok:
            play(ch_sfx, fx.sfx_boot(rate))
        glitch.trigger(0.6, 40)

    status = ""
    held: set = set()
    frame = 0
    FRAME_S = 1.0 / FPS
    next_t = time.perf_counter()
    stats = {"late": 0, "caught_up": 0, "resyncs": 0}
    panel = pygame.Surface((PANEL_W, screen.get_height()))
    panel_key = None

    def emulate_one():
        game.step(rt.next_input() | held)
        if audio_ok:
            abuf.append(game.env.em.get_audio())
    last_map = None
    reapply_at = None
    abuf: list = []
    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                running = False
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_p and agent:
                (agent.enabled.clear() if agent.enabled.is_set() else agent.enabled.set())
                log("ghost " + ("resumed" if agent.enabled.is_set() else "paused"))
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_u:
                log(rt.undo(), "hack")
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F12:
                rt._checkpoint("before ROM restore")
                game.rom_restore(); world.relocated_chart = None
                world.chart_file.unlink(missing_ok=True)
                on_hack("rom", "restored the original ROM")
            elif ev.type in (pygame.KEYDOWN, pygame.KEYUP) and ev.key in KEYS:
                (held.add if ev.type == pygame.KEYDOWN else held.discard)(KEYS[ev.key])

        rt.pump()
        emulate_one()
        frame += 1

        # persistent recolor: the game reloads palettes on map change, so re-apply after the fade-in
        loc = world.location()[:2] if frame % 10 == 0 else last_map
        if loc != last_map:
            last_map = loc
            if world.persistent_palette:
                reapply_at = frame + 45
        if reapply_at and frame >= reapply_at:
            reapply_at = None
            p = world.persistent_palette
            world.recolor(**p, persistent=True)

        if audio_ok:
            if len(abuf) >= 3:
                chunk = np.ascontiguousarray(np.concatenate(abuf).astype(np.int16)); abuf.clear()
                if ch_game.get_queue() is None:
                    snd = pygame.sndarray.make_sound(chunk)
                    (ch_game.queue(snd) if ch_game.get_busy() else ch_game.play(snd))

        img = glitch.apply(game.screen())
        surf = pygame.surfarray.make_surface(img.swapaxes(0, 1))
        screen.blit(pygame.transform.scale(surf, (240 * SCALE, 160 * SCALE)), (0, 0))
        if time.monotonic() < caption[1]:
            txt = bold.render(caption[0][:70], True, (255, 255, 255), (0, 0, 0))
            screen.blit(txt, (8, 160 * SCALE - 26))

        # panel: re-rendered only when something on it changed (font rendering every frame costs ms)
        x0 = 240 * SCALE
        if frame % 30 == 0 or frame == 1:
            try:
                s = world.snapshot_light()
                status = f"{s['map'][:30]} {s['pos']}  ${s['money']}" + ("  [BATTLE]" if s['in_battle'] else "")
            except Exception:
                status = "(reading...)"
        key = (len(log_lines), log_lines[-1] if log_lines else None, status,
               (round(agent.meters.enjoyment), round(agent.meters.boredom), agent.enabled.is_set(),
                agent.calls["free"], agent.calls["paid"], agent.last_model,
                agent.meters.knowledge, agent.meters.cruelty()) if agent else None)
        if key != panel_key:
            panel_key = key
            draw_panel(panel, agent, font, bold, log_lines, status)
        screen.blit(panel, (x0, 0))
        pygame.display.flip()
        # frame pacing: sleep, then spin the last ~1.5 ms; if late, emulate (without drawing) to catch up
        next_t += FRAME_S
        now = time.perf_counter()
        if now < next_t:
            if next_t - now > 0.002:
                time.sleep(next_t - now - 0.0015)
            while time.perf_counter() < next_t:
                pass
        else:
            behind = now - next_t
            stats["late"] += 1
            if behind > 0.25:                       # a ROM hot-swap stalls ~0.3 s: resync instead of fast-forwarding
                next_t = now
                stats["resyncs"] += 1
            else:
                for _ in range(min(4, int(behind / FRAME_S))):
                    rt.pump(); emulate_one(); frame += 1; stats["caught_up"] += 1
                    next_t += FRAME_S
        if args.frames and frame >= args.frames:
            break

    rt.stop.set()
    print(f"frames {frame}  late {stats['late']}  caught up {stats['caught_up']}  resyncs {stats['resyncs']}")
    if agent:
        print(f"model calls: free {agent.calls['free']}  paid {agent.calls['paid']}  errors {agent.calls['errors']}  last {agent.last_model}")
    game.close()
    pygame.quit()


if __name__ == "__main__":
    main()
