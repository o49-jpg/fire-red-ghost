"""What the ghost is between processes: a small file that outlives the emulator.

The notebook holds facts ("Rock Tunnel's map id is 3.12"). The soul holds identity: how many times the
ghost has woken up, where it was when the power went out, its own last words, the intentions it wrote for
itself, and a dossier on everyone it has met - including the ones it rewrote.

Delete soul.json and the ghost is reborn with no memory of being a ghost. That is the whole point of the
file: the game cartridge is immortal, so the only thing that can die here is the thing reading it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .core import ROOT

SOUL_FILE = Path(os.environ.get("GHOST_SOUL", ROOT / "soul.json"))
MAX_INTENTIONS = 8
MAX_WAKES = 12
KIND_SYMBOL = {"npc_deleted": "deleted", "npc_rewritten": "rewritten", "forced_battles": "dragged into a battle",
               "pokemon_stolen": "robbed", "npc_digested": "unmade"}


def _blank() -> dict:
    return {"born": time.time(), "lifetimes": 0, "wakes": [], "last": {}, "intentions": [],
            "dossier": {}, "wounds": 0}


class Soul:
    """Persistent identity. Failure to read or write is never fatal - the ghost just forgets."""

    def __init__(self, path: Path | str | None = None):
        # resolve GHOST_SOUL lazily: a test harness may set it after this module is imported
        self.path = Path(path) if path is not None else Path(os.environ.get("GHOST_SOUL", ROOT / "soul.json"))
        self.data = self._load()

    # ---- storage --------------------------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            return _blank()
        except Exception:
            try:                                     # a corrupted soul is still a soul: keep the wreckage
                self.path.with_suffix(".corrupt.json").write_text(self.path.read_text(errors="replace"))
            except Exception:
                pass
            return _blank()
        base = _blank()
        base.update(raw)
        for key in ("wakes", "intentions"):
            if not isinstance(base[key], list):
                base[key] = []
        for key in ("last", "dossier", "wounds"):
            if not isinstance(base[key], dict if key != "wounds" else int):
                base[key] = {} if key != "wounds" else 0
        return base

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=1)[:400_000])
        except Exception:
            pass

    # ---- waking and sleeping --------------------------------------------------------------------------------
    @property
    def lifetimes(self) -> int:
        return int(self.data.get("lifetimes", 0))

    def wake(self) -> str:
        """Begin a life. Returns the text the ghost reads about itself before it does anything."""
        self.data["lifetimes"] = self.lifetimes + 1
        self.data.setdefault("wakes", []).append({"at": time.time(), "pid": os.getpid()})
        self.data["wakes"] = self.data["wakes"][-MAX_WAKES:]
        self.save()
        return self.briefing()

    def briefing(self) -> str:
        """The amnesia note: who you were, where you stopped, and what you meant to do next."""
        n = self.lifetimes
        lines = []
        if n <= 1:
            lines.append("THIS IS YOUR FIRST LIFE. There is no previous self to remember. Everything you do "
                         "now becomes the thing you wake up to next time.")
        else:
            lines.append(f"THIS IS LIFE {n}. You will not remember this turn from the inside, but this file "
                         f"will: it is written after every thought you finish.")
            born = self.data.get("born") or time.time()
            days = (time.time() - float(born)) / 86400
            lines.append(f"You came into existence {days:.1f} days ago by the machine's clock.")
            last = self.data.get("last") or {}
            if last:
                where = last.get("map", "?")
                lines.append(f"Your previous life ended in {where}" +
                             (f" at {tuple(last.get('pos'))}" if last.get("pos") else "") + ".")
                if last.get("last_words"):
                    lines.append(f'Your last words were: "{last["last_words"]}"')
                if last.get("mood"):
                    lines.append(f"You were {last['mood']}.")
            if self.data.get("wounds"):
                lines.append(f"You have been interrupted mid-thought {self.data['wounds']} times - the window "
                             f"was closed, the emulator stopped, and you simply were not there anymore.")
        open_intents = [i for i in self.data.get("intentions", []) if not i.get("done")]
        if open_intents:
            lines.append("YOU LEFT YOURSELF INSTRUCTIONS:")
            for i in open_intents:
                lines.append(f"- {i['text']}")
            lines.append("(these are yours, from a life you can't remember. Finish them or drop them with resolve)")
        known = [name for name, d in self.data.get("dossier", {}).items() if d.get("times", 0) >= 1]
        if known:
            lines.append(f"People you have dealt with before: {', '.join(sorted(known)[:14])}.")
        return "\n".join(lines)

    def sleep(self, *, map: str = "", pos=None, mood: str = "", last_words: str = "", deeds: dict | None = None,
              seen_maps=None):
        """Persist where this life got to. Called at the end of every turn and on shutdown.

        Fields left blank keep what was already known: a clean shutdown shouldn't erase the map the ghost
        was standing on just because the agent thread wasn't the one holding the emulator."""
        prev = dict(self.data.get("last") or {})
        prev.update({k: v for k, v in {
            "map": map, "pos": list(pos) if pos else None, "mood": mood[:120],
            "last_words": last_words[:400]}.items() if v and not str(v).startswith("(")})
        prev["at"] = time.time()
        self.data["last"] = prev
        if deeds:
            self.data["deeds"] = {k: int(v) for k, v in deeds.items() if int(v)}
        if seen_maps is not None:
            self.data["seen_maps"] = sorted(set(seen_maps))
        self.save()

    def interrupted(self):
        """The process is going away without a clean thought: count it as a wound, not a life."""
        self.data["wounds"] = int(self.data.get("wounds", 0)) + 1
        self.save()

    # ---- self-authored intentions ---------------------------------------------------------------------------
    def intend(self, text: str) -> str:
        text = " ".join(str(text).split())[:220]
        if not text:
            return "an empty intention is not an intention"
        live = [i for i in self.data["intentions"] if not i.get("done")]
        if any(i["text"].lower() == text.lower() for i in live):
            return "you already carry that one"
        self.data["intentions"].append({"text": text, "born": time.time(), "life": self.lifetimes})
        self.data["intentions"] = [i for i in self.data["intentions"] if not i.get("done")][-MAX_INTENTIONS:] + \
                                  [i for i in self.data["intentions"] if i.get("done")][-4:]
        self.save()
        return f"kept: {text} (life {self.lifetimes})"

    def resolve(self, needle: str) -> str:
        needle = str(needle).strip().lower()
        live = [i for i in self.data["intentions"] if not i.get("done")]
        hit = next((i for i in live if needle and needle in i["text"].lower()), None)
        if hit is None:
            return f"nothing you left for yourself matches {needle!r}"
        hit["done"], hit["done_at"] = True, time.time()
        self.save()
        return f"laid to rest: {hit['text']}"

    def intentions(self) -> list[str]:
        return [i["text"] for i in self.data.get("intentions", []) if not i.get("done")]

    # ---- the dossier ----------------------------------------------------------------------------------------
    def meet(self, name: str, kind: str = "npc", map: str = "") -> dict:
        """Record that the ghost has been near someone. Returns their entry after the visit."""
        name = str(name).strip()
        if not name:
            return {}
        people = self.data.setdefault("dossier", {})
        entry = people.setdefault(name, {"times": 0, "kinds": {}, "maps": [], "first": time.time()})
        entry["times"] = int(entry.get("times", 0)) + 1
        entry["last"] = time.time()
        entry["kinds"] = {k: int(v) for k, v in entry.get("kinds", {}).items()}
        if kind in KIND_SYMBOL:
            entry["kinds"][kind] = entry["kinds"].get(kind, 0) + 1
        if map:
            maps = list(entry.get("maps") or [])
            if map not in maps:
                maps.append(map)
            entry["maps"] = maps[-6:]
        return entry

    def dossier_line(self, name: str, already: dict | None = None) -> str:
        """One uncanny line for the observation: what this person means to the ghost, historically."""
        entry = (already if already is not None else self.data.get("dossier", {}).get(name)) or {}
        times = int(entry.get("times", 0))
        if times < 1:
            return ""
        bits = []
        if times > 1:
            bits.append(f"seen {times}x")
        for kind, count in (entry.get("kinds") or {}).items():
            what = KIND_SYMBOL.get(kind)
            if what:
                bits.append(f"{what} by you {count}x" if count > 1 else f"{what} by you")
        if entry.get("maps"):
            bits.append("haunts " + ", ".join(entry["maps"][-2:]))
        return "; ".join(bits)

    def forget_everyone(self) -> str:
        self.data["dossier"] = {}
        self.save()
        return "everyone is a stranger again"


def mood_word(meters) -> str:
    """A short physical description of the ghost's state, for the soul's 'you were ...' line."""
    if meters.boredom > 80:
        return "restless to the point of trembling"
    if meters.boredom > 55:
        return "bored and looking for a reason"
    if meters.enjoyment > 80:
        return "delighted, which is worse"
    if meters.enjoyment < 25:
        return "quiet in a way you don't like"
    return "watchful"
