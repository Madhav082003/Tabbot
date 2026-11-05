"""Build a local corpus of public Tableau workbooks for pattern mining and parser tests.

For each repository in corpus_sources.yaml:
  1. partial-clone it (no file contents), list files ending in .twb / .twbx,
  2. download only those files,
  3. copy each workbook into <out>/workbooks/, extract the .twb XML (and any .hyper) from .twbx safely,
  4. write <out>/manifest.json with source URL, domain, sizes, Tableau version and package contents.

Usage:
    python tools/fetch_corpus.py --out corpus --cache .corpus_cache
    python tools/fetch_corpus.py --out corpus --add-local path/to/my_workbook.twbx   # add private files too

Workbooks keep their original licences. Do not commit the corpus folder (it is git-ignored).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import yaml

MAX_FILE_BYTES = 80_000_000          # skip workbooks larger than this
MAX_MEMBER_BYTES = 500_000_000       # per extracted member
MAX_RATIO = 200                      # compression ratio guard (zip bomb)

HEALTH_WORDS = r"health|hospital|patient|clinic|clinical|trial|covid|mortality|nhs|care|medic|emergency|drug|pharma|disease"
SALES_WORDS = r"sales|superstore|store|retail|amazon|walmart|revenue|order|megastore|customer"


def guess_domain(path: str, repo_domain: str) -> str:
    low = path.lower()
    if re.search(HEALTH_WORDS, low):
        return "health"
    if re.search(SALES_WORDS, low):
        return "sales"
    return repo_domain if repo_domain not in ("mixed",) else "other"


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def fetch_repo(repo: str, cache: Path) -> tuple[Path | None, list[str]]:
    """Partial clone + checkout of workbook files only. Returns (repo_dir, workbook paths)."""
    dest = cache / repo.replace("/", "__")
    if not dest.exists():
        r = run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--no-checkout",
                 f"https://github.com/{repo}.git", str(dest)], timeout=180)
        if r.returncode != 0:
            print(f"  ! clone failed: {repo}: {r.stderr.strip()[:200]}", file=sys.stderr)
            return None, []
    # names only: `ls-tree -l` needs every blob's size, which in a blob-less partial clone downloads every file
    ls = run(["git", "-C", str(dest), "ls-tree", "-r", "--name-only", "HEAD"])
    files = [p for p in ls.stdout.splitlines() if re.search(r"\.twbx?$", p, re.I)]
    missing = [f for f in files if not (dest / f).exists()]
    if missing:
        r = run(["git", "-C", str(dest), "checkout", "-q", "HEAD", "--", *missing], timeout=600)
        if r.returncode != 0:
            print(f"  ! checkout failed: {repo}: {r.stderr.strip()[:300]}", file=sys.stderr)
    return dest, [f for f in files if (dest / f).exists() and 0 < (dest / f).stat().st_size <= MAX_FILE_BYTES]


def safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Reject absolute paths, parent-directory escapes, huge members and zip bombs."""
    ok = []
    for info in zf.infolist():
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in Path(name).parts or re.match(r"^[A-Za-z]:", name):
            raise ValueError(f"unsafe path in zip: {name!r}")
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(f"member too large: {name!r}")
        if info.compress_size and info.file_size / info.compress_size > MAX_RATIO:
            raise ValueError(f"suspicious compression ratio: {name!r}")
        ok.append(info)
    return ok


def tableau_version(xml_head: bytes) -> dict:
    head = xml_head.decode("utf-8", "replace")
    out = {}
    for attr in ("version", "source-build", "source-platform", "original-version"):
        m = re.search(rf"<workbook[^>]*\s{attr}='([^']*)'", head) or re.search(rf'<workbook[^>]*\s{attr}="([^"]*)"', head)
        if m:
            out[attr] = m.group(1)
    return out


def add_workbook(src: Path, wid: str, out: Path, meta: dict) -> dict:
    wb_dir, xml_dir, data_dir = out / "workbooks", out / "xml", out / "data"
    for d in (wb_dir, xml_dir, data_dir):
        d.mkdir(parents=True, exist_ok=True)
    dst = wb_dir / f"{wid}{src.suffix.lower()}"
    shutil.copy2(src, dst)
    raw = src.read_bytes()
    entry = {**meta, "id": wid, "file": dst.name, "bytes": len(raw),
             "sha256": hashlib.sha256(raw).hexdigest(), "package": None, "error": None}
    try:
        if src.suffix.lower() == ".twbx":
            with zipfile.ZipFile(src) as zf:
                members = safe_members(zf)
                entry["package"] = [{"name": m.filename, "bytes": m.file_size} for m in members]
                twbs = [m for m in members if m.filename.lower().endswith(".twb")]
                if not twbs:
                    raise ValueError("no .twb inside .twbx")
                xml = zf.read(twbs[0])
                (xml_dir / f"{wid}.twb").write_bytes(xml)
                for m in members:
                    if m.filename.lower().endswith(".hyper"):
                        target = data_dir / wid
                        target.mkdir(exist_ok=True)
                        (target / Path(m.filename).name).write_bytes(zf.read(m))
        else:
            xml = raw
            (xml_dir / f"{wid}.twb").write_bytes(xml)
        entry["tableau"] = tableau_version(xml[:4000])
        entry["twb_bytes"] = len(xml)
    except Exception as exc:  # keep going; the manifest records the failure
        entry["error"] = f"{type(exc).__name__}: {exc}"
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default=str(Path(__file__).with_name("corpus_sources.yaml")))
    ap.add_argument("--out", default="corpus")
    ap.add_argument("--cache", default=".corpus_cache")
    ap.add_argument("--add-local", nargs="*", default=[], help="extra local workbooks (kept private)")
    args = ap.parse_args()

    out, cache = Path(args.out), Path(args.cache)
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    sources = yaml.safe_load(Path(args.sources).read_text())["repos"]

    manifest, n = [], 0
    for src in sources:
        repo, repo_domain = src["repo"], src.get("domain", "other")
        print(f"- {repo}")
        repo_dir, paths = fetch_repo(repo, cache)
        for p in sorted(paths):
            n += 1
            wid = f"w{n:03d}"
            meta = {"source": "github", "repo": repo, "path": p,
                    "url": f"https://github.com/{repo}/blob/HEAD/{p}",
                    "domain": guess_domain(p, repo_domain)}
            manifest.append(add_workbook(repo_dir / p, wid, out, meta))
            print(f"    {wid} {p}")
    for local in args.add_local:
        n += 1
        wid = f"p{n:03d}"
        manifest.append(add_workbook(Path(local), wid, out, {"source": "local", "path": Path(local).name,
                                                               "domain": "private"}))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    ok = sum(1 for m in manifest if not m["error"])
    print(f"\n{ok}/{len(manifest)} workbooks ready in {out}/ (xml/ holds the extracted .twb files)")


if __name__ == "__main__":
    main()
