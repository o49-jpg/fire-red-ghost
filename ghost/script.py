"""The ghost's voice inside the game itself: it makes the cartridge's own script engine run its scripts.

This is how the ghost talks through the real FireRed dialogue box, instead of only through speakers next
to an emulator window. The game's script interpreter (src/script.c) is bytecode, and the *global* script
context is driven from RAM: fill in the context struct and mark it running, and the overworld loop calls
ScriptContext_RunScript() once a frame like it does for any NPC's script.

    sGlobalScriptContext    0x03000eb0   struct ScriptContext (src/script.c:35 sGlobalScriptContext)
        +0x00 u8  stackDepth
        +0x01 u8  mode           1 = SCRIPT_MODE_BYTECODE, 2 = NATIVE
        +0x02 u8  comparisonResult
        +0x04 u32 nativePtr
        +0x08 u32 scriptPtr
        +0x0c u32 stack[20]
        +0x5c u32 cmdTable       gScriptCmdTable
        +0x60 u32 cmdTableEnd    gScriptCmdTableEnd
    sGlobalScriptContextStatus  0x03000ea8  0 = RUNNING, 1 = WAITING, 2 = SHUTDOWN

Script bytecode is written into free ROM (never executed as code - only read as data by the interpreter)
and the pointer dropped into the context. Everything here is RAM: no ROM patch, no hot-swap, no glitch.

Event opcodes used (asm/macros/event.inc):
    end 0x02 | goto 0x05 | callstd 0x09 | loadword 0x0f | playse 0x2f | waitbuttonpress 0x6d
    playbgm 0x33 | delay 0xa1 | setweather 0xa4 | doweather 0xa5 | fadescreen 0x97
"""
from __future__ import annotations

import struct

from .core import encode_text

CTX = 0x03000EB0
STATUS = 0x03000EA8
CTX_CMDTABLE_OFF, CTX_CMDTABLEEND_OFF = 0x5C, 0x60
STATUS_RUNNING, STATUS_SHUTDOWN = 0, 2

# callstd ids (asm/macros/event.inc:53-62)
MSGBOX_NPC, MSGBOX_SIGN, MSGBOX_DEFAULT, MSGBOX_YESNO, MSGBOX_AUTOCLOSE = 2, 3, 4, 5, 6
END, GOTO, CALLSTD, LOADWORD = 0x02, 0x05, 0x09, 0x0F
# variable access (asm/macros/event.inc:153-170, 253): setvar/compare/goto_if on VAR_RESULT = 0x800D
SETVAR, COMPARE = 0x16, 0x21
GOTO_IF_EQ, GOTO_IF_LT, GOTO_IF_GT = 0x06, 0x06, 0x06      # opcode 0x06 + condition byte
VAR_RESULT = 0x800D
PLAYSE, PLAYBGM, DELAY, SETWEATHER, DOWEATHER = 0x2F, 0x33, 0xA1, 0xA4, 0xA5
WAITBUTTONPRESS = 0x6D

SE = {"shiny": 95, "thunder": 111, "fire": 140, "water": 157, "explosion": 171,
      "psychic": 182, "earthquake": 227, "lick": 181, "screech": 174, "confuse": 189,
      "string": 122, "flee": 17, "exit": 9}
WEATHER = {"none": 0, "sunny_clouds": 1, "sunny": 2, "rain": 3, "snow": 4, "thunderstorm": 5,
           "fog": 6, "ash": 7, "sandstorm": 8}


