"""Archive acceptance: user data stays out and hashes describe exact source bytes."""
import importlib.util
import json
from pathlib import Path
import shutil
import zipfile

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("roi_distribution_tests", APP_ROOT/"scripts/build_source_zip.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.fixture
def distribution_root(tmp_path):
    root = tmp_path/"source 日本語"
    root.mkdir()
    for name in ("assets", "scripts", "bundled", "licenses"):
        shutil.copytree(APP_ROOT/name, root/name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in builder.REQUIRED_ROOT:
        (root/name).write_text("authored source\n", encoding="utf-8")
    package = root/"source/drawing_roi_ocr"
    package.mkdir(parents=True)
    (package/"__init__.py").write_text("__version__ = '0.1.2'\n", encoding="utf-8")
    (root/"source/run.py").write_text("print('authored test fixture')\n", encoding="utf-8")
    return root


def test_archive_includes_exact_sources_and_excludes_live_assets(distribution_root, tmp_path):
    root = distribution_root
    for name in ("runtime/python.exe", "data/result.json", "logs/run.log", "models/ocr/model.pdiparams",
                 "input/drawing.xdw", "templates/Scale.xdw", "exports/result.xlsx", "source/__pycache__/x.pyc",
                 "assets/unreviewed-model.json", "bundled/unknown.whl"):
        path = root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"excluded private material")
    output = tmp_path/"final.zip"
    result = builder.build_source_zip(root, output)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert "bundled/core/docuworks_ctypes-1.0.1-py3-none-any.whl" in names
        assert not any(name.startswith(("runtime/", "data/", "logs/", "models/", "input/", "templates/", "exports/")) for name in names)
        assert "assets/unreviewed-model.json" not in names
        manifest = json.loads(archive.read("SOURCE_MANIFEST.json"))
        assert len(names) == len(manifest["files"])+1
        for record in manifest["files"]:
            data = archive.read(record["path"])
            assert record["bytes"] == len(data) and record["sha256"] == builder.sha256(data)
    assert result["sha256"] == builder.sha256(output.read_bytes())
    assert output.with_name(output.name+".sha256").read_text().startswith(result["sha256"])
    sidecar = json.loads(output.with_name(output.name+".manifest.json").read_text(encoding="utf-8"))
    assert sidecar["archive"]["sha256"] == result["sha256"]


def test_distribution_preserves_prior_output(distribution_root, tmp_path):
    output = tmp_path/"existing.zip"
    output.write_bytes(b"existing distribution")
    with pytest.raises(FileExistsError):
        builder.build_source_zip(distribution_root, output)
    assert output.read_bytes() == b"existing distribution"


def test_corrupt_core_rejected_before_archive_creation(distribution_root, tmp_path):
    path = distribution_root/"bundled/core/docuworks_ctypes-1.0.1-py3-none-any.whl"
    path.write_bytes(b"not the fixed public Core")
    output = tmp_path/"broken.zip"
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        builder.build_source_zip(distribution_root, output)
    assert not output.exists()


def test_missing_launcher_rejected(distribution_root, tmp_path):
    (distribution_root/"抽出.bat").unlink()
    with pytest.raises(ValueError, match="Missing required"):
        builder.build_source_zip(distribution_root, tmp_path/"missing.zip")


def test_source_archive_is_reproducible(distribution_root, tmp_path):
    first, second = tmp_path/"one.zip", tmp_path/"two.zip"
    builder.build_source_zip(distribution_root, first)
    builder.build_source_zip(distribution_root, second)
    assert first.read_bytes() == second.read_bytes()


def test_distribution_rejects_stale_version(distribution_root, tmp_path):
    (distribution_root/"source/drawing_roi_ocr/__init__.py").write_text("__version__ = '0.0.0'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="version differs"):
        builder.build_source_zip(distribution_root, tmp_path/"stale.zip")
