#!/usr/bin/env python3
"""Copy folders from an iPhone to a local directory over USB, with a progress bar.

Why not copy from "This PC\\Apple iPhone\\Internal Storage"?
That location is a Windows shell namespace, not a real path, so Python, robocopy
and xcopy cannot read it. This script talks to the phone directly over USB using
Apple's AFC protocol (via pymobiledevice3). It is faster than Explorer, resumable,
and works on Windows, macOS and Linux.

Setup (once):
    pip install pymobiledevice3 tqdm
    Windows only: install "Apple Devices" (Microsoft Store) or iTunes, which
    provides the Apple Mobile Device USB driver.
    Unlock the iPhone and tap "Trust This Computer".

Usage:
    python iphone_pull.py --list                         # see what folders the phone exposes
    python iphone_pull.py --dst D:\\iphone                # copy /DCIM to D:\\iphone
    python iphone_pull.py --src /DCIM/100APPLE --dst ~/pics
    python iphone_pull.py --src / --dst E:\\backup --workers 6
    python iphone_pull.py --dst D:\\iphone --dry-run       # scan only, copy nothing
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import logging
import os
import posixpath
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from tqdm import tqdm

log = logging.getLogger("iphone_pull")

CHUNK = 4 * 1024 * 1024  # AFC's max read size per request
DEFAULT_SRC = "/DCIM"  # the camera roll, shown by Windows as "Internal Storage"


@dataclass(frozen=True)
class Entry:
    remote: str
    rel: str
    size: int
    mtime: float


async def call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a pymobiledevice3 method whether this version is sync or async."""
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def lp(path: Path) -> str:
    """Long-path-safe string for Windows (avoids the 260 character limit)."""
    s = str(path.absolute())
    if os.name != "nt" or s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def to_ts(value: Any) -> float:
    return value.timestamp() if isinstance(value, datetime) else float(value)


# --------------------------------------------------------------------------- device


async def connect(udid: str | None) -> Any:
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.afc import AfcService

    lockdown = await call(create_using_usbmux, serial=udid)
    afc = AfcService(lockdown)
    if hasattr(afc, "__aenter__"):
        await afc.__aenter__()
    return afc


async def disconnect(afc: Any) -> None:
    closer = getattr(afc, "aclose", None) or getattr(afc, "close", None)
    if closer:
        with contextlib.suppress(Exception):
            await call(closer)


# --------------------------------------------------------------------------- list


async def list_dir(afc: Any, path: str) -> int:
    """Print the entries of one folder on the phone so users can pick a --src."""
    try:
        names = await call(afc.listdir, path)
    except Exception as exc:
        print(f"Cannot list {path}: {exc!r}", file=sys.stderr)
        return 1
    print(f"{path}")
    for name in sorted(names):
        full = posixpath.join(path, name)
        try:
            kind = (await call(afc.stat, full)).get("st_ifmt")
        except Exception:
            kind = None
        mark = "dir " if kind == "S_IFDIR" else "file" if kind == "S_IFREG" else "?   "
        print(f"  {mark}  {full}")
    return 0


# --------------------------------------------------------------------------- scan


async def scan(afc: Any, root: str) -> tuple[list[Entry], list[str]]:
    """Walk the device tree with many stat calls in flight at once."""
    files: list[Entry] = []
    dirs: list[str] = []
    sem = asyncio.Semaphore(32)
    bar = tqdm(desc="Scanning", unit=" files", leave=False)

    async def stat(path: str) -> Any:
        async with sem:
            return await call(afc.stat, path)

    async def visit(remote_dir: str, rel_dir: str) -> None:
        try:
            names = await call(afc.listdir, remote_dir)
        except Exception as exc:
            log.warning("cannot list %s: %s", remote_dir, exc)
            return
        remotes = [posixpath.join(remote_dir, n) for n in names]
        infos = await asyncio.gather(*(stat(r) for r in remotes), return_exceptions=True)
        subdirs = []
        for name, remote, info in zip(names, remotes, infos):
            rel = posixpath.join(rel_dir, name) if rel_dir else name
            if isinstance(info, BaseException):
                log.warning("cannot stat %s: %s", remote, info)
                continue
            kind = info.get("st_ifmt")
            if kind == "S_IFDIR":
                dirs.append(rel)
                subdirs.append(visit(remote, rel))
            elif kind == "S_IFREG":
                files.append(Entry(remote, rel, int(info["st_size"]), to_ts(info["st_mtime"])))
                bar.update(1)
            else:
                log.debug("skipping %s (%s)", remote, kind)
        await asyncio.gather(*subdirs)

    try:
        await visit(root, "")
    finally:
        bar.close()
    return files, dirs


# --------------------------------------------------------------------------- copy


def already_copied(dst: Path, size: int) -> bool:
    try:
        return os.stat(lp(dst)).st_size == size
    except OSError:
        return False


