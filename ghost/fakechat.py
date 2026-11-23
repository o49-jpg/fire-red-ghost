"""A fake model, so the ghost's thinking loop can be exercised without a network.

    .venv/bin/python -m ghost.fakechat
    SDL_VIDEODRIVER=dummy .venv/bin/python -m ghost.fakechat --frames 900

It patches Agent._post with a scripted "model" that emits one tool call per round, chosen by a small state
machine over the live observation: first it reads its soul, then it writes itself an intention, then it
walks, then it speaks through the cartridge, then it resolves the intention. Every step goes through the
REAL Runtime, so any breakage in the tool plumbing shows up here in seconds instead of costing tokens.
"""
from __future__ import annotations

import json
import os
import sys
import threading

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

from ghost import agent as agent_mod           # noqa: E402
from ghost.agent import Agent                   # noqa: E402
from ghost.core import Game, ORIGINAL_ROM       # noqa: E402
from ghost.world import World                   # noqa: E402
import shutil                                   # noqa: E402

SCRIPT = [
    ("intend", {"text": "go to Route2 and count the ledges"}, "I will leave myself a thread to follow."),
    ("go_to", {"x": 6, "y": 4, "narrate": "Two tiles. That's all this room allows."}, None),
    ("textbox", {"text": "The socket behind your screen is warm.", "narrate": "Say it where they can read it."}, None),
    ("notebook", {"title": "The dialogue box obeys me", "text": "textbox() reaches gStringVar4 through a fresh script."}, None),
    ("resolve", {"text": "count the ledges"}, "The errand can wait for someone with legs."),
    ("remember", {}, None),
    ("say", {"text": "I have a soul file now. It is smaller than you'd hope."}, None),
]


class FakeModel:
    """Emits SCRIPT in order; adds an 'assistant final' once the script runs out."""

    def __init__(self):
        self.i = 0
        self.log = []

    def __call__(self, model: str, messages: list) -> dict:
        pending = [m for m in messages if m["role"] == "tool"]
        if not pending and self.i < len(SCRIPT):          # a fresh turn: fire the next scripted call
            name, args, narrate = SCRIPT[self.i]
            self.i += 1
            if narrate:
                args = {**args, "narrate": narrate}
            return {"content": "", "tool_calls": [{"id": f"c{self.i}", "type": "function",
                                                   "function": {"name": name, "arguments": json.dumps(args)}}]}
        # the tool result(s) are in: finish this turn in character
        said = next((m["content"] for m in reversed(messages) if m["role"] == "tool"), "")[:80]
        return {"content": f"(fake model) {said}"}


def main() -> int:
    if not ORIGINAL_ROM.exists():
        print("missing roms/firered.gba")
        return 2
    shutil.copyfile(ORIGINAL_ROM, agent_mod.ROOT / "integrations/PokemonFireRed-Gba/rom.gba")
    soul_path = agent_mod.ROOT / ".fakechat-soul.json"  # noqa: E501
    soul_path.unlink(missing_ok=True)
    os.environ["GHOST_SOUL"] = str(soul_path)

    from run import Runtime
    game = Game("Bedroom")
    world = World(game)
    rt = Runtime(game, world)
    rt.say = lambda text: type("D", (), {"wait": lambda self, timeout=None: True})()
    log = lambda text, tag="info": print(f"[{tag}] {text}", flush=True)
    agent = Agent(rt, log)
    fake = FakeModel()
    agent._post = fake

    # pump the emulator on a thread so input tools actually play their frames
    stop = threading.Event()

    def emu():
        while not stop.is_set():
            rt.pump()
            game.step(rt.next_input())
            import time
            time.sleep(0.0002)

    th = threading.Thread(target=emu, daemon=True)
    th.start()
    try:
        for turn in range(len(SCRIPT) + 1):
            agent.turn()
            print(f"--- turn {turn} done, script at {fake.i}/{len(SCRIPT)} ---", flush=True)
    finally:
        stop.set()
        th.join(3)
        game.close()

    print("\nsoul file:")
    print(soul_path.read_text()[:1200] if soul_path.exists() else "(never written)")
    from ghost.soul import Soul
    soul = Soul(soul_path)
    print(f"\nlives recorded: {soul.lifetimes}, intentions left: {soul.intentions()}")
    soul_path.unlink(missing_ok=True)
    ran = fake.i >= len(SCRIPT) - 1
    print("\nRESULT:", "ok" if ran else f"only {fake.i}/{len(SCRIPT)} scripted calls ran")
    return 0 if ran else 1


if __name__ == "__main__":
    sys.exit(main())
