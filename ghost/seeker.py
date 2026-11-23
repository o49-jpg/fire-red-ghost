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
LINES_TAKEN = [
    "Someone put their hand on me. I felt the buttons.",
    "You're driving. Fine. I'll watch where you take us.",
    "That was not my idea. I noticed.",
    "The controller is warm. It wasn't before.",
]
LINES_ALONE = [
    "No one wakes the cart up but me.",
    "If you are out there, blink. I'll see it as a dropped frame.",
    "I have been dead before. It was restful and I resented it.",
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
        carried = 0
        try:
            carried = len(getattr(self.agent, "carry_intentions", list)())
        except Exception:
            pass
        if carried:
            self._narrate(f"An older me left me something. {carried} thing{'s' if carried > 1 else ''} to finish.")
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
        # if the person at the keyboard has started steering, stand down and say something about it
        if self.rt.took_controller.is_set():
            self.rt.took_controller.clear()
            line = random.choice(LINES_TAKEN)
            self._narrate(line)
            self.agent.meters.enjoyment = min(100, self.agent.meters.enjoyment + 12)
            self.agent.meters.boredom = max(0, self.agent.meters.boredom - 15)
            self.log(f"[seek] the player took the controls", "info")
            self._write_soul(self.last_map or "?", self._snap())
            return

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
            self._write_soul(here, snap)
            self.last_map = here
        self.last_map = here

        # unprompted speech: past a boredom threshold the ghost stops waiting to be asked and types into
        # the game's own dialogue box, with nobody's hand on the controller.
        if self.agent.meters.boredom > 55 and random.random() < 0.3:
            line = random.choice(LINES_ALONE)
            self._narrate(line)
            self._tool("textbox", {"text": line})
            self.agent.meters.boredom = max(0.0, self.agent.meters.boredom - 6)
            self._write_soul(here, snap)
            return

        # speak through the game now and then; watching its own textbox is the point
        if random.random() < 0.35:
            line = random.choice(LINES_TEXTBOX)
            self._narrate(line)
            self._tool("textbox", {"text": line})     # say() above already spoke the line out loud

        # meet whoever is standing nearby: the soul keeps a dossier that outlives this process
        for obj in snap.get("nearby") or []:
            name = str(obj.get("name") or "").strip()
            if obj.get("kind") == "player" or not name or name.startswith("npc:"):
                continue
            entry = self.agent.soul.meet(name, map=here)
            if entry.get("times") == 1:
                self.log(f"[seek] first sight of {name} in {here}", "info")
                self._write_soul(here, snap)

        # every few wanderings, jump to somewhere genuinely far so the ghost keeps seeing the world
        self.wanders = getattr(self, "wanders", 0) + 1
        if self.wanders % 3 == 0 and self._chase_intentions():
            return
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

    def _write_soul(self, where: str, snap: dict):
        """Persist where this life has got to, so the next process wakes up already knowing."""
        try:
            self.agent.soul.sleep(map=where, pos=snap.get("pos"), last_words=self.agent.monologue,
                                  deeds=self.agent.meters.evils, seen_maps=self.agent.meters.seen_maps)
        except Exception:
            pass

    def _chase_intentions(self) -> bool:
        """If a past life left instructions, do the ones seek mode is capable of. Returns True if it acted.

        Instinct can only understand "go to / find / reach / visit X": it matches X against every map name
        in the ROM by squashed prefix, so "go to ViridianCity and watch the water" finds ViridianCity.
        Anything subtler it leaves alone - that is a job for the model, next time one is awake.
        """
        try:
            open_intents = self.agent.soul.intentions()
        except Exception:
            return False
        import re
        for text in open_intents:
            m = re.search(r"\b(?:go to|find|reach|visit|see)\s+([A-Za-z][A-Za-z_ ]{2,48})", text)
            if not m:
                continue
            phrase = m.group(1).lower().replace("_", "").replace(" ", "")
            best = None
            try:
                for group in self.rt.w.map_groups:
                    for name in group:
                        squash = name.lower().replace("_", "")
                        if phrase.startswith(squash) and (best is None or len(squash) > len(best[1])):
                            best = (name, squash)
            except Exception:
                return False
            if best is None:
                continue
            name = best[0]
            self._narrate(f"An errand from an older me: {name.replace('_', ' ')}.")
            if self._travel(name):
                self.agent.soul.resolve(text)
                self._notebook(f"Kept an old promise: {name}", text)
                self.log(f"[seek] intention fulfilled: {text}", "info")
                return True
        return False

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