async def download(afc: Any, entry: Entry, dst_root: Path, bar: tqdm) -> None:
    dst = dst_root / entry.rel
    part = dst.with_name(dst.name + ".part")
    got = 0
    ok = False
    pending: asyncio.Future | None = None
    handle = None
    try:
        handle = await call(afc.fopen, entry.remote)
        with open(lp(part), "wb") as f:
            remaining = entry.size
            while remaining > 0:
                data = await call(afc.fread, handle, min(CHUNK, remaining))
                if not data:
                    raise IOError("device returned no data")
                if pending is not None:  # overlap the disk write with the next USB read
                    await pending
                pending = asyncio.ensure_future(asyncio.to_thread(f.write, data))
                remaining -= len(data)
                got += len(data)
                bar.update(len(data))
            if pending is not None:
                await pending
                pending = None
        os.utime(lp(part), (entry.mtime, entry.mtime))
        os.replace(lp(part), lp(dst))
        ok = True
    finally:
        if pending is not None:
            with contextlib.suppress(BaseException):
                await pending
        if handle is not None:
            with contextlib.suppress(BaseException):
                await call(afc.fclose, handle)
        if not ok:
            bar.update(-got)  # roll the bar back so retries are not double counted
            with contextlib.suppress(OSError):
                os.remove(lp(part))


async def worker(
    queue: asyncio.Queue, afc: Any, dst_root: Path, bar: tqdm, failures: list[str], retries: int, total: int, done: list[int]
) -> None:
    while True:
        try:
            entry = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        for attempt in range(1, retries + 1):
            try:
                await download(afc, entry, dst_root, bar)
                break
            except Exception as exc:
                log.debug("attempt %d failed for %s: %r", attempt, entry.rel, exc)
                if attempt == retries:
                    failures.append(f"{entry.rel}: {exc!r}")
                else:
                    await asyncio.sleep(attempt)
        done[0] += 1
        bar.set_postfix_str(f"{done[0]}/{total} files", refresh=False)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


async def transfer(
    afc: Any, src: str, dst_root: Path, *, workers: int, overwrite: bool, dry_run: bool, retries: int
) -> int:
    files, dirs = await scan(afc, src)
    if overwrite:
        todo, skipped = files, 0
    else:
        todo = [e for e in files if not already_copied(dst_root / e.rel, e.size)]
        skipped = len(files) - len(todo)
    total = sum(e.size for e in todo)
    print(f"Found {len(files)} files. To copy: {len(todo)} ({human(total)}), already present: {skipped}.")
    if dry_run or not todo:
        return 0

    dst_root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dst_root).free
    if total > free:
        print(f"Not enough space in {dst_root}: need {human(total)}, free {human(free)}.", file=sys.stderr)
        return 1
    for d in dirs:
        os.makedirs(lp(dst_root / d), exist_ok=True)

    todo.sort(key=lambda e: e.size, reverse=True)  # big files first, small ones fill the gaps
    queue: asyncio.Queue = asyncio.Queue()
    for e in todo:
        queue.put_nowait(e)

    failures: list[str] = []
    done = [0]
    start = time.perf_counter()
    bar = tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024, desc="Copying", smoothing=0.1)
    try:
        await asyncio.gather(
            *(worker(queue, afc, dst_root, bar, failures, retries, len(todo), done) for _ in range(workers))
        )
    finally:
        copied = bar.n
        bar.close()

    elapsed = max(time.perf_counter() - start, 1e-9)
    print(f"Done in {elapsed:.1f}s ({human(copied / elapsed)}/s). Failed: {len(failures)}.")
    for line in failures[:20]:
        print(f"  FAILED {line}", file=sys.stderr)
    if len(failures) > 20:
        print(f"  ... and {len(failures) - 20} more (rerun with -v)", file=sys.stderr)
    return 1 if failures else 0


# --------------------------------------------------------------------------- cli


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Copy a folder from an iPhone to a local directory over USB.",
        epilog="Not sure what to pass to --src? Run with --list (and optionally --src /DCIM) to browse the phone.",
    )
    p.add_argument("-s", "--src", help=f"folder on the phone (default {DEFAULT_SRC}, or / with --list). Use / to copy everything")
    p.add_argument("-d", "--dst", type=Path, help="local folder to copy into (created if missing). Required unless --list")
    p.add_argument("--list", action="store_true", help="list the contents of --src on the phone and exit")
    p.add_argument("--workers", type=int, default=4, help="parallel transfers (default 4)")
    p.add_argument("--retries", type=int, default=3, help="attempts per file (default 3)")
    p.add_argument("--udid", help="pick a device when several are connected")
    p.add_argument("--overwrite", action="store_true", help="re-copy files even if the size already matches")
    p.add_argument("--dry-run", action="store_true", help="scan and report, copy nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    if not args.list and args.dst is None:
        p.error("--dst is required (where to copy to). Use --list to browse the phone instead.")
    if args.dst is not None:
        args.dst = args.dst.expanduser()
    src = args.src if args.src is not None else ("/" if args.list else DEFAULT_SRC)
    args.src = "/" + src.replace("\\", "/").strip("/")
    args.workers = max(1, args.workers)
    return args


async def run(args: argparse.Namespace) -> int:
    try:
        afc = await connect(args.udid)
    except Exception as exc:
        print(
            f"Could not connect to the iPhone: {exc!r}\n"
            "Check: cable plugged in, phone unlocked, 'Trust This Computer' accepted, "
            "and on Windows the Apple Devices app or iTunes is installed.",
            file=sys.stderr,
        )
        return 2
    try:
        if args.list:
            return await list_dir(afc, args.src)
        return await transfer(
            afc,
            args.src,
            args.dst,
            workers=args.workers,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            retries=args.retries,
        )
    finally:
        await disconnect(afc)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Partial files were removed. Rerun to resume.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
