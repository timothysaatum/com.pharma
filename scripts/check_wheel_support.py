#!/usr/bin/env python3
"""Check every pinned requirement in requirements.txt for installable wheels.

Deterministic, no guessing. For each pinned version this queries the PyPI JSON
API and classifies installability per interpreter/platform using real wheel tag
semantics:

  * platform tags are normalized so manylinux_2_17_x86_64, manylinux2014_x86_64
    and friends all collapse to linux_x86_64
  * py3-none-any / py2.py3-none-any wheels count as universal
  * a version is only BLOCKED when there is no usable wheel at all (sdist-only)
    for the target interpreter

Usage:
  python scripts/check_wheel_support.py backend.laso/requirements.txt [cp312|cp313|cp314]
"""
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

REQ = sys.argv[1] if len(sys.argv) > 1 else "backend.laso/requirements.txt"
PYTAG = sys.argv[2] if len(sys.argv) > 2 else "cp312"

TARGETS = {
    "linux_x86_64": "Linux x86_64 (WSL/native)",
    "win_amd64": "Windows amd64",
    "any": "universal (py3-none-any)",
}


def normalize_platform(plats):
    """Collapse wheel platform tags onto the set of targets we care about.

    Platform tags are dot-separated and must be matched as whole segments.
    A substring test is wrong here: "any" is contained in "manylinux_2_17_x86_64",
    which silently classifies every Linux wheel as universal.
    """
    out = set()
    for p in plats:
        for tag in p.lower().split("."):
            if tag == "any":
                out.add("any")
            elif "linux" in tag and "x86_64" in tag and "musllinux" not in tag:
                out.add("linux_x86_64")
            elif "musllinux" in tag and "x86_64" in tag:
                out.add("musllinux_x86_64")
            elif "win" in tag and "amd64" in tag:
                out.add("win_amd64")
    return out


def parse_wheel(filename):
    """Return (python_tags, abi_tags, platforms) or None for non-wheels."""
    if not filename.endswith(".whl"):
        return None
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        return None
    return set(parts[2].split(".")), set(parts[3].split(".")), parts[4:]


def parse_pins(path):
    pins = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            m = re.match(r"^([A-Za-z0-9._-]+)\s*==\s*([A-Za-z0-9._+!-]+)$", line)
            pins.append((m.group(1), m.group(2)) if m else (line, None))
    return pins


def _ver(tag):
    m = re.match(r"^cp(\d+)", tag)
    return int(m.group(1)) if m else None


def tag_compatible(py_tags, abi_tags, target):
    """PEP 425 compatibility for a wheel's python/abi tag sets.

    Handles the cases a naive exact match gets wrong:
      * py3 / py2.py3            -> pure python, any interpreter
      * cp312-cp312              -> exact interpreter match
      * cp39-abi3 (stable abi3)  -> forward compatible with any CPython >= 3.9
      * cp312-abi3               -> abi3 built for this interpreter
    """
    tgt = _ver(target)
    for py in py_tags:
        for abi in abi_tags:
            if py.startswith("py3") or py in ("py2.py3", "py2.py3.py4"):
                if abi in ("none", "abi3"):
                    return True
            if abi.startswith("cp") and abi == target and py == target:
                return True
            if abi == "abi3" and py.startswith("cp") and tgt is not None:
                built = _ver(py)
                if built is not None and built <= tgt:
                    return True
            if abi == "none" and py == target:
                return True
    return False


def check(pin):
    name, version = pin
    url = (
        f"https://pypi.org/pypi/{name}/{version}/json"
        if version
        else f"https://pypi.org/pypi/{name}/json"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wheel-check/1.0"})
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "version": version, "error": str(exc)[:70]}

    urls = data.get("urls", [])
    hit = {"linux_x86_64": None, "win_amd64": None, "any": None}
    for entry in urls:
        parsed = parse_wheel(entry.get("filename", ""))
        if not parsed:
            continue
        py_tags, abi_tags, plats = parsed
        plats_n = normalize_platform(plats)
        for plat in plats_n & set(hit):
            if plat == "any" or tag_compatible(py_tags, abi_tags, PYTAG):
                if hit[plat] is None:
                    hit[plat] = entry["filename"]
    return {
        "name": name,
        "version": version,
        "requires_python": data.get("info", {}).get("requires_python") or "",
        "sdist": any(not u.get("filename", "").endswith(".whl") for u in urls),
        "hits": hit,
    }


def main():
    pins = parse_pins(REQ)
    print(f"{len(pins)} pinned requirements from {REQ}   (interpreter tag: {PYTAG})\n")
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(check, pins))

    errors = [r for r in results if r.get("error")]
    rows = [r for r in results if not r.get("error")]

    linux_blocked, win_blocked, linux_only = [], [], []
    for r in rows:
        h = r["hits"]
        if not h["any"] and not h["linux_x86_64"]:
            linux_blocked.append(r)
        elif not h["any"] and not h["win_amd64"]:
            linux_only.append(r)
        if not h["any"] and not h["win_amd64"]:
            win_blocked.append(r)

    width = max(len(r["name"]) for r in rows) + 2

    def show(title, items):
        if not items:
            return
        print(f"=== {title} ({len(items)}) ===")
        for r in items:
            h = r["hits"]
            print(f"  {r['name'].ljust(width)}{r['version']:<14}requires_python={r['requires_python'] or 'n/a'}")
            print(f"  {' ' * width}linux:{h['linux_x86_64'] or 'NONE'}  win:{h['win_amd64'] or 'NONE'}")
        print()

    show("BLOCKED on Linux (needs compiling from source)", linux_blocked)
    show("BLOCKED on Windows (needs compiling from source)", linux_only)

    print("=== SUMMARY ===")
    print(f"total pinned             : {len(rows)}")
    print(f"install cleanly on Linux : {len(rows) - len(linux_blocked)}")
    print(f"install cleanly on Win   : {len(rows) - len(linux_only)}")
    if errors:
        print(f"\n=== LOOKUP ERRORS ({len(errors)}) ===")
        for r in errors:
            print(f"  {r['name']}=={r['version']}: {r['error']}")


if __name__ == "__main__":
    main()
