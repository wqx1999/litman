#!/usr/bin/env python3
"""Fetch the litman-bench fixture PDFs listed in manifest.yaml.

Fully automated, idempotent, sha256-verified. No manual steps for the
arXiv / bioRxiv sources used here (ChemRxiv was deliberately excluded because
its Cloudflare JS challenge cannot be cleared by a headless downloader).

Run inside the `litman` conda env (uses ruamel.yaml; only the stdlib otherwise).

Usage:
    python fetch_fixtures.py               # download missing PDFs, verify against fixtures.lock
    python fetch_fixtures.py --write-lock  # download all, (re)compute and WRITE fixtures.lock
    python fetch_fixtures.py --check       # verify already-downloaded PDFs against the lock (no network)
    python fetch_fixtures.py --dest DIR    # override download directory

First-time pinning: run `--write-lock` once on a machine with real network/browser
access, then commit the generated fixtures.lock. Every later run verifies bytes
against those pins, so a wrong URL / silent bot-block page can never slip through.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from ruamel.yaml import YAML  # litman's YAML lib (PyYAML is not in the env)

BENCH_DIR = Path(__file__).resolve().parent
MANIFEST = BENCH_DIR / "manifest.yaml"
LOCK = BENCH_DIR / "fixtures.lock"
DEFAULT_DEST = BENCH_DIR / "fixtures" / "pdfs"

# A real browser UA: arXiv is permissive, but bioRxiv 403s default/library UAs.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

RETRIES = 3
TIMEOUT = 60          # seconds per request
POLITE_DELAY = 1.5    # seconds between downloads, to stay friendly to arXiv/bioRxiv


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest() -> list[dict]:
    yaml = YAML(typ="safe")
    with MANIFEST.open(encoding="utf-8") as f:
        return yaml.load(f)["papers"]


def load_lock() -> dict[str, str]:
    """Read fixtures.lock (sha256sum format '<hash>  <name>') -> {name: hash}."""
    pins: dict[str, str] = {}
    if LOCK.exists():
        for line in LOCK.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            h, _, name = line.partition("  ")
            pins[name.strip()] = h.strip()
    return pins


def download(url: str) -> bytes:
    """GET url with a browser UA; retry with backoff; reject non-PDF bodies."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/pdf,*/*"}
    )
    last_err: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                data = resp.read()
            if data[:5] != b"%PDF-":
                raise ValueError(
                    f"got {len(data)} bytes but no %PDF- header — "
                    "likely a bot-block / HTML error page, not the PDF"
                )
            return data
        except (urllib.error.HTTPError, urllib.error.URLError,
                ValueError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            print(f"    attempt {attempt}/{RETRIES} failed: {e} "
                  f"(retry in {wait}s)", file=sys.stderr)
            time.sleep(wait)
    raise RuntimeError(f"failed to download {url}: {last_err}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch litman-bench fixture PDFs.")
    ap.add_argument("--write-lock", action="store_true",
                    help="(re)compute and write fixtures.lock")
    ap.add_argument("--check", action="store_true",
                    help="verify existing files against the lock; no network")
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                    help="download directory (default: ./fixtures/pdfs)")
    args = ap.parse_args()

    papers = load_manifest()
    pins = load_lock()
    dest: Path = args.dest
    dest.mkdir(parents=True, exist_ok=True)

    computed: dict[str, str] = {}
    failures: list[str] = []

    for p in papers:
        name = f"{p['id']}.pdf"
        target = dest / name
        pinned = pins.get(name)

        # --- verify-only mode (no network) ---
        if args.check:
            if not target.exists():
                print(f"✗ {name}: missing")
                failures.append(name)
            elif pinned and sha256_of(target.read_bytes()) != pinned:
                print(f"✗ {name}: sha256 mismatch")
                failures.append(name)
            elif not pinned:
                print(f"? {name}: present but no pin in fixtures.lock")
            else:
                print(f"✓ {name}: verified ({p['short']})")
            continue

        # --- already cached & pinned-matching: skip ---
        if target.exists() and pinned and sha256_of(target.read_bytes()) == pinned:
            print(f"✓ {name}: cached, verified ({p['short']})")
            computed[name] = pinned
            continue

        # --- present, unpinned, and not (re)building the lock: accept as-is ---
        if target.exists() and pinned is None and not args.write_lock:
            h = sha256_of(target.read_bytes())
            print(f"· {name}: present, not yet pinned (sha256={h[:12]}…)")
            computed[name] = h
            continue

        # --- download ---
        print(f"↓ {name}: {p['short']}  <-  {p['url']}")
        try:
            data = download(p["url"])
        except RuntimeError as e:
            print(f"✗ {name}: {e}", file=sys.stderr)
            failures.append(name)
            continue
        h = sha256_of(data)
        if pinned and h != pinned and not args.write_lock:
            print(f"✗ {name}: sha256 mismatch "
                  f"(pinned {pinned[:12]}… got {h[:12]}…)", file=sys.stderr)
            failures.append(name)
            continue
        target.write_bytes(data)
        computed[name] = h
        print(f"  saved {len(data):,} bytes  sha256={h[:12]}…")
        time.sleep(POLITE_DELAY)

    # --- write the lockfile ---
    if args.write_lock:
        if failures:
            print("\nnot writing fixtures.lock — some downloads failed.",
                  file=sys.stderr)
        else:
            lines = [
                "# litman-bench fixture checksums (sha256sum format). Commit this file.",
                "# regenerate with: python fetch_fixtures.py --write-lock",
            ]
            for p in papers:
                name = f"{p['id']}.pdf"
                if name in computed:
                    lines.append(f"{computed[name]}  {name}")
            LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
            print(f"\nwrote {LOCK} ({len(computed)} entries)")

    if failures:
        print(f"\n{len(failures)} failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"\nOK — {len(computed)}/{len(papers)} fixtures present in {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
