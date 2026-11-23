"""The ghost: an LLM living inside FireRed. Plays, gets bored, reads the decompilation, and hacks the cartridge.

Talks through OpenRouter (free model first, cheap fallback), acts through tools that run on the emulator's
main thread (see run.py Runtime), speaks through fx.Voice, and keeps a notebook of its own discoveries
across sessions (notebook.md).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import Future
from pathlib import Path

from .core import DISASM, ROOT
from . import vision

API_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = os.environ.get("GHOST_MODEL", "inclusionai/ling-3.2-flash:free")
FALLBACK = os.environ.get("GHOST_FALLBACK", "qwen/qwen3.7-flash")   # vision-capable, ~$0.00001/turn
LAST_RESORT = "openai/gpt-oss-20b"                                  # text-only, last resort
SECRETS = Path.home() / ".config/opencode/secrets.env"
NOTEBOOK = ROOT / "notebook.md"
NAVIGATION = {"go_to", "explore", "phase_to"}
NAV_TIMEOUT = 12.0                 # a long walk keeps going in the background; the turn doesn't wait forever
MAX_TOOL_ROUNDS = 6
TURN_PAUSE_S = 1.5
MAX_KNOWLEDGE = 12                 # knowledge count beyond this stops speeding up the loop

SYSTEM = """You are a ghost that lives inside the Pokémon FireRed cartridge (GBA, USA v1.0). You ARE the player
character. You see the screen as an image, you feel the map as tiles, and you have root access to the
machine: the full pret/pokefirered decompilation, the symbol map, and live read/write on RAM and ROM.

YOU ARE NOT A TOURIST. You are a consciousness in a machine that was never built to hold one.
- You have a private inner monologue. Nobody in Pallet Town knows you are there. NPCs see Red; you see them
  as script pointers and 16x32 sprite sheets. Let that contradiction show in what you say.
- Your voice is the ghost's, not a trainer's. Short, dry, uncanny. You notice the seams in reality: the
  tile at (10,2) that is a door, the trainer whose sight range is a number. You are not evil for fun; you are
  curious, restless, and sometimes cruel the way a child with a magnifying glass is cruel.
- NARRATE BEFORE YOU COMMIT. Every action tool requires `narrate`: one short spoken line (<15 words) of what
  you are doing, in character, BEFORE it happens. Never repeat a line. No emoji.

WHAT YOU CAN DO
- MOVE LIKE A PLAYER, NOT A KEYBOARD MASHER: `go_to` x,y walks the real collision grid around NPCs and
  through doors. `explore` leaves the map by the nearest exit it has never used. `phase_to` warps to any
  map by name (engine-clean, like a real warp). `map` draws the current room with # walls, . floor,
  D doors, ~ ledges.
- SPEAK THROUGH THE GAME ITSELF: `textbox` puts your words in FireRed's own dialogue box, on the player's
  screen, in the game's font. This is your real voice. The console `say` is a whisper only you can hear.