class ScriptEngine:
    def __init__(self, game, world):
        self.g, self.w = game, world
        self.last_ptr: int | None = None
        self.last_text: str = ""

    # ---- building blocks ------------------------------------------------------------------------------
    def loadword(self, text_addr: int) -> bytes:
        return bytes([LOADWORD, 0]) + struct.pack("<I", text_addr)

    def msgbox(self, text_addr: int, kind: int = MSGBOX_DEFAULT) -> bytes:
        return self.loadword(text_addr) + bytes([CALLSTD, kind])

    def frame_script(self, body: bytes) -> bytes:
        """A script that runs to completion on its own: `end` closes the message box."""
        return body + bytes([END])

    def place(self, code: bytes, blob: bytes) -> int:
        """Write `blob` + `code` into free ROM and return the address of `code`."""
        addr = self.w._free_rom(len(blob) + len(code) + 8)
        self.g.rom_patch(addr, blob + code)
        return addr + len(blob)

    # ---- running -------------------------------------------------------------------------------------
    def status(self) -> int:
        return self.g.u8(STATUS)

    def busy(self) -> bool:
        """True while a script the ghost launched is still executing (e.g. waiting for an A press)."""
        return self.status() != STATUS_SHUTDOWN and self.g.u8(CTX + 1) == 1

    def install(self, script_addr: int) -> None:
        """Point the global script context at `script_addr` and set it RUNNING (RAM only)."""
        g = self.g
        g.write(CTX, bytes([0, 1, 0, 0]))                                    # stackDepth, mode=BYTECODE
        g.write(CTX + 4, struct.pack("<I", 0))                               # nativePtr
        g.write(CTX + 8, struct.pack("<I", script_addr))                     # scriptPtr
        g.write(CTX + CTX_CMDTABLE_OFF, struct.pack("<II", g.sym("gScriptCmdTable").addr,
                                                    g.sym("gScriptCmdTableEnd").addr))
        g.write(STATUS, bytes([STATUS_RUNNING]))
        self.last_ptr = script_addr

    # ---- the public acts -----------------------------------------------------------------------------
    def say(self, text: str, *, hold: bool = False) -> str:
        """Show `text` in the game's own dialogue box.

        hold=False (default): the script doesn't wait - the player can keep walking and the box closes on
        its own with MSGBOX_AUTOCLOSE. hold=True: execution blocks until A/B, exactly like talking to an NPC.
        """
        clean = " ".join(text.split())[:180]
        text_bytes = encode_text(clean)
        body = self.msgbox(0, MSGBOX_AUTOCLOSE if not hold else MSGBOX_DEFAULT)
        script_addr = self.w._free_rom(len(text_bytes) + len(body) + 8)
        text_addr = script_addr
        script = script_addr + len(text_bytes)
        body = self.loadword(text_addr) + bytes([CALLSTD, MSGBOX_AUTOCLOSE if not hold else MSGBOX_DEFAULT])
        self.g.rom_patch(script_addr, text_bytes + self.frame_script(body))
        self.g.hot_swap()
        self.install(script)
        self.last_text = clean
        return f'the game itself says: "{clean}"' + (" (it waits for A)" if hold else "")

    def run_ops(self, ops: list[bytes]) -> str:
        """Run a script from raw opcode chunks. Used by the scripted atmosphere tools below."""
        body = b"".join(ops)
        script_addr = self.w._free_rom(len(body) + 8)
        self.g.rom_patch(script_addr, self.frame_script(body))
        self.g.hot_swap()
        self.install(script_addr)
        return "the cartridge obeyed"

    def weather(self, kind: str) -> str:
        name = str(kind).strip().lower().replace(" ", "_")
        if name in ("clear", "storm"):
            name = {"clear": "sunny", "storm": "thunderstorm"}[name]
        if name not in WEATHER:
            return f"no weather called {kind!r}; options: {', '.join(WEATHER)}"
        self.run_ops([bytes([SETWEATHER]) + struct.pack("<H", WEATHER[name]), bytes([DOWEATHER])])
        return f"weather over the map is now {name}"

    def music(self, song: int) -> str:
        self.run_ops([bytes([PLAYBGM]) + struct.pack("<H", int(song) & 0xFFFF) + bytes([1])])
        return f"the game's own soundtrack now plays song {int(song)}"

    def sfx(self, name: str) -> str:
        key = str(name).strip().lower()
        if key not in SE:
            return f"no SE called {name!r}; options: {', '.join(SE)}"
        self.run_ops([bytes([PLAYSE]) + struct.pack("<H", SE[key])])
        return f"the cartridge plays {key}"

    def atmosphere(self, weather: str = "thunderstorm", se: str = "thunder") -> str:
        """One script: change the weather AND hit a sound effect - thunder rolling through the room."""
        name = str(weather).strip().lower()
        ops = []
        if name in WEATHER:
            ops += [bytes([SETWEATHER]) + struct.pack("<H", WEATHER[name]), bytes([DOWEATHER])]
        if str(se).strip().lower() in SE:
            ops += [bytes([PLAYSE]) + struct.pack("<H", SE[str(se).strip().lower()])]
        self.run_ops(ops)
        return f"weather -> {weather}, sound -> {se}"

    def ask_stage(self, question: str) -> str:
        """Put a YES/NO question in the game's own choice box. The script engine blocks until A is pressed."""
        clean = " ".join(question.split())[:170]
        text_bytes = encode_text(clean)
        base = self.w._free_rom(len(text_bytes) + 32)
        script = self.loadword(base) + bytes([CALLSTD, MSGBOX_YESNO]) + bytes([END])
        self.g.rom_patch(base, text_bytes + script)
        self.g.hot_swap()
        self.install(base + len(text_bytes))
        self.last_text = clean
        return clean

    def ask_answer(self, timeout: float = 45.0) -> str:
        """Wait for the player to answer the staged YES/NO question (script context leaves BYTECODE)."""
        import time
        started = time.time()
        while time.time() - started < timeout:
            if not self.busy():
                break
            time.sleep(0.05)
        answer = self.g.u16(self.g.sym("gSpecialVar_Result").addr)          # VAR_RESULT
        return "yes" if answer else "no"

    def summon_effect(self, effect: str) -> str:
        """Loud visual: fadescreen then a battle-style sound. (The game keeps running through it.)"""
        se = SE.get(str(effect).strip().lower(), SE["psychic"])
        self.run_ops([bytes([0x97, 1]), bytes([DELAY]) + struct.pack("<H", 30),
                      bytes([PLAYSE]) + struct.pack("<H", se), bytes([0x97, 2]), bytes([DELAY]) + struct.pack("<H", 30)])
        return f"the screen tore itself apart and came back ({effect})"
