#!/usr/bin/env python3
"""Package every extension and write the market index (persona-extensions.json).

    python3 -I scripts/package.py            build missing packages, refresh the index
    python3 -I scripts/package.py --check    fail when a package or the index is not what the sources make (CI)

Each extension becomes packages/<id>/<version>/<id>-<version>.tar.gz: its files plus the shared core
(core/openmate_core), both manifest names (persona-extension.json and the legacy persona-plugin.json) without the
source-only tags and icon. The archive is reproducible: sorted entries, fixed times and owners, modes 0644 / 0755,
gzip without a name or time. A published version is never rewritten: when the sources change, bump the version.
The comparison is made on the uncompressed tar, so another zlib's different (but equivalent) compression is not a
change.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import stat
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "core", "openmate_core")
INDEX = "persona-extensions.json"
MTIME = 1767225600  # 2026-01-01T00:00:00Z: every entry's time
MARKET = {
    "market_version": 1,
    "name": "OpenMate",
    "description": "OpenMate：把飞书等聊天软件接进 Persona 的开源扩展（MIT）。",
    "homepage": "https://github.com/NginxL/openmate",
}
SHIPPED_DIRS = ("bin", "assistant", "assets", "l10n")
SKIP_NAMES = {"__pycache__", ".DS_Store", "tests"}


class PackageError(Exception):
    pass


def load_manifest(ext_dir):
    with open(os.path.join(ext_dir, "persona-extension.json"), encoding="utf-8") as f:
        return json.load(f)


def packaged(manifest):
    return {k: v for k, v in manifest.items() if k not in ("tags", "icon")}


def manifest_bytes(manifest):
    return (json.dumps(packaged(manifest), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _walk(base, prefix):
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_NAMES and not d.startswith("."))
        for name in sorted(filenames):
            if name in SKIP_NAMES or name.startswith(".") or name.endswith((".pyc", ".pyo")):
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                raise PackageError("%s is a link: packages hold regular files only" % path)
            rel = os.path.relpath(path, base).replace(os.sep, "/")
            out.append((prefix + rel, path))
    return out


def entries(ext_dir, manifest):
    """[(name in the archive, bytes, mode)], sorted by name."""
    ext_id = manifest["id"]
    files = {}
    mb = manifest_bytes(manifest)
    files["persona-extension.json"] = (mb, 0o644)
    files["persona-plugin.json"] = (mb, 0o644)
    with open(os.path.join(ROOT, "LICENSE"), "rb") as f:
        files["LICENSE"] = (f.read(), 0o644)
    sources = []
    for d in SHIPPED_DIRS:
        if os.path.isdir(os.path.join(ext_dir, d)):
            sources += _walk(os.path.join(ext_dir, d), d + "/")
    py_pkgs = [d for d in sorted(os.listdir(ext_dir)) if os.path.isfile(os.path.join(ext_dir, d, "__init__.py"))]
    if not py_pkgs:
        raise PackageError("%s has no Python package" % ext_id)
    for d in py_pkgs:
        sources += _walk(os.path.join(ext_dir, d), d + "/")
    sources += _walk(CORE, "openmate_core/")
    command = (manifest.get("runtime") or {}).get("command")
    for name, path in sources:
        with open(path, "rb") as f:
            data = f.read()
        executable = name == command or (name.startswith("bin/") and bool(os.stat(path).st_mode & stat.S_IXUSR))
        files[name] = (data, 0o755 if executable else 0o644)
    if command and command not in files:
        raise PackageError("%s: runtime.command %s is not in the package" % (ext_id, command))
    for ref in [manifest.get("icon")] + [a.get("instructions") for a in (manifest.get("contributes") or {}).get("assistant", [])]:
        if ref and ref not in files:
            raise PackageError("%s: %s is not in the package" % (ext_id, ref))
    return [(n,) + files[n] for n in sorted(files)]


def build_tar(items):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data, mode in items:
            if len(name) >= 100:
                raise PackageError("%s: path too long for a plain tar header" % name)
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = MTIME
            ti.mode = mode
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.type = tarfile.REGTYPE
            tar.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def gzip_bytes(raw):
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as gz:
        gz.write(raw)
    return buf.getvalue()


def extension_dirs():
    base = os.path.join(ROOT, "extensions")
    return [os.path.join(base, d) for d in sorted(os.listdir(base))
            if os.path.isfile(os.path.join(base, d, "persona-extension.json"))]


def run(out_root, check=False, allow_replace=False, log=print):
    problems = []
    extensions = []
    for ext_dir in extension_dirs():
        manifest = load_manifest(ext_dir)
        ext_id, version = manifest["id"], manifest["version"]
        if os.path.basename(ext_dir) != ext_id:
            raise PackageError("extensions/%s holds %s: the directory is the id" % (os.path.basename(ext_dir), ext_id))
        raw = build_tar(entries(ext_dir, manifest))
        rel = "packages/%s/%s/%s-%s.tar.gz" % (ext_id, version, ext_id, version)
        path = os.path.join(out_root, rel)
        archive = None
        if os.path.exists(path):
            with open(path, "rb") as f:
                existing = f.read()
            try:
                same = gzip.decompress(existing) == raw
            except (OSError, EOFError):
                same = False
            if same:
                archive = existing
            elif check:
                problems.append("%s differs from the sources: bump the version (published packages never change)" % rel)
            elif not allow_replace:
                raise PackageError("%s exists with other content: bump %s's version (or --allow-replace before it is published)" % (rel, ext_id))
        elif check:
            problems.append("%s is missing: run scripts/package.py" % rel)
        if archive is None:
            archive = gzip_bytes(raw)
            if not check:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(archive)
                log("wrote %s" % rel)
        entry = {"manifest": packaged(manifest)}
        if manifest.get("tags"):
            entry["tags"] = manifest["tags"]
        if manifest.get("icon"):
            entry["icon"] = manifest["icon"]
        entry["packages"] = [{"url": rel, "sha256": hashlib.sha256(archive).hexdigest(), "size": len(archive)}]
        extensions.append(entry)
    index = dict(MARKET)
    index["extensions"] = extensions
    text = json.dumps(index, ensure_ascii=False, indent=2) + "\n"
    index_path = os.path.join(out_root, INDEX)
    current = None
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            current = f.read()
    if check:
        if current != text:
            problems.append("%s is not up to date: run scripts/package.py" % INDEX)
    elif current != text:
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(text)
        log("wrote %s" % INDEX)
    return problems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="only check that packages and the index are up to date")
    ap.add_argument("--allow-replace", action="store_true", help="rewrite a package of an unpublished version")
    ap.add_argument("--out", default=ROOT, help="where packages/ and the index go (default: the repository)")
    args = ap.parse_args(argv)
    try:
        problems = run(os.path.abspath(args.out), check=args.check, allow_replace=args.allow_replace)
    except PackageError as e:
        sys.stderr.write("package: %s\n" % e)
        return 1
    for p in problems:
        sys.stderr.write("package: %s\n" % p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
