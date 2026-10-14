"""Failure-path coverage for online setup; these tests never use the network or SDK."""
from contextlib import nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import zipfile

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("roi_bootstrap_tests", APP_ROOT/"scripts/bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture
def setup_root(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "MAX_ROOT_UTF16", 240)
    root = tmp_path/"日本語 portable"
    (root/"assets").mkdir(parents=True)
    for path in (APP_ROOT/"assets").glob("*"):
        shutil.copy2(path, root/"assets"/path.name)
    (root/"data").mkdir()
    return root


def test_capacity_uses_fixed_asset_sizes_and_reusable_cache(setup_root, monkeypatch):
    monkeypatch.setattr(bootstrap.shutil, "disk_usage", lambda _: SimpleNamespace(free=100*bootstrap.GIB))
    plan = bootstrap.check_capacity(setup_root)
    wheels = bootstrap.read(setup_root/"assets/python-packages.json")["packages"]
    models = bootstrap.read(setup_root/"assets/ocr-models.json")
    py = bootstrap.read(setup_root/"assets/python-runtime.json")["assets"]
    total = sum(p["bytes"] for p in wheels)+sum(p["archive_bytes"] for p in models)+sum(p["size"] for p in py)
    assert plan["download_bytes"] == total
    assert plan["missing_download_bytes"] == total
    assert plan["required_free_bytes"] == total+plan["extraction_and_work_reserve_bytes"]
    cached = setup_root/"downloads/python"/py[0]["name"]
    cached.parent.mkdir(parents=True)
    with cached.open("wb") as stream:
        stream.truncate(py[0]["size"])
    assert bootstrap.check_capacity(setup_root)["missing_download_bytes"] == total-py[0]["size"]
    monkeypatch.setattr(bootstrap.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(RuntimeError, match="GiB"):
        bootstrap.check_capacity(setup_root)


def test_offline_download_reuses_only_matching_sha(tmp_path, monkeypatch):
    content = b"fixed upstream asset"
    item = {"filename": "asset.whl", "url": "https://files.pythonhosted.org/asset.whl",
            "sha256": hashlib.sha256(content).hexdigest()}
    target = tmp_path/item["filename"]
    target.write_bytes(content)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: pytest.fail("Offline setup used network"))
    assert bootstrap.download(item, tmp_path, offline=True) == target
    target.write_bytes(b"broken cache")
    with pytest.raises(RuntimeError, match="オフライン"):
        bootstrap.download(item, tmp_path, offline=True)
    assert not target.exists()
    assert len(list(tmp_path.glob("asset.whl.bad-*"))) == 1


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/escape", "dir/../../escape", "dir\\..\\escape"])
def test_archive_paths_cannot_escape(tmp_path, name):
    with pytest.raises(ValueError):
        bootstrap.safe_member(name, tmp_path)


def test_link_archive_is_rejected(tmp_path):
    archive = tmp_path/"linked.zip"
    item = zipfile.ZipInfo("linked")
    item.create_system = 3
    item.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr(item, "../elsewhere")
    with pytest.raises(ValueError, match="リンク"):
        bootstrap.extract(archive, tmp_path/"out")


@pytest.mark.parametrize("failed_stage", ["dependencies", "core", "ocr_models", "gpu", "selfcheck"])
def test_any_failed_stage_replaces_old_completion(setup_root, monkeypatch, failed_stage):
    bootstrap.write(setup_root/"data/setup-complete.json", {"status": "complete", "old": True})
    monkeypatch.setattr(bootstrap, "operation_lock", lambda _: nullcontext())
    monkeypatch.setattr(bootstrap, "check_capacity", lambda _: {})
    monkeypatch.setattr(bootstrap, "bundled_core_manifest", lambda _: {})
    functions = {"dependencies": "setup_dependencies", "core": "setup_bundled_core", "ocr_models": "setup_ocr",
                 "gpu": "check_runtime", "selfcheck": "check_application"}
    calls = []
    def action(stage):
        def run(*args):
            calls.append(stage)
            if stage == failed_stage:
                raise RuntimeError("authored stage failure")
        return run
    for stage, function in functions.items():
        monkeypatch.setattr(bootstrap, function, action(stage))
    with pytest.raises(RuntimeError, match="authored stage failure"):
        bootstrap.run_setup(setup_root, offline=True)
    record = bootstrap.read(setup_root/"data/setup-complete.json")
    assert record["status"] == "failed" and record["stage"] == failed_stage
    assert "old" not in record
    assert calls[-1] == failed_stage


def test_real_checks_require_new_complete_records(setup_root, monkeypatch):
    paths = ("setup-real-ocr.json", "setup-docuworks.json")
    for name in paths:
        bootstrap.write(setup_root/"data"/name, {"status": "complete", "run_id": "old"})
    def no_change(*args, **kwargs):
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(bootstrap.subprocess, "run", no_change)
    with pytest.raises(RuntimeError, match="更新"):
        bootstrap.check_application(setup_root)
    def fresh(*args, **kwargs):
        for name in paths:
            bootstrap.write(setup_root/"data"/name, {"status": "complete", "run_id": "new"})
    monkeypatch.setattr(bootstrap.subprocess, "run", fresh)
    records = bootstrap.check_application(setup_root)
    assert set(records) == {"data/setup-real-ocr.json", "data/setup-docuworks.json"}
    assert all(len(record["sha256"]) == 64 and not Path(record["path"]).is_absolute() for record in records.values())


def test_setup_success_records_ocr_only_and_relative_evidence(setup_root, monkeypatch):
    monkeypatch.setattr(bootstrap, "operation_lock", lambda _: nullcontext())
    for name in ("check_capacity", "bundled_core_manifest", "setup_dependencies", "setup_ocr", "check_runtime"):
        monkeypatch.setattr(bootstrap, name, lambda *args: {})
    monkeypatch.setattr(bootstrap, "setup_bundled_core", lambda root: bootstrap.write(root/"data/setup-core.json", {"status": "complete"}))
    reports = {name: {"path": name, "sha256": "a"*64} for name in ("data/setup-real-ocr.json", "data/setup-docuworks.json")}
    monkeypatch.setattr(bootstrap, "check_application", lambda _: reports)
    assert bootstrap.run_setup(setup_root) == 0
    record = bootstrap.read(setup_root/"data/setup-complete.json")
    assert record["status"] == "complete" and record["app_version"] == "0.1.1"
    assert record["checks"] == reports
    assert not any(key in record for key in ("ollama_archive_sha256", "model_digest", "model"))


def test_gpu_check_fails_instead_of_using_cpu(setup_root, monkeypatch):
    commands = []
    def capture(command, **kwargs):
        commands.append(command)
    monkeypatch.setattr(bootstrap.subprocess, "run", capture)
    bootstrap.check_runtime(setup_root)
    assert "assert result['cuda'] and result['gpu_count']>0" in commands[0][-1]
    assert not hasattr(bootstrap, "setup_ollama") and not hasattr(bootstrap, "setup_model")


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5 regression")
def test_winps_can_atomically_replace_an_existing_setup_record(tmp_path):
    script = tmp_path/"atomic-json-probe.ps1"
    script.write_text("""param([string]$Functions, [string]$Folder)
$ErrorActionPreference = 'Stop'
. $Functions -Root $Folder -ManifestPath (Join-Path $Folder 'unused.json') -FunctionsOnly
$record = Join-Path $Folder 'record.json'
Write-AtomicJson $record @{status='in_progress'}
Write-AtomicJson $record @{status='failed';reason='authored regression'}
""", encoding="ascii")
    command = [str(Path(os.environ.get("SystemRoot", "C:/Windows"))/"System32/WindowsPowerShell/v1.0/powershell.exe"),
               "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
               "-Functions", str(APP_ROOT/"scripts/setup_python.ps1"), "-Folder", str(tmp_path)]
    result = subprocess.run(command, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    record = json.loads((tmp_path/"record.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed" and record["reason"] == "authored regression"


@pytest.mark.parametrize("root", ["C:\\"+"a"*37, "C:\\"+"a"*35+"\U0001f600"])
def test_root_path_accepts_exactly_40_utf16_units(root):
    assert bootstrap.check_root_length(root) == {"root_utf16_units": 40, "max_root_utf16_units": 40}


@pytest.mark.parametrize("root", ["C:\\"+"a"*38, "C:\\"+"a"*36+"\U0001f600"])
def test_root_path_rejects_41_utf16_units_with_short_location(root):
    with pytest.raises(ValueError, match=r"41 / 40") as error:
        bootstrap.check_root_length(root)
    assert r"E:\ROI-OCR" in str(error.value)


def test_long_root_replaces_previous_completion_before_download_or_runtime(setup_root, monkeypatch):
    monkeypatch.setattr(bootstrap, "MAX_ROOT_UTF16", 40)
    monkeypatch.setattr(bootstrap, "operation_lock", lambda _: nullcontext())
    monkeypatch.setattr(bootstrap, "check_capacity", lambda _: pytest.fail("Path rejection must precede capacity and downloads"))
    bootstrap.write(setup_root/"data/setup-complete.json", {"status": "complete", "old": True})
    with pytest.raises(ValueError, match="40"):
        bootstrap.run_setup(setup_root)
    record = bootstrap.read(setup_root/"data/setup-complete.json")
    assert record["status"] == "failed" and record["stage"] == "root_path" and "old" not in record
    assert not (setup_root/"runtime").exists() and not (setup_root/"temp").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5 startup guard")
@pytest.mark.parametrize("script_name", ["setup.ps1", "setup_python.ps1"])
def test_winps_rejects_long_root_before_python_and_invalidates_old_completion(tmp_path, script_name):
    root = tmp_path/"long root with supplementary \U0001f600"
    (root/"data").mkdir(parents=True)
    bootstrap.write(root/"data/setup-complete.json", {"status": "complete", "old": True})
    command = [str(Path(os.environ.get("SystemRoot", "C:/Windows"))/"System32/WindowsPowerShell/v1.0/powershell.exe"),
               "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(APP_ROOT/"scripts"/script_name),
               "-Root", str(root)]
    if script_name == "setup_python.ps1":
        command.extend(["-ManifestPath", str(root/"unused.json")])
    result = subprocess.run(command, capture_output=True, timeout=30)
    assert result.returncode != 0
    assert b"E:\\ROI-OCR" in result.stdout+result.stderr
    record = bootstrap.read(root/"data/setup-complete.json")
    assert record["status"] == "failed" and record["stage"] == "root_path" and "old" not in record
    assert record["root_utf16_units"] == len(str(root).encode("utf-16-le"))//2
    assert not (root/"runtime").exists() and not (root/"temp").exists()


@pytest.mark.skipif(os.name != "nt", reason="Real Windows BAT argument and working-directory regression")
def test_setup_bat_keeps_its_own_root_after_shift_from_another_directory(tmp_path):
    app = tmp_path/"app 日本語 space"
    (app/"scripts").mkdir(parents=True)
    unrelated = tmp_path/"unrelated cwd"
    unrelated.mkdir()
    shutil.copy2(APP_ROOT/"初期設定.bat", app/"初期設定.bat")
    # This stub records only launcher arguments; it cannot install Python or start OCR/GPU.
    (app/"scripts/setup.ps1").write_text("""param([string]$Root, [switch]$Offline)
$ErrorActionPreference = 'Stop'
$record = @{root=[IO.Path]::GetFullPath($Root).TrimEnd('\\'); offline=[bool]$Offline; cwd=(Get-Location).Path}
[IO.File]::WriteAllText((Join-Path $Root 'stub.json'), ($record | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
""", encoding="ascii")
    result = subprocess.run(["cmd.exe", "/d", "/c", str(app/"初期設定.bat"), "--no-pause", "--offline"],
                            cwd=unrelated, capture_output=True, timeout=30)
    assert result.returncode == 0, (result.stdout+result.stderr).decode(errors="replace")
    record = json.loads((app/"stub.json").read_text(encoding="utf-8-sig"))
    assert Path(record["root"]).resolve() == app.resolve()
    assert Path(record["cwd"]).resolve() == app.resolve()
    assert record["offline"] is True
    assert not (unrelated/"stub.json").exists() and not (app/"runtime").exists()
