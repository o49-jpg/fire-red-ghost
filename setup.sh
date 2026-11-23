#!/usr/bin/env bash
# FireRed Ghost — one-shot setup.
#
#   ./setup.sh              # full setup, then run the live self-tests
#   ./setup.sh --skip-tests # same, but stop after wiring everything up
#   ./setup.sh --check      # verify an existing install, change nothing
#
# Everything is idempotent: re-running only does the parts that are missing.
set -euo pipefail
cd "$(dirname "$0")"

ROM_SHA1="41cb23d8dccc8ebd7c649cd8fbb58eeace6e2fdc"   # FireRed (USA) v1.0, BPRE
ROM="roms/firered.gba"
LIVE="integrations/PokemonFireRed-Gba/rom.gba"
SYM="pokefirered.sym"
RUN_TESTS=1
CHECK_ONLY=0
PY="${PYTHON:-python3}"

for arg in "$@"; do
  case "$arg" in
    --skip-tests|--no-tests) RUN_TESTS=0 ;;
    --check)                 CHECK_ONLY=1; RUN_TESTS=0 ;;
    -h|--help)               sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'unknown option: %s (try --help)\n' "$arg" >&2; exit 2 ;;
  esac
done

step() { printf '\033[35m::\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  !!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m!!\033[0m %s\n' "$*" >&2; exit 1; }

# sha1 of a file, on any platform: coreutils, BSD, or bare python
sha1() {
  if command -v sha1sum >/dev/null 2>&1; then sha1sum "$1" | cut -d' ' -f1
  elif command -v shasum  >/dev/null 2>&1; then shasum -a 1 "$1" | cut -d' ' -f1
  else "$PY" -c 'import hashlib,sys;print(hashlib.sha1(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  fi
}

