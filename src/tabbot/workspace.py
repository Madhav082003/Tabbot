"""Per-upload workspace folders and safe unpacking of .twbx files.

Layout:  <workspace_dir>/<id>/{upload, unpacked, parquet, results, logs, thumbnails}
The original upload is copied in; it is never modified. Nothing inside a package is ever executed.
"""

from __future__ import annotations

import shutil
import stat
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

MAX_MEMBERS = 500
MAX_RATIO = 100  # uncompressed / compressed, per member (zip-bomb guard)
WORKBOOK_SUFFIXES = {".twb"}
EXTRACT_SUFFIXES = {".hyper"}
OLD_EXTRACT_SUFFIXES = {".tde"}


class UnsafeArchiveError(ValueError):
    """The .twbx failed a safety check and was not unpacked."""


@dataclass
class Workspace:
    id: str
    root: Path
    upload: Path = field(init=False)
    unpacked: Path = field(init=False)
    parquet: Path = field(init=False)
    results: Path = field(init=False)
    logs: Path = field(init=False)
    thumbnails: Path = field(init=False)

    def __post_init__(self) -> None:
        for name in ("upload", "unpacked", "parquet", "results", "logs", "thumbnails"):
            p = self.root / name
            p.mkdir(parents=True, exist_ok=True)
            setattr(self, name, p)


@dataclass
class Package:
    """What was found inside an upload."""

    workbook: Path                     # the .twb
    extracts: list[Path]               # .hyper files
    unsupported_extracts: list[Path]   # .tde (old format)
    other_files: list[str]             # names only (images, csv, xlsx ...), never opened


def new_workspace(base_dir: Path) -> Workspace:
    wid = uuid.uuid4().hex[:12]
    return Workspace(id=wid, root=Path(base_dir) / wid)


def delete_workspace(ws: Workspace) -> bool:
    """Delete a workspace (the unpacked workbook and its data copies). Returns True if it is gone."""
    if ws.root.exists():
        shutil.rmtree(ws.root, ignore_errors=True)
    return not ws.root.exists()


def purge_workspaces(base_dir: Path, keep: tuple[str, ...] = ("audit", "_demo", "chats", "logs")) -> int:
    """Delete every per-upload workspace under base_dir (left over from earlier runs). Returns how many."""
    base = Path(base_dir)
    if not base.exists():
        return 0
    n = 0
    for child in base.iterdir():
        if child.is_dir() and child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)
            n += not child.exists()
    return n


def _check_member(info: zipfile.ZipInfo, dest: Path) -> Path:
    name = info.filename.replace("\\", "/")
    pp = PurePosixPath(name)
    if name.startswith("/") or pp.is_absolute() or (len(name) > 1 and name[1] == ":"):
        raise UnsafeArchiveError(f"absolute path in archive: {name!r}")
    if ".." in pp.parts:
        raise UnsafeArchiveError(f"path traversal in archive: {name!r}")
    mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_ISLNK(mode):
        raise UnsafeArchiveError(f"symbolic link in archive: {name!r}")
    target = (dest / Path(*pp.parts)).resolve()
    if not target.is_relative_to(dest.resolve()):
        raise UnsafeArchiveError(f"path escapes destination: {name!r}")
    if info.compress_size > 0 and info.file_size / info.compress_size > MAX_RATIO and info.file_size > 10_000_000:
        raise UnsafeArchiveError(f"suspicious compression ratio for {name!r}")
    return target


def safe_extract(zip_path: Path, dest: Path, max_unzipped_mb: int = 2000) -> list[Path]:
    """Extract a zip after validating every member. Raises UnsafeArchiveError before writing anything."""
    dest = Path(dest)
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > MAX_MEMBERS:
            raise UnsafeArchiveError(f"too many members ({len(infos)} > {MAX_MEMBERS})")
        total = sum(i.file_size for i in infos)
        if total > max_unzipped_mb * 1024 * 1024:
            raise UnsafeArchiveError(f"uncompressed size {total // 2**20} MB exceeds {max_unzipped_mb} MB")
        targets = [(i, _check_member(i, dest)) for i in infos]
        written = []
        for info, target in targets:
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            written.append(target)
    return written


def open_upload(src: Path, ws: Workspace, extra_extracts: list[Path] | None = None,
                max_unzipped_mb: int = 2000) -> Package:
    """Copy an uploaded .twbx or .twb into the workspace and locate its parts."""
    src = Path(src)
    suffix = src.suffix.lower()
    copied = ws.upload / src.name
    shutil.copy2(src, copied)

    if suffix == ".twbx":
        if not zipfile.is_zipfile(copied):
            raise UnsafeArchiveError("file is not a valid .twbx (zip) archive")
        files = safe_extract(copied, ws.unpacked, max_unzipped_mb)
    elif suffix == ".twb":
        target = ws.unpacked / src.name
        shutil.copy2(copied, target)
        files = [target]
    else:
        raise ValueError(f"unsupported file type {suffix!r}; upload a .twbx or .twb")

    for e in extra_extracts or []:
        target = ws.unpacked / Path(e).name
        shutil.copy2(e, target)
        files.append(target)

    twbs = [f for f in files if f.suffix.lower() in WORKBOOK_SUFFIXES]
    if not twbs:
        raise ValueError("no .twb workbook found in the upload")
    # a .twbx holds one workbook at its root; prefer the shallowest one
    twbs.sort(key=lambda p: len(p.relative_to(ws.unpacked).parts))
    return Package(
        workbook=twbs[0],
        extracts=[f for f in files if f.suffix.lower() in EXTRACT_SUFFIXES],
        unsupported_extracts=[f for f in files if f.suffix.lower() in OLD_EXTRACT_SUFFIXES],
        other_files=[str(f.relative_to(ws.unpacked)) for f in files
                     if f.suffix.lower() not in WORKBOOK_SUFFIXES | EXTRACT_SUFFIXES | OLD_EXTRACT_SUFFIXES],
    )
