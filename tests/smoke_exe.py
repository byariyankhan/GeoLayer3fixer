"""CI smoke test for the built exe.

  python tests/smoke_exe.py make  DIR   -> writes 30 tiles (clean / CRC-broken / filter-broken)
  python tests/smoke_exe.py check DIR   -> verifies what GeoLayer_Fixer.exe --fix did

30 files > 16, so the exe takes the multi-process path (the one that needs
multiprocessing.freeze_support to work inside a PyInstaller exe).
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from geolayer_fixer.core import inspect_bytes  # noqa: E402
from tests.test_core import bad_crc, bad_filter, make_png  # noqa: E402

KINDS = ["clean", "crc", "filter"]


def make(d):
    os.makedirs(d, exist_ok=True)
    for i in range(30):
        kind = KINDS[i % 3]
        data = {"clean": make_png, "crc": lambda: bad_crc(make_png()),
                "filter": lambda: bad_filter(make_png())}[kind]()
        with open(os.path.join(d, f"{kind}_{i}.png"), "wb") as f:
            f.write(data)


def check(d):
    good = Image.open(io.BytesIO(make_png())).tobytes()
    errors = []
    for i in range(30):
        kind = KINDS[i % 3]
        p = os.path.join(d, f"{kind}_{i}.png")
        if kind == "filter":
            if os.path.exists(p):
                errors.append(f"{p}: should have been quarantined")
            continue
        data = open(p, "rb").read()
        if inspect_bytes(data):
            errors.append(f"{p}: still broken")
        elif Image.open(io.BytesIO(data)).tobytes() != good:
            errors.append(f"{p}: pixels changed")
    leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
    if leftovers:
        errors.append(f"temp files left: {leftovers}")
    print("\n".join(errors) or "smoke test OK: 10 clean, 10 repaired losslessly, 10 quarantined")
    return 1 if errors else 0


if __name__ == "__main__":
    mode, folder = sys.argv[1], sys.argv[2]
    sys.exit(make(folder) or 0 if mode == "make" else check(folder))
