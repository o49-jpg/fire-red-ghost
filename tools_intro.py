"""Mash through the FireRed intro headless; screenshots every N frames to /tmp/fr_intro_*.png."""
import os, sys, numpy as np, stable_retro as retro
from PIL import Image
retro.data.Integrations.add_custom_path(os.path.abspath("integrations"))
env = retro.make("PokemonFireRed-Gba", state=retro.State.NONE, inttype=retro.data.Integrations.ALL, use_restricted_actions=retro.Actions.ALL)
env.reset(); B = env.buttons
def press(name, hold=2, after=12):
    a = np.zeros(len(B), np.int8); a[B.index(name)] = 1
    for _ in range(hold): env.step(a)
    for _ in range(after): obs, *_ = env.step(np.zeros(len(B), np.int8))
    return obs
total = int(sys.argv[1]) if len(sys.argv) > 1 else 6000
f = 0; shot = 0
while f < total:
    obs = press("A"); f += 14
    if f // 1500 > shot:
        shot = f // 1500; Image.fromarray(obs).save(f"/tmp/fr_intro_{shot:02d}.png")
import gzip
with gzip.open("/tmp/fr_intro.state", "wb") as fh: fh.write(env.em.get_state())
print("frames", f)
