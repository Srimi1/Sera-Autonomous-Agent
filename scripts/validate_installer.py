#!/usr/bin/env python3
"""P-99: CI entry point — validate installer manifest for one OS (or all)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from sera.install.manifest import ManifestError, validate, validate_for_os  # noqa: E402


def main(argv: list[str]) -> int:
    try:
        if len(argv) > 1:
            target = validate_for_os(argv[1])
            print(f"OK: {target.os} → {target.format} {target.arch}")
        else:
            targets = validate()
            for t in targets:
                print(f"OK: {t.os} → {t.format} {t.arch} signed={t.codesign_required}")
    except ManifestError as e:
        print(f"INSTALLER MANIFEST INVALID: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
