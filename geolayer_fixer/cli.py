"""Command line: python -m geolayer_fixer --scan [FOLDER...] | --fix [--force] [FOLDER...]"""

import sys

from .core import default_folders
from .runner import run_batch


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--scan" in argv
    force = "--force" in argv
    folders = [a for a in argv if not a.startswith("--")] or default_folders()
    print(("Scanning" if dry else "Fixing"), folders)

    def show(r):
        print(f"{r.status.upper():9} {r.path}  {'; '.join(r.problems)} {r.message}")

    stats = run_batch(folders, force=force, dry_run=dry, on_result=show)
    print(dict(stats) or "No PNG files found.")
    return 1 if stats.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
