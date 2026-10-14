"""Build and reread an allowlisted source distribution; never bundle live data."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import zipfile

APP_NAME = "drawing-roi-ocr"
APP_VERSION = "0.1.1"
ROOT_FILES = frozenset({"README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "pyproject.toml", ".gitignore",
                        "初期設定.bat", "テンプレート作成.bat", "抽出.bat"})
REQUIRED_ROOT = ROOT_FILES - {".gitignore", "pyproject.toml"}
SCRIPT_FILES = frozenset({"bootstrap.py", "setup.ps1", "setup_python.ps1", "bundled_core.py", "build_source_zip.py"})
ASSET_FILES = frozenset({"python-runtime.json", "python-packages.json", "requirements-runtime.lock.txt",
                         "bundled-core.json", "ocr-models.json"})
SECRET_PATTERNS = (r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{30,}",
                   r"AKIA[A-Z0-9]{16}", r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_core_validator():
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from bundled_core import validate_core_bundle
    return validate_core_bundle


def allowed(relative):
    parts = relative.parts
    if len(parts) == 1:
        return relative.name in ROOT_FILES
    if any(part.startswith(".") or part == "__pycache__" for part in parts):
        return False
    if parts[0] == "source":
        return relative.suffix == ".py" or relative.as_posix() in {"source/pyproject.toml", "source/LICENSE"}
    if parts[0] == "tests":
        return relative.suffix == ".py"
    if parts[0] == "scripts":
        return len(parts) == 2 and parts[1] in SCRIPT_FILES
    if parts[0] == "assets":
        return len(parts) == 2 and parts[1] in ASSET_FILES
    if parts[0] == "licenses":
        return relative.suffix.lower() in {".md", ".txt", ".json"}
    if parts[0] == "docs":
        return relative.suffix.lower() == ".md"
    return False


def source_files(root):
    root = Path(root).resolve(strict=True)
    core = load_core_validator()(root)
    paths = [root/name for name in ROOT_FILES if (root/name).exists()]
    for directory in ("source", "scripts", "assets", "licenses", "docs", "tests"):
        if (root/directory).is_dir():
            paths.extend(path for path in (root/directory).rglob("*") if path.is_file())
    files = {}
    for path in paths:
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if not allowed(relative):
            continue
        if path.is_symlink() or any(part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction())
                                    for part in (path, *path.parents) if part != root.parent):
            raise ValueError(f"Links are not publishable: {relative}")
        path.resolve(strict=True).relative_to(root)
        content = path.read_bytes()
        if len(content) > 1024*1024:
            raise ValueError(f"Source text is unexpectedly large: {relative}")
        text = content.decode("utf-8-sig")
        if "\x00" in text or any(re.search(pattern, text) for pattern in SECRET_PATTERNS):
            raise ValueError(f"Binary or credential material in source: {relative}")
        if re.search(r"[A-Za-z]:[\\/]Users[\\/](?!Public(?:[\\/]|$))", text, re.IGNORECASE):
            raise ValueError(f"Personal Windows path in source: {relative}")
        if path.suffix == ".py":
            ast.parse(text, filename=str(relative))
        if path.suffix == ".ps1" and any(ord(char) > 127 for char in text):
            raise ValueError(f"PowerShell source must be ASCII for Windows PowerShell 5: {relative}")
        files[relative.as_posix()] = content
    core_path = root/core["path"]
    files[core["path"]] = core_path.read_bytes()
    required = set(REQUIRED_ROOT) | {"source/run.py", "source/drawing_roi_ocr/__init__.py",
               "licenses/core/LICENSE.txt", "licenses/core/NOTICE.md", "licenses/ocr/LICENSE-APACHE-2.0.txt",
               "licenses/ocr/NOTICE.txt"}
    required.update("scripts/"+name for name in SCRIPT_FILES)
    required.update("assets/"+name for name in ASSET_FILES)
    missing = required-files.keys()
    if missing:
        raise ValueError("Missing required source files: " + ", ".join(sorted(missing)))
    init = ast.parse(files["source/drawing_roi_ocr/__init__.py"].decode("utf-8-sig"))
    versions = [node.value.value for node in init.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
                and isinstance(node.value, ast.Constant)]
    if versions != [APP_VERSION]:
        raise ValueError("Application source version differs from the distribution version")
    return dict(sorted(files.items())), core


def zip_info(name):
    item = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    item.compress_type = zipfile.ZIP_DEFLATED
    item.create_system = 3
    item.external_attr = (stat.S_IFREG | 0o644) << 16
    return item


def build_source_zip(root, output):
    root, output = Path(root).resolve(strict=True), Path(output).absolute()
    if output.suffix.lower() != ".zip":
        raise ValueError("Output must be a ZIP file")
    sidecars = (output.with_name(output.name+".sha256"), output.with_name(output.name+".manifest.json"))
    if any(path.exists() for path in (output, *sidecars)):
        raise FileExistsError("Distribution output already exists")
    files, core = source_files(root)
    manifest = {"schema_version": 1, "package": APP_NAME, "version": APP_VERSION,
                "distribution_kind": "source-with-bundled-core-online-setup",
                "bundled_core": {key: core[key] for key in ("name", "version", "path", "sha256")},
                "files": [{"path": name, "bytes": len(data), "sha256": sha256(data)} for name, data in files.items()]}
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2)+"\n").encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name+".building")
    if temporary.exists():
        raise FileExistsError(temporary)
    try:
        with zipfile.ZipFile(temporary, "x") as archive:
            for name, content in files.items():
                archive.writestr(zip_info(name), content, compresslevel=9)
            archive.writestr(zip_info("SOURCE_MANIFEST.json"), encoded, compresslevel=9)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() or set(archive.namelist()) != set(files)|{"SOURCE_MANIFEST.json"}:
                raise ValueError("Distribution ZIP verification failed")
            for name, content in files.items():
                if archive.read(name) != content:
                    raise ValueError(f"Distribution contents differ: {name}")
            if archive.read("SOURCE_MANIFEST.json") != encoded:
                raise ValueError("Distribution manifest differs")
        # Publishing an output name must not overwrite an archive created by another process.
        temporary.rename(output)
    finally:
        if temporary.exists():
            temporary.unlink()
    digest = sha256(output.read_bytes())
    summary = {**manifest, "archive": {"name": output.name, "bytes": output.stat().st_size, "sha256": digest}}
    with sidecars[0].open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{digest}  {output.name}\n")
    with sidecars[1].open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"package": APP_NAME, "version": APP_VERSION, "source_files": len(files),
            "archive": output.name, "bytes": output.stat().st_size, "sha256": digest}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build_source_zip(args.root, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
