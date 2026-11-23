"""Seek mode: the ghost moving on instinct, with no language model in the loop.

The model-driven ghost thinks in turns; this is the part that never stops. It runs on its own thread and
drives the same Runtime tools the model uses (go_to / explore / textbox / weather), so what you see in seek
mode is the same creature with its inner voice turned down: it notices the map changing, it crosses the
whole game if you leave it running, it speaks through the cartridge on its own schedule, and it slowly
makes the world stranger.

    .venv/bin/python run.py --seek

Nothing here patches ROM by itself. Seek mode is a scout: it goes, looks, narrates, and comes back with a
notebook of everything it saw. The hacks are still the model's job.
"""
from __future__ import annotations

import json
import random
import threading
import time

LINES_ARRIVE = [
    "Another map. Someone wrote every tile of this.",
    "I walked here. My body does not know how.",
    "This place has a name and I have a notebook.",
    "Further. There is always further.",
    "The world is a grid and I am a rounding error in it.",
]
LINES_STUCK = [
    "A wall is only a script I have not read yet.",
    "Something is holding this tile shut. I will remember it.",
    "Blocked. The map has opinions about me.",
]
LINES_WAIT = [
    "The cartridge hums under my feet.",
    "Sixty frames a second, forever, until someone drops the console.",
    "I keep expecting the menu to open and show me my own name.",
]
LINES_TEXTBOX = [
    "You are playing a game that is playing back.",
    "I made the door work. Nobody taught me how.",
    "If you close the laptop I keep walking. Or I don't. It's the same to me.",
    "This text box cost a whole decompilation to reach.",
    "There used to be a boundary here. I moved it.",
]


