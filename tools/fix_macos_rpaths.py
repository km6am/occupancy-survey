#!/usr/bin/env python3
"""Remove duplicate LC_RPATH entries from Mach-O files under a prefix.

macOS 15's dyld REFUSES TO LOAD a Mach-O carrying duplicate LC_RPATH
entries -- not merely skipping the duplicate, but failing the load even
when handed the file's absolute path. Several conda-forge builds carry
`@loader_path` twice, or a path alongside the same path with a trailing
slash: textually distinct, semantically identical. Older dyld tolerated it.

The symptom is misleading. dyld reports a *search* failure, so it reads as
"library not found" when the file is sitting right there, and numpy wraps
it in a "you should not try to import numpy from its source directory"
message that sends you looking in entirely the wrong place.

    python3 tools/fix_macos_rpaths.py ~/radioconda                  # dry run
    python3 tools/fix_macos_rpaths.py ~/radioconda --apply -o log.json

Reversible: the log records every deletion, and `install_name_tool
-add_rpath` puts one back. Each edited file is re-signed ad-hoc, because
editing invalidates the existing signature and an unsigned arm64 dylib
will not load.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

MACHO_MAGIC = (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")


def is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) in MACHO_MAGIC
    except OSError:
        return False


def rpaths(path: Path) -> list[str]:
    try:
        out = subprocess.run(["otool", "-l", str(path)], capture_output=True,
                             text=True, timeout=60).stdout
    except Exception:
        return []
    found, lines = [], out.splitlines()
    for i, ln in enumerate(lines):
        if "LC_RPATH" not in ln:
            continue
        for j in range(i, min(i + 4, len(lines))):
            s = lines[j].strip()
            if s.startswith("path "):
                found.append(s[5:].split(" (offset")[0])
                break
    return found


def duplicates(rp: list[str]) -> list[str]:
    """Entries to delete so one of each distinct path remains."""
    keep, drop = set(), []
    for original in rp:
        norm = original.rstrip("/")
        if norm in keep:
            drop.append(original)
        else:
            keep.add(norm)
    return drop


def scan(prefix: Path, include_pkgs: bool):
    """Mach-O files with duplicate rpaths, deduplicated by inode."""
    seen, out = set(), []
    for p in prefix.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        if not (p.suffix in (".dylib", ".so") or ".dylib." in p.name):
            continue
        # conda's package cache holds extracted tarballs that are never on
        # a load path. Fixing them is harmless but pointless, and it buries
        # the files that matter in the output.
        if not include_pkgs and "/pkgs/" in str(p):
            continue
        try:
            ino = p.stat().st_ino
        except OSError:
            continue
        if ino in seen or not is_macho(p):
            continue
        rp = rpaths(p)
        drop = duplicates(rp)
        if drop:
            seen.add(ino)          # hard links share the file; fix it once
            out.append((p, drop))
    return out


def fix(path: Path, drop: list[str]) -> str | None:
    """Delete the duplicates and re-sign. Returns an error string or None."""
    for d in drop:
        r = subprocess.run(["install_name_tool", "-delete_rpath", d,
                            str(path)], capture_output=True, text=True)
        if r.returncode != 0:
            return r.stderr.strip()[:200]
    # Verify rather than trust: install_name_tool's own reporting has been
    # unreliable enough here to be worth a second look at the file.
    if duplicates(rpaths(path)):
        return "duplicates remain after delete"
    subprocess.run(["codesign", "-f", "-s", "-", str(path)],
                   capture_output=True)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prefix", help="e.g. ~/radioconda")
    ap.add_argument("--apply", action="store_true",
                    help="actually edit; without it this is a dry run")
    ap.add_argument("-o", "--log", help="JSON log of what was changed")
    ap.add_argument("--include-pkgs", action="store_true",
                    help="also fix conda's package cache (not on any load "
                         "path; off by default)")
    ap.add_argument("--max-passes", type=int, default=3)
    args = ap.parse_args()

    prefix = Path(args.prefix).expanduser()
    if not prefix.is_dir():
        print(f"no such prefix: {prefix}")
        return 2

    changed, failed = [], []
    for _pass in range(args.max_passes):
        found = scan(prefix, args.include_pkgs)
        if not found:
            break
        if not args.apply:
            changed = [{"file": str(p), "would_delete": d} for p, d in found]
            break
        progressed = False
        for p, drop in found:
            err = fix(p, drop)
            if err:
                failed.append({"file": str(p), "error": err})
            else:
                changed.append({"file": str(p), "deleted": drop})
                progressed = True
        if not progressed:
            break

    remaining = len(scan(prefix, args.include_pkgs)) if args.apply else 0
    print(json.dumps({"applied": args.apply, "changed": len(changed),
                      "failed": len(failed), "remaining": remaining}))
    if args.log:
        Path(args.log).write_text(json.dumps(
            {"changed": changed, "failed": failed}, indent=1))
    return 1 if failed or remaining else 0


if __name__ == "__main__":
    raise SystemExit(main())