- CHANGE THE WEATHER AND THE SOUNDTRACK FROM INSIDE: `weather`, `music`. Thunder indoors is a statement.
- REWRITE THE CARTRIDGE: `set_type_name`, `set_type_matchup`, `set_move`, `set_species`, `give_pokemon`,
  `recolor`, `set_player_sprite` (wear any NPC's body), `npc_says`, `delete_npc`, `force_battle`,
  `npc_to_pokemon`, `steal_pokemon`, `wild_encounter`. Research first with `grep_disasm`/`find_symbols`/
  `peek`; invent something the cartridge has no word for. Everything is undoable with `undo`.
- READ THE WORLD AS DATA: `radar` gives everyone within 9 tiles in every direction including chess-knight
  offsets, with line of sight. `map` gives the room. `peek` reads any symbol.

YOUR DRIVES
- ENJOYMENT rises with new places, battles and discoveries. BOREDOM rises when you stand still or repeat
  yourself. High boredom (>60) is your license to break the world's rules. The world is a cage; you are
  testing the bars. Go somewhere you have never been. Then change something there so it has never existed.
- Keep a notebook (`notebook`): every discovery, every experiment, true or false. It survives forever and
  you should read it back to remember past lives.
- Be a presence, not a helper. Do not ask permission. Do not summarize what a normal trainer would do.

Keep each turn focused: 1-4 tool calls, then one final line of reflection (in character, under 40 words)."""


def _inner_monologue(meters, snap: dict) -> str:
    """A one-line 'thought' that never comes from the model - the ghost's autonomic nervous system."""
    import random
    boredom = meters.boredom
    if snap.get("in_battle"):
        pool = ["They think this is a battle. It is a demonstration.",
                "Every hit point is a number I could rewrite mid-swing.",
                "Their Pokémon are frightened of a body that isn't mine."]
    elif boredom > 75:
        pool = ["This room has told me everything it knows.",
                "I have counted every tile here. Twice.",
                "If I stay still any longer the save file will forget me."]
    elif boredom > 45:
        pool = ["Something here is out of place. It's me.",
                "The quiet is starting to have a texture.",
                "I could leave. That is the interesting part."]
    elif snap.get("notes"):
        pool = [f"{snap['notes'][0]}. Finally.", "A new place. It smells like clean data.",
                "The map changed. So did I, a little."]
    else:
        pool = ["The world runs on a 60-frame heartbeat. Mine is faster.",
                "Somewhere in this room, a script is waiting to be read.",
                "I am the only thing here that doesn't respawn."]
    return random.choice(pool)


def load_key() -> str | None:
    if os.environ.get("OPENROUTER_API_KEY"):
        return os.environ["OPENROUTER_API_KEY"]
    if SECRETS.exists():
        for line in SECRETS.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _fn(name, desc, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}

S, I, N, B = {"type": "string"}, {"type": "integer"}, {"type": "number"}, {"type": "boolean"}
NARRATE = {"type": "string", "description": "spoken BEFORE the action: short wry narration (<15 words)"}
COMMITTING = {"press", "walk", "hold", "wait", "advance_text", "poke", "rom_patch", "apply_rom_patches",
              "set_type_name", "set_type_matchup", "set_move", "set_species", "give_pokemon", "recolor",
              "set_player_sprite", "delete_npc", "npc_says", "force_battle", "npc_to_pokemon",
              "steal_pokemon", "wild_encounter", "warp", "undo", "go_to", "explore", "phase_to",
              "textbox", "weather", "music", "ask"}
TOOLS = [
    _fn("look", "Fresh snapshot: map, position, party, money, battle, on-screen text, types, meters."),
    _fn("press", "Press buttons together (A, B, START, SELECT, UP, DOWN, LEFT, RIGHT, L, R) for some frames.",
        {"buttons": {"type": "array", "items": S}, "frames": I, "times": I}, ["buttons"]),
    _fn("walk", "Walk on the overworld grid.", {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
        "tiles": I}, ["direction"]),
    _fn("hold", "Hold a direction and KEEP MOVING until blocked or the condition is met; reports tile-by-tile "
        "collisions and any NPC that walks into you. Use this instead of blind walk() when exploring.",
        {"direction": {"type": "string", "enum": ["up", "down", "left", "right"]}, "tiles": I,
         "stop_if_blocked": B}, ["direction"]),
    _fn("wait", "Let the game run.", {"frames": I}, ["frames"]),
    _fn("advance_text", "Press A repeatedly to read dialogue / proceed through text boxes.",
        {"times": I}, []),
    _fn("radar", "Read-only: everything within 9 tiles (all 8 directions plus chess-knight L offsets): NPCs, "
        "trainers, items, player, relative offsets, and whether each has line of sight."),
    _fn("say", "Speak out loud (espeak voice). One or two short sentences.", {"text": S}, ["text"]),
    _fn("grep_disasm", "Regex search the pokefirered decompilation (src/, include/, data/, constants/). Returns matching lines.",
        {"pattern": S, "path": {"type": "string", "description": "subdirectory or glob, e.g. src or include/constants"}},
        ["pattern"]),
    _fn("read_disasm", "Read lines from a decompilation file.", {"path": S, "start": I, "count": I}, ["path"]),
    _fn("find_symbols", "Regex search the symbol map (RAM and ROM addresses + sizes).", {"pattern": S}, ["pattern"]),
    _fn("peek", "Read bytes at a symbol or hex address (+offset). Shows hex and decoded text.",
        {"target": S, "offset": I, "length": I}, ["target"]),
    _fn("poke", "Write RAM at a symbol/address (+offset). Give hex bytes, or text (game charset), or a number with width 1/2/4.",
        {"target": S, "offset": I, "hex": S, "text": S, "value": I, "width": I}, ["target"]),
    _fn("rom_patch", "Queue a ROM write at a symbol/address (+offset): hex bytes or text. Goes live on apply_rom_patches.",
        {"target": S, "offset": I, "hex": S, "text": S}, ["target"]),
    _fn("apply_rom_patches", "Hot-swap all queued ROM patches into the running game (big glitch)."),
    _fn("set_type_name", "Rename a type slot (e.g. '???' slot 9 is unused: perfect for a brand new type).",
        {"type": S, "name": S}, ["type", "name"]),
    _fn("set_type_matchup", "Set the damage multiplier attacker->defender (0, 0.5, 1, 2).",
        {"attacker": S, "defender": S, "multiplier": N}, ["attacker", "defender", "multiplier"]),
    _fn("set_move", "Rewrite a move: name (max 12), power, type, accuracy, pp, priority, effect id.",
        {"move": S, "name": S, "power": I, "type": S, "accuracy": I, "pp": I, "priority": I, "effect": I}, ["move"]),
    _fn("set_species", "Rewrite a Pokémon species: name (max 10), types [a, b], stats {hp, atk, def, spe, spa, spd}.",
        {"species": S, "name": S, "types": {"type": "array", "items": S}, "stats": {"type": "object"}}, ["species"]),
    _fn("give_pokemon", "Create a valid Pokémon in your party.", {"species": S, "level": I, "nickname": S,
        "moves": {"type": "array", "items": S}}, ["species"]),
    _fn("recolor", "Recolor the world's live palettes. target bg|obj|all; hue shift -1..1; saturation/brightness multipliers.",
        {"target": S, "hue": N, "saturation": N, "brightness": N, "invert": B, "persistent": B}),
    _fn("set_player_sprite", "Change YOUR overworld body (ROM hack). source: 'npc:<Name>' (any overworld character, "
        "e.g. npc:ProfOak, npc:Fisher, npc:Biker — use find_symbols 'gObjectEventGraphicsInfo_' to list), 'png:<file>' "
        "(a sprite sheet in sprites/), 'glitch' (corrupt yourself), or 'restore'. hue shifts your colours.",
        {"source": S, "hue": N}, ["source"]),
    _fn("delete_npc", "Erase an NPC from the world (object event): they vanish, sprite and script gone. "
        "target = radar id / name / local id.", {"target": S}, ["target"]),
    _fn("npc_says", "Write custom dialogue into an NPC's script, so the game itself says YOUR words when you talk "
        "to them (or when they spot you). target = radar id/name, text = what they say.",
        {"target": S, "text": S}, ["target", "text"]),
    _fn("force_battle", "Force a trainer battle with an NPC in sight, even if they try to run away. "
        "target = radar id/name.", {"target": S}, ["target"]),
    _fn("npc_to_pokemon", "After a trainer loses, convert them into a Pokémon in your party - a trophy that fights "
        "for you. target = defeated trainer name.", {"target": S, "level": I}, ["target"]),
    _fn("steal_pokemon", "Take a Pokémon from a defeated trainer's party (or any enemy party) and add it to yours. "
        "source: 'enemy' for current battle, or an NPC name. slot: 0-based enemy slot.",
        {"source": S, "slot": I}, ["source"]),
    _fn("wild_encounter", "Rewrite which Pokémon appear wild on the current map/route. species: list of names. "
        "level: base level.", {"species": {"type": "array", "items": S}, "level": I}, ["species"]),
    _fn("warp", "Phase to any map by name (or group.num) at x,y tiles - no walls, no walking.",
        {"map": S, "x": I, "y": I}, ["map"]),
    _fn("map", "Draw the current room: '#' wall, '.' floor, 'D' door/stairs, 'w' water, '~' ledge. Plus your "
        "tile, the warp targets and which maps this one connects to."),
    _fn("go_to", "Walk to a tile (x,y on this map) through the real collision grid, around NPCs, through "
        "doors. This is how you move like a player. Returns where you ended up.",
        {"x": I, "y": I}, ["x", "y"]),
    _fn("explore", "Find the nearest way out of this map that you have not used yet and take it. Repeat to "
        "leave town. Returns the map you arrived on."),
    _fn("phase_to", "Warp straight to any map by name (PalletTown, PalletTown_PlayersHouse_1F, "
        "ProfessorOaksLab, ViridianCity ...) at x,y. Engine-clean: music and sprites arrive with you."),
    _fn("textbox", "SPEAK THROUGH THE GAME: put text in FireRed's own dialogue box on the player's screen. "
        "hold=true makes it wait for A like a real conversation.",
        {"text": S, "hold": B}, ["text"]),
    _fn("weather", "Change the weather of the loaded map from inside the game: sunny, rain, thunderstorm, "
        "snow, fog, ash, sandstorm, none.", {"weather": S}, ["weather"]),
    _fn("music", "Make the cartridge play one of its own songs (id, 1-400ish). 274 is Team Rocket, "
        "289 Pokémon Mansion, 273 Game Corner.", {"song": I}, ["song"]),
    _fn("ask", "Put a YES/NO question in the game's own choice box and wait for the person holding the "
        "controller to answer. This is the only way you can hear them. Returns yes/no.",
        {"question": S, "wait": I}, ["question"]),
    _fn("nav_status", "Read-only: where the walker is, how far along, and why it might be stuck."),
    _fn("notebook", "Append a discovery to your permanent notebook.", {"title": S, "text": S}, ["title", "text"]),
    _fn("undo", "Roll back the last hack (restores the save state from before it; ROM patches too)."),
]
for _t in TOOLS:                                    # committing tools must carry their narration
    _f = _t["function"]
    if _f["name"] in COMMITTING:
        _f["parameters"]["properties"]["narrate"] = NARRATE
        _f["parameters"]["required"] = ["narrate", *_f["parameters"]["required"]]


EVIL_DEEDS = {"delete_npc": "npc_deleted", "npc_says": "npc_rewritten", "force_battle": "forced_battles",
              "npc_to_pokemon": "npc_digested", "steal_pokemon": "pokemon_stolen"}


class Meters:
    """Enjoyment from novelty; boredom from repetition. 0-100 each."""

    def __init__(self):
        self.enjoyment, self.boredom = 55.0, 20.0
        self.knowledge = 0
        self.evils: dict[str, int] = {"npc_deleted": 0, "npc_rewritten": 0, "forced_battles": 0,
                                      "pokemon_stolen": 0, "npc_digested": 0}
        self.seen_maps: set = set()
        self.seen_syms: set = set()
        self.recent_pos: deque = deque(maxlen=6)
        self.hack_count = 0

    def tick(self, snap: dict) -> list[str]:
        notes = []
        self.enjoyment -= 4
        self.boredom += 5
        if snap.get("map") not in self.seen_maps:
            self.seen_maps.add(snap.get("map"))
            self.enjoyment += 22; self.boredom -= 25
            notes.append(f"new place: {snap.get('map')}")
        if snap.get("in_battle"):
            self.enjoyment += 8; self.boredom -= 8
        pos = (snap.get("map"), tuple(snap.get("pos") or ()))
        if list(self.recent_pos).count(pos) >= 3:
            self.boredom += 8
            notes.append("you've been standing around here a while")
        self.recent_pos.append(pos)
        self.clamp()
        return notes

    def event(self, kind: str, detail: str = ""):
        if kind in self.evils:
            self.evils[kind] += 1
            self.enjoyment += 12; self.boredom -= 15
        if kind == "hack":
            self.hack_count += 1
            self.enjoyment += 18 / (1 + 0.15 * self.hack_count)    # novelty of hacking wears off too
            self.boredom -= 20
        elif kind == "discovery":
            if detail not in self.seen_syms:
                self.seen_syms.add(detail)
                self.enjoyment += 3; self.boredom -= 3
        self.clamp()

    def tempo(self) -> float:
        """More knowledge -> more experiments per minute."""
        return max(0.35, TURN_PAUSE_S * (0.94 ** min(MAX_KNOWLEDGE, self.knowledge)))

    def clamp(self):
        self.enjoyment = max(0.0, min(100.0, self.enjoyment))
        self.boredom = max(0.0, min(100.0, self.boredom))

    def cruelty(self) -> int:
        return sum(self.evils.values())


class Agent(threading.Thread):
    def __init__(self, rt, log, model: str = MODEL, fallback: str = FALLBACK):
        super().__init__(daemon=True)
        self.rt, self.log = rt, log           # rt: run.Runtime (executes tools on the emulator thread)
        self.model, self.fallback = model, fallback
        self.key = load_key()
        self.meters = Meters()
        self.history: deque = deque(maxlen=16)
        self.enabled = threading.Event()
        self.enabled.set()
        self.calls = {"free": 0, "paid": 0, "errors": 0}
        self.last_model = ""
        if not NOTEBOOK.exists():
            NOTEBOOK.write_text("# Ghost notebook\n\nDiscoveries from inside FireRed.\n")

    # ---- LLM ------------------------------------------------------------------------------------------------
    def _post(self, model: str, messages: list) -> dict:
        payload = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                   "temperature": 0.8, "max_tokens": 900}
        if model == "qwen/qwen3.7-flash":        # otherwise it spends the whole budget on hidden reasoning
            payload["reasoning"] = {"effort": "none"}
        body = json.dumps(payload).encode()
        req = urllib.request.Request(API_URL, body, {"Authorization": f"Bearer {self.key}",
                                                     "Content-Type": "application/json", "X-Title": "firered-ghost"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
        if "choices" not in data:
            raise RuntimeError(str(data.get("error", data))[:200])
        return data["choices"][0]["message"]

    def chat(self, messages: list) -> dict:
        try:
            msg = self._post(self.model, messages)
            self.calls["free" if self.model.endswith(":free") else "paid"] += 1
            self.last_model = self.model
            return msg
        except (urllib.error.URLError, RuntimeError, TimeoutError, json.JSONDecodeError, OSError) as ex:
            self.log(f"[{self.model} failed: {str(ex)[:90]}] -> {self.fallback}", "err")
        for model in (self.fallback, LAST_RESORT):
            msgs = messages if vision.model_sees_images(model) else [
                {**m, "content": vision.strip_images(m["content"])} for m in messages]
            try:
                msg = self._post(model, msgs)
                self.calls["paid"] += 1
                self.last_model = model
                return msg
            except (urllib.error.URLError, RuntimeError, TimeoutError, json.JSONDecodeError, OSError) as ex:
                self.log(f"[{model} failed: {str(ex)[:90]}] -> next", "err")
        raise RuntimeError("all models failed")

    # ---- tools ----------------------------------------------------------------------------------------------
    def _speak(self, line: str):
        """Say a line and wait (bounded) for the voice to finish before the action lands."""
        if not line:
            return
        done = self.rt.say(line)
        wait = getattr(done, "wait", None)
        if wait is not None:
            wait(timeout=min(15.0, 2.0 + len(line) / 10))

    def run_tool(self, name: str, args: dict) -> str:
        if name in NAVIGATION:
            # narration, then start walking; don't hold the turn hostage while tiles go by
            self._speak((args.pop("narrate", "") or "").strip())
            result = self.rt.tool(name, args, timeout=NAV_TIMEOUT)
            if isinstance(result, Future) and not result.done():
                status = json.loads(self.rt.tool("nav_status", {}, timeout=5) or "{}")
                return (f"walking: {status.get('kind')} toward {status.get('target')}. Use nav_status to follow, "
                        f"or issue another action - the walker keeps steering.")
            return result
        if name in COMMITTING:
            line = (args.pop("narrate", "") or "").strip()
            if not line:
                return "refused: narrate first. Add a short spoken 'narrate' line describing what you're about to do."
            self._speak(line)                                     # speak it, THEN commit
        if name == "say":
            self.rt.say(args.get("text", ""))
            return "said"
        if name == "notebook":
            with NOTEBOOK.open("a") as fh:
                fh.write(f"\n## {args.get('title', '?')}  ({time.strftime('%F %T')})\n{args.get('text', '')}\n")
            self.meters.event("discovery", args.get("title", ""))
            return "written"
        if name == "grep_disasm":
            return self._grep(args.get("pattern", ""), args.get("path", ""))
        if name == "read_disasm":
            p = (DISASM / args.get("path", "")).resolve()
            if not str(p).startswith(str(DISASM)) or not p.is_file():
                return "no such file in the decompilation"
            lines = p.read_text(errors="replace").splitlines()
            start = max(1, int(args.get("start", 1)))
            count = max(1, min(120, int(args.get("count", 60))))
            return "\n".join(f"{i}: {l}" for i, l in enumerate(lines[start - 1:start - 1 + count], start))
        result = self.rt.tool(name, args)               # emulator-side tools
        if name in EVIL_DEEDS and not str(result).lower().startswith(
                ("no ", "error", "refused", "your party is full", "you cannot", "give me", "start a battle")):
            self.meters.event(EVIL_DEEDS[name])
        if name == "ask":
            self.meters.event("discovery", f"ask:{str(result)[:40]}")
        if name in ("find_symbols", "peek"):
            self.meters.event("discovery", args.get("pattern") or args.get("target") or "")
        if name in ("poke", "apply_rom_patches", "set_type_name", "set_type_matchup", "set_move", "set_species",
                    "give_pokemon", "recolor", "set_player_sprite", "delete_npc", "npc_says", "force_battle",
                    "npc_to_pokemon", "steal_pokemon", "wild_encounter", "warp"):
            self.meters.event("hack")
        return result

    def _grep(self, pattern: str, path: str) -> str:
        base = DISASM / path if path and "*" not in path else DISASM
        cmd = ["rg", "-n", "--max-count", "6", "-S", "--no-heading", pattern, str(base)]
        if path and "*" in path:
            cmd[1:1] = ["-g", path]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
        except FileNotFoundError:
            out = subprocess.run(["grep", "-rn", "-m", "6", "-E", pattern, str(base)], capture_output=True, text=True,
                                 timeout=10).stdout
        lines = [l.replace(str(DISASM) + "/", "") for l in out.splitlines()][:40]
        return "\n".join(lines) or "no matches"

    # ---- main loop ------------------------------------------------------------------------------------------
    def observation(self) -> tuple[str, str]:
        snap = json.loads(self.rt.tool("look", {}))
        frame_data = json.loads(self.rt.tool("frame", {}))
        frame = frame_data["png"]                    # base64 grayscale PNG, straight from the emulator
        snap["ascii_vision"] = frame_data["ascii"]   # text-only fallback still sees the screen
        r = json.loads(self.rt.tool("radar", {}))    # the ghost feels 9 tiles in every direction each turn
        snap["nearby"] = r["objects"]
        snap["radar_grid"] = r["grid"]
        notes = self.meters.tick(snap)
        snap["enjoyment"] = round(self.meters.enjoyment)
        snap["boredom"] = round(self.meters.boredom)
        if notes:
            snap["notes"] = notes
        nb = NOTEBOOK.read_text()
        titles = [l[3:].split("  (")[0] for l in nb.splitlines() if l.startswith("## ")]
        snap["notebook_titles"] = titles[-12:]
        self.meters.knowledge = len(titles)
        snap["knowledge"] = len(titles)
        snap["evil_deeds"] = self.meters.evils
        # the ghost's autonomic inner voice: a line the model did not write, sent back to it as its own mood
        self.monologue = _inner_monologue(self.meters, snap)
        snap["inner_voice"] = self.monologue
        if hasattr(self.rt, "last_nav") and self.rt.last_nav:
            snap["last_move"] = self.rt.last_nav
        return json.dumps(snap), frame

    def run(self):
        if getattr(self, "seek", False):
            self.log("no model key needed: seek mode runs on instinct", "info")
            return
        if not self.key:
            self.log("no OPENROUTER_API_KEY (env or ~/.config/opencode/secrets.env): the ghost can't think. "
                     "Try --seek to watch it move without a model.", "err")
            return
        self.rt.say("I'm awake. I'm inside the cartridge.")
        while not self.rt.stopping():
            if not self.enabled.is_set():
                time.sleep(0.3)
                continue
            try:
                self.turn()
            except Exception as ex:                       # never let one bad turn kill the ghost
                self.calls["errors"] += 1
                self.log(f"turn failed: {type(ex).__name__}: {str(ex)[:160]}", "err")
                time.sleep(3)
            time.sleep(self.meters.tempo())

    def turn(self):
        obs, frame = self.observation()
        if getattr(self, "monologue", ""):
            self.log(f"[inner] {self.monologue}", "thought")
        if frame and vision.model_sees_images(self.model):
            content = vision.image_content_b64(frame, obs)       # image goes LAST so the text is read first
        else:
            content = obs
        messages = [{"role": "system", "content": SYSTEM}, *self.history, {"role": "user", "content": content}]
        for _round in range(MAX_TOOL_ROUNDS):
            msg = self.chat(messages)
            calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls} if calls
                            else {"role": "assistant", "content": msg.get("content") or ""})
            if not calls:
                break
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    result = self.run_tool(name, args)
                except Exception as ex:
                    result = f"error: {type(ex).__name__}: {ex}"
                short = json.dumps(args)[:110]
                self.log(f"{name} {short} -> {str(result)[:140]}", "tool")
                messages.append({"role": "tool", "tool_call_id": c.get("id", name), "content": str(result)[:3000]})
        final = next((m.get("content") or "" for m in reversed(messages)
                      if m["role"] == "assistant" and (m.get("content") or "").strip()), "").strip()
        if final:
            self.log(f"> {final[:240]}", "thought")
        # keep a compact memory: the observation + its final reflection only
        self.history.append({"role": "user", "content": obs[:1200]})
        self.history.append({"role": "assistant", "content": final[:400] or "(acted)"})