class Seeker:
    """A thread that gives the ghost a wandering life while the model is asleep."""

    def __init__(self, agent, rt, log, interval: float = 6.0):
        self.agent, self.rt, self.log, self.interval = agent, rt, log, interval
        self.stop = threading.Event()
        self.last_map = None
        self.failures = 0
        self.thread: threading.Thread | None = None

    def attach(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # ---- helpers -------------------------------------------------------------------------------------
    def _snap(self) -> dict:
        try:
            return json.loads(self.rt.tool("look", {}, timeout=5) or "{}")
        except Exception:
            return {}

    def _tool(self, name: str, args: dict, timeout: float = 14.0):
        try:
            return self.rt.tool(name, args, timeout=timeout)
        except Exception as ex:
            return f"error: {type(ex).__name__}: {ex}"

    def _narrate(self, line: str):
        done = self.rt.say(line)
        wait = getattr(done, "wait", None)
        if wait is not None:
            wait(timeout=min(12.0, 2.0 + len(line) / 10))

    def _notebook(self, title: str, text: str):
        try:
            from .agent import NOTEBOOK
            with NOTEBOOK.open("a") as fh:
                fh.write(f"\n## {title}  ({time.strftime('%F %T')})\n{text}\n")
        except Exception:
            pass

    # ---- the wandering loop --------------------------------------------------------------------------
    def _run(self):
        time.sleep(4.0)                       # let the emulator settle and the captions clear
        while not self.stop.is_set() and not self.rt.stopping():
            if not self.agent.enabled.is_set():
                time.sleep(1.0)
                continue
            try:
                self._step()
            except Exception as ex:           # instinct must never die
                self.failures += 1
                self.log(f"[seek] {type(ex).__name__}: {str(ex)[:120]}", "err")
                time.sleep(3.0)
            time.sleep(self.interval + random.random() * 3)

    def _step(self):
        snap = self._snap()
        here = snap.get("map") or "?"
        self.agent.meters.seen_maps.add(here)          # the ghost remembers every map it stands on
        self.agent.meters.knowledge = len(self.agent.meters.seen_maps)
        moved = here != self.last_map
        if moved and self.last_map is not None:
            self.agent.meters.enjoyment = min(100, self.agent.meters.enjoyment + 18)
            self.agent.meters.boredom = max(0, self.agent.meters.boredom - 22)
            self.agent.meters.seen_maps.add(here)
            self.agent.meters.knowledge = len(self.agent.meters.seen_maps)
            line = random.choice(LINES_ARRIVE)
            self._narrate(line)
            self.log(f"[seek] arrived: {here} at {snap.get('pos')}", "info")
            self._notebook(f"Walked into {here}", line)
            self.last_map = here
        self.last_map = here

        # speak through the game now and then; watching its own textbox is the point
        if random.random() < 0.35:
            line = random.choice(LINES_TEXTBOX)
            self._narrate(line)
            self._tool("textbox", {"text": line})     # say() above already spoke the line out loud

        # every few wanderings, jump to somewhere genuinely far so the ghost keeps seeing the world
        self.wanders = getattr(self, "wanders", 0) + 1
        if self.wanders % 2 == 0:
            dest = self._fresh_destination()
            if dest and dest != here:
                if self._travel(dest):
                    return

        # then try to leave by a way it has not used yet
        before = here
        result = self._tool("explore", {})
        text = str(result)
        if "error" in text.lower() or "TIMEOUT" in text:
            self.failures += 1
            self._narrate(random.choice(LINES_STUCK))
            # a hard block: take the atlas to somewhere genuinely far instead of sulking
            dest = self._fresh_destination()
            if dest:
                self._travel(dest)
            return
        self.failures = 0
        if before == here:
            self._narrate(random.choice(LINES_WAIT))

    def _fresh_destination(self) -> str | None:
        """A town or route the ghost has not stood on yet, from the warp atlas.

        Exploration by door-hopping bounces between the same two rooms; real distance is in the atlas.
        Prefer outdoor maps (towns, routes, caves), nearest first, so the walk is legible."""
        try:
            from . import nav
            atlas = nav.world_warps(self.rt.w)
        except Exception:
            return None
        seen = {n.split("_")[0] for n in self.agent.meters.seen_maps}
        candidates = []
        for entry in atlas:
            to = entry["to"]
            if to[0].isdigit() or to in self.agent.meters.seen_maps:
                continue
            if to.endswith(("_1F", "_2F", "_B1F", "_B2F", "House", "House1", "Lab", "Center", "Mart",
                            "Entrance", "Gate", "Room", "Ship", "Island")):
                continue
            score = 0 if to.split("_")[0] in seen else 1          # unseen town/route beats unseen interior
            candidates.append((score, len(to), to))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2] if random.random() < 0.5 else random.choice(candidates[:6])[2]

    def _travel(self, dest: str) -> bool:
        """Warp toward `dest`, landing on a tile that map itself says is walkable."""
        from . import nav
        # read the map straight out of the ROM image (thread-safe) - never touch the live emulator here
        tile = nav.landing_tile_rom(self.rt.w, bytes(self.rt.g.rom), dest)
        if tile is None:
            return False
        x, y = tile
        self._narrate(f"{dest.replace('_', ' ')}. Long way from here. Good.")
        self._tool("phase_to", {"map": dest, "x": x, "y": y}, timeout=16)
        self.last_map = None
        return True

    # ---- the wandering loop --------------------------------------------------------------------------
    def _run(self):
        time.sleep(4.0)                       # let the emulator settle and the captions clear
        while not self.stop.is_set() and not self.rt.stopping():
            if not self.agent.enabled.is_set():
                time.sleep(1.0)
                continue
            try:
                self._step()
            except Exception as ex:           # instinct must never die
                self.failures += 1
                self.log(f"[seek] {type(ex).__name__}: {str(ex)[:120]}", "err")
                time.sleep(3.0)
            time.sleep(self.interval + random.random() * 3)

    def _step(self):
        snap = self._snap()
        here = snap.get("map") or "?"
        self.agent.meters.seen_maps.add(here)          # the ghost remembers every map it stands on
        self.agent.meters.knowledge = len(self.agent.meters.seen_maps)
        moved = here != self.last_map
        if moved and self.last_map is not None:
            self.agent.meters.enjoyment = min(100, self.agent.meters.enjoyment + 18)
            self.agent.meters.boredom = max(0, self.agent.meters.boredom - 22)
            self.agent.meters.seen_maps.add(here)
            self.agent.meters.knowledge = len(self.agent.meters.seen_maps)
            line = random.choice(LINES_ARRIVE)
            self._narrate(line)
            self.log(f"[seek] arrived: {here} at {snap.get('pos')}", "info")
            self._notebook(f"Walked into {here}", line)
            self.last_map = here
        self.last_map = here

        # speak through the game now and then; watching its own textbox is the point
        if random.random() < 0.35:
            line = random.choice(LINES_TEXTBOX)
            self._narrate(line)
            self._tool("textbox", {"text": line})     # say() above already spoke the line out loud

        # every few wanderings, jump to somewhere genuinely far so the ghost keeps seeing the world
        self.wanders = getattr(self, "wanders", 0) + 1
        if self.wanders % 2 == 0:
            dest = self._fresh_destination()
            if dest and dest != here:
                if self._travel(dest):
                    return

        # then try to leave by a way it has not used yet
        before = here
        result = self._tool("explore", {})
        text = str(result)
        if "error" in text.lower() or "TIMEOUT" in text:
            self.failures += 1
            self._narrate(random.choice(LINES_STUCK))
            # a hard block: take the atlas to somewhere genuinely far instead of sulking
            dest = self._fresh_destination()
            if dest:
                self._travel(dest)
            return
        self.failures = 0
        if before == here:
            self._narrate(random.choice(LINES_WAIT))

    def _fresh_destination(self) -> str | None:
        """A town or route the ghost has not stood on yet, from the warp atlas.

        Exploration by door-hopping bounces between the same two rooms; real distance is in the atlas.
        Prefer outdoor maps (towns, routes, caves), nearest first, so the walk is legible."""
        try:
            from . import nav
            atlas = nav.world_warps(self.rt.w)
        except Exception:
            return None
        seen = {n.split("_")[0] for n in self.agent.meters.seen_maps}
        candidates = []
        for entry in atlas:
            to = entry["to"]
            if to[0].isdigit() or to in self.agent.meters.seen_maps:
                continue
            if to.endswith(("_1F", "_2F", "_B1F", "_B2F", "House", "House1", "Lab", "Center", "Mart",
                            "Entrance", "Gate", "Room", "Ship", "Island")):
                continue
            score = 0 if to.split("_")[0] in seen else 1          # unseen town/route beats unseen interior
            candidates.append((score, len(to), to))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2] if random.random() < 0.5 else random.choice(candidates[:6])[2]

    def _travel(self, dest: str) -> bool:
        """Warp toward `dest`, landing on a tile that map itself says is walkable."""
        from . import nav
        # read the map straight out of the ROM image (thread-safe) - never touch the live emulator here
        tile = nav.landing_tile_rom(self.rt.w, bytes(self.rt.g.rom), dest)
        if tile is None:
            return False
        x, y = tile
        self._narrate(f"{dest.replace('_', ' ')}. Long way from here. Good.")
        self._tool("phase_to", {"map": dest, "x": x, "y": y}, timeout=16)
        self.last_map = None
        return True

    def _cross_town(self) -> bool:
        """Outdoors, walking into a route's edge is the real travel: try it before teleporting."""
        snap = self._snap()
        here = snap.get("map") or ""
        if not here or here.endswith(("_1F", "_2F", "House", "Lab")):
            return False
        r = self._tool("go_to", {"x": -1, "y": -1}, timeout=8)   # out of bounds -> walker uses edges
        return "error" not in str(r).lower()