human() { "$PY" -c 'import os,sys;n=os.path.getsize(sys.argv[1]);
print(f"{n/1048576:.0f} MB")' "$1" 2>/dev/null || echo "?"; }

# ---------------------------------------------------------------------------------------------------------------
# 1. PREFLIGHT — the things that need YOU, checked before anything is downloaded.
#    Getting this wrong after a 156 MB pip install is the worst part of any first run.
# ---------------------------------------------------------------------------------------------------------------
step "preflight"

command -v "$PY" >/dev/null 2>&1 || die "python3 not found. Install Python 3.10 or newer."
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "$("$PY" -V) is too old: this needs Python 3.10+ (it uses modern typing and match-free syntax)."
ok "python $("$PY" -c 'import platform;print(platform.python_version())')"

if [ ! -f "$ROM" ]; then
  mkdir -p roms
  cat >&2 <<EOF
$(printf '\033[31m!!\033[0m') no cartridge at $ROM

  This project does not ship the ROM: it is Nintendo's copyrighted code, and the
  ghost needs YOUR legally dumped copy. Dump it from a cartridge you own.

  Put it here:      $ROM
  It must be:       Pokemon FireRed (USA) v1.0  ==  Pokemon - FireRed Version (USA)
  SHA1 must be:     $ROM_SHA1

  Already have a dump under another name? Copy it into place and re-run:

      cp /path/to/your\ dump.gba $ROM
      ./setup.sh

  Then this script verifies the hash and does the rest by itself.
EOF
  exit 1
fi

got="$(sha1 "$ROM")"
if [ "$got" != "$ROM_SHA1" ]; then
  cat >&2 <<EOF
$(printf '\033[31m!!\033[0m') $ROM is not FireRed (USA) v1.0

  found:    $got
  expected: $ROM_SHA1

  size:     $(stat -c %s "$ROM" 2>/dev/null || stat -f %z "$ROM") bytes (a GBA ROM is 16777216)

  The symbol map is generated from this exact ROM revision, so every memory read
  in the project would land on the wrong address with any other dump. Common
  causes: a European/Japanese ROM, a v1.1 revision, a pre-patched/hacked ROM, or a
  file that is not a ROM at all.
EOF
  exit 1
fi
ok "cartridge verified ($(human "$ROM"))"

[ -f "$SYM" ] || die "missing $SYM — the symbol map that turns addresses into names. Re-clone the repository."
ok "symbol map present ($(human "$SYM"))"

if [ "$CHECK_ONLY" = 1 ]; then
  # verify the rest without touching anything
  [ -x .venv/bin/python ] || die "no .venv — run ./setup.sh without --check"
  [ -f disasm/charmap.txt ] || die "no disasm/charmap.txt — run ./setup.sh without --check"
  [ -f "$LIVE" ] || die "no live cartridge — run ./setup.sh without --check"
  step "running self-tests"
  .venv/bin/python -m ghost.selftest
  ok "install is healthy"; exit 0
fi

# ---------------------------------------------------------------------------------------------------------------
# 2. PYTHON ENVIRONMENT
# ---------------------------------------------------------------------------------------------------------------
step "python environment"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv 2>/dev/null || die "could not create .venv.
  On Debian/Ubuntu this usually means the venv module is missing:
      sudo apt install python3-venv"
  ok "created .venv"
else
  ok ".venv already exists"
fi

# only install when something is actually missing, so re-runs are instant
if .venv/bin/python -c "import stable_retro, numpy, PIL, pygame" >/dev/null 2>&1; then
  ok "dependencies already installed"
else
  step "installing dependencies (stable-retro is ~120 MB: this is the slow part)"
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt \
    || die "dependency install failed — see the pip output above"
  ok "dependencies installed"
fi

# ---------------------------------------------------------------------------------------------------------------
# 3. DECOMPILATION — used as a reference (charmap + disassembly text), never built.
# ---------------------------------------------------------------------------------------------------------------
step "pret/pokefirered reference"
if [ -f disasm/charmap.txt ]; then
  ok "decompilation already present ($(du -sh disasm 2>/dev/null | cut -f1))"
else
  command -v git >/dev/null 2>&1 || die "git is required to fetch the decompilation"
  [ -d disasm ] && rm -rf disasm          # half-finished clone from an interrupted run
  saylog="$(mktemp)"
  git clone --depth 1 --quiet https://github.com/pret/pokefirered disasm 2>"$saylog" \
    || { cat "$saylog" >&2; rm -f "$saylog"; die "could not clone pret/pokefirered"; }
  rm -f "$saylog"
  rm -rf disasm/.git                      # ~40 MB of history we will never use
  [ -f disasm/charmap.txt ] || die "clone finished but disasm/charmap.txt is missing"
  ok "fetched decompilation ($(du -sh disasm | cut -f1)) — used for text and symbols, never compiled"
fi

# ---------------------------------------------------------------------------------------------------------------
# 4. LIVE CARTRIDGE — the copy the ghost is allowed to patch.
# ---------------------------------------------------------------------------------------------------------------
step "live cartridge"
if [ ! -f "$LIVE" ]; then
  mkdir -p "$(dirname "$LIVE")"
  cp "$ROM" "$LIVE"
  ok "seeded from your dump"
else
  live_sha="$(sha1 "$LIVE")"
  if [ "$live_sha" = "$ROM_SHA1" ]; then
    ok "pristine (the ghost has not hacked it yet)"
  else
    warn "the live cartridge is currently PATCHED ($(sha1 "$LIVE" | cut -c1-12)…) — that is the ghost's own work."
    printf '     keep playing with ./run.py, or factory-reset with: ./run.py --fresh\n'
  fi
fi

# ---------------------------------------------------------------------------------------------------------------
# 5. SELF-TESTS
# ---------------------------------------------------------------------------------------------------------------
if [ "$RUN_TESTS" = 1 ]; then
  step "self-tests (live emulator: 21 checks)"
  if .venv/bin/python -m ghost.selftest; then
    ok "all checks passed"
  else
    die "the self-tests failed — the install is not usable yet (see the FAIL lines above)"
  fi
fi

cat <<'TIP'

  ready.  next:

    .venv/bin/python run.py            # window + the model-driven ghost (needs OPENROUTER_API_KEY)
    .venv/bin/python run.py --seek     # no LLM: watch it wander, narrate, and talk through the game
    .venv/bin/python run.py --soul     # what it remembers from its previous lives
    .venv/bin/python run.py --fresh    # factory-reset the cartridge and archive the notebook

  no API key and want to test the plumbing?  .venv/bin/python -m ghost.fakechat
  controls: arrows move · Z A · X B · Enter START · P pause the ghost · U undo · F12 restore ROM · Esc quit
TIP
