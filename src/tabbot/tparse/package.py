"""Opening workbook packages safely.

`.twbx` files are zips written by other people: treat them as untrusted. We never extract to arbitrary paths,
we cap sizes, and we refuse zip-slip names and zip bombs. The `.twb` is read into memory; extracts are copied
into a workspace folder only when asked for.
"""
from __future__ import annotations

import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_MEMBERS = 500
MAX_MEMBER_BYTES = 2_000_000_000
MAX_TOTAL_BYTES = 4_000_000_000
MAX_RATIO = 200


class UnsafePackage(ValueError):
    pass


@dataclass
class PackageInfo:
    path: Path
    is_packaged: bool
    twb_name: str | None = None
    members: list[tuple[str, int]] = field(default_factory=list)    # (name, uncompressed bytes)

    @property
    def extracts(self) -> list[str]:
        return [n for n, _ in self.members if n.lower().endswith((".hyper", ".tde"))]

    @property
    def images(self) -> list[str]:
        return [n for n, _ in self.members if n.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg"))]


def _check(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise UnsafePackage(f"too many members: {len(infos)}")
    total = 0
    for info in infos:
        name = info.filename
        if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name) or ".." in Path(name).parts:
            raise UnsafePackage(f"unsafe path in package: {name!r}")
        if info.external_attr >> 16 & 0o170000 == 0o120000:
            raise UnsafePackage(f"symlink in package: {name!r}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise UnsafePackage(f"member too large: {name!r}")
        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO and info.file_size > 50_000_000:
            raise UnsafePackage(f"suspicious compression ratio: {name!r}")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise UnsafePackage("package too large when unpacked")
    return infos


def read_workbook(path: str | Path) -> tuple[bytes, PackageInfo]:
    """Return the .twb XML bytes and package info for a .twb or .twbx path."""
    p = Path(path)
    if p.suffix.lower() == ".twb":
        return p.read_bytes(), PackageInfo(path=p, is_packaged=False, twb_name=p.name)
    if not zipfile.is_zipfile(p):
        raise UnsafePackage(f"not a .twb and not a zip package: {p.name}")
    with zipfile.ZipFile(p) as zf:
        infos = _check(zf)
        twbs = [i for i in infos if i.filename.lower().endswith(".twb")]
        if not twbs:
            raise UnsafePackage("no .twb inside the package")
        # the workbook is the .twb at the shallowest level (packages can carry .twb copies of data sources)
        twb = sorted(twbs, key=lambda i: (i.filename.count("/"), i.filename))[0]
        return zf.read(twb), PackageInfo(path=p, is_packaged=True, twb_name=twb.filename,
                                         members=[(i.filename, i.file_size) for i in infos])


def new_workspace(root: str | Path = "workspace") -> Path:
    ws = Path(root) / uuid.uuid4().hex[:12]
    for sub in ("upload", "extracts", "parquet", "results", "audit"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    return ws


def extract_data_files(package: PackageInfo, workspace: Path) -> list[Path]:
    """Copy .hyper extracts out of a .twbx into <workspace>/extracts/, flattening names safely."""
    out: list[Path] = []
    if not package.is_packaged:
        return out
    with zipfile.ZipFile(package.path) as zf:
        _check(zf)
        for name in package.extracts:
            if not name.lower().endswith(".hyper"):
                continue
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
            target = workspace / "extracts" / safe
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            out.append(target)
    return out


def delete_workspace(ws: Path) -> None:
    shutil.rmtree(ws, ignore_errors=True)
