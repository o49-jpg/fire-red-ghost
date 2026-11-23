#!/usr/bin/env bash
# FireRed Ghost setup: python env, deps, decompilation, ROM sanity check.
set -euo pipefail
cd "$(dirname "$0")"

ROM_EXPECTED="41cb23d8dccc8ebd7c649cd8fbb58eeace6e2fdc"
PY="${PYTHON:-python3}"

say() { printf '\033[35m::\033[0m %s\n' "$*"; }
die() { printf '\033[31m!!\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$PY" >/dev/null || die "python3 not found"
"$PY" - <<'EOF' || die "python 3.10+ required"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
EOF

if [ ! -d .venv ]; then
  say "creating .venv"
  "$PY" -m venv .venv
fi
say "installing python deps"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f disasm/charmap.txt ]; then
  say "fetching pret/pokefirered (shallow) into disasm/"
  command -v git >/dev/null || die "git not found (needed to fetch the decompilation)"
  tmp="$(mktemp -d)"
  git clone --depth 1 https://github.com/pret/pokefirered "$tmp/pfr"
  rm -rf "$tmp/pfr/.git"
  mv "$tmp/pfr" disasm
  rmdir "$tmp"
else
  say "decompilation already present"
fi

if [ ! -f roms/firered.gba ]; then
  die "put your legally dumped Pokemon FireRed (USA) v1.0 ROM at roms/firered.gba"
fi
got="$(sha1sum roms/firered.gba | cut -d' ' -f1)"
if [ "$got" != "$ROM_EXPECTED" ]; then
  die "roms/firered.gba is not FireRed USA v1.0 (sha1 $got; expected $ROM_EXPECTED)"
fi
say "ROM verified"

if [ ! -f integrations/PokemonFireRed-Gba/rom.gba ]; then
  cp roms/firered.gba integrations/PokemonFireRed-Gba/rom.gba
  say "seeded the live cartridge (it gets patched as the ghost hacks)"
fi

say "running self-tests"
.venv/bin/python -m ghost.selftest

cat <<'TIP'

  ready.
    .venv/bin/python run.py          # window + ghost (needs OPENROUTER_API_KEY)
    .venv/bin/python run.py --seek   # no model: watch it wander and speak
TIP
