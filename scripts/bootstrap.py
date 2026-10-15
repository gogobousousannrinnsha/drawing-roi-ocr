"""Build the app locally from pinned upstream assets; never download the user's documents."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile

APP_NAME = "drawing-roi-ocr"
APP_VERSION = "0.1.2"
MAX_ROOT_UTF16 = 40
GIB = 1024 ** 3
ALLOWED_HOSTS = {"files.pythonhosted.org", "paddle-whl.cdn.bcebos.com", "paddle-model-ecology.bj.bcebos.com"}


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b""):
            value.update(block)
    return value.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".new")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    temporary.replace(path)


@contextmanager
def operation_lock(root):
    """Use the same Windows byte lock as FileLock, before FileLock is installed."""
    import msvcrt
    path = Path(root)/"data/operation.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError("他の初期設定・テンプレート作成・抽出が実行中です") from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def validate_asset(item):
    from urllib.parse import urlparse
    url = urlparse(item["url"])
    if url.scheme != "https" or url.hostname not in ALLOWED_HOSTS or url.username or url.password:
        raise ValueError("取得先が公式資産一覧の許可範囲外です")
    digest = item["sha256"]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("SHA-256が不正です")
    filename = item["filename"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+\-]*", filename) or filename in (".", ".."):
        raise ValueError("取得ファイル名が不正です")


def download(item, folder, offline=False):
    validate_asset(item)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / item["filename"]
    expected = item["sha256"]
    if target.is_file() and sha(target) == expected:
        print(f"[再利用] {target.name}", flush=True)
        return target
    if target.exists():
        target.rename(target.with_name(target.name+f".bad-{uuid.uuid4().hex[:12]}"))
    if offline:
        raise RuntimeError(f"オフライン: 検査済み資産がありません: {target.name}")
    partial = target.with_name(target.name+".part")
    print(f"[取得] {target.name}", flush=True)
    command = [str(Path(os.environ.get("SystemRoot", "C:/Windows"))/"System32/curl.exe"),
               "--fail", "--location", "--silent", "--show-error", "--retry", "3",
               "--connect-timeout", "30", "--proto", "=https", "--proto-redir", "=https",
               "--continue-at", "-", "--output", str(partial), item["url"]]
    result = subprocess.run(command, check=False)
    if result.returncode in (33, 36) and partial.exists():
        partial.rename(partial.with_name(partial.name+f".resume-{uuid.uuid4().hex[:12]}"))
        result = subprocess.run(command, check=False)
    if result.returncode == 23:
        raise RuntimeError(f"保存できませんでした。空き容量・書込み権限を確認して再実行してください: {target.name}")
    if result.returncode:
        raise RuntimeError(f"取得に失敗しました({result.returncode})。再実行すると途中から再開します: {target.name}")
    if sha(partial) != expected:
        partial.rename(partial.with_name(partial.name+f".bad-{uuid.uuid4().hex[:12]}"))
        raise RuntimeError(f"取得内容が固定SHAと一致しません: {target.name}")
    partial.replace(target)
    return target


def safe_member(name, root):
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in ("..", "") or ":" in part for part in path.parts):
        raise ValueError(f"アーカイブの保存先が不正です: {name}")
    destination = Path(root).joinpath(*path.parts)
    destination.resolve().relative_to(Path(root).resolve())
    return destination


def extract(archive, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = safe_member(member.filename, destination)
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("リンクを含むアーカイブは展開しません")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive) as bundle:
            for member in bundle:
                target = safe_member(member.name, destination)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
                else:
                    raise ValueError("リンク・特殊ファイルを含むアーカイブは展開しません")


def installed_wheels_ok(root, packages):
    from importlib.metadata import distributions
    import base64
    import csv
    import io
    site = root/"runtime/Lib/site-packages"
    installed = {dist.metadata["Name"].lower().replace("_", "-"): dist
                 for dist in distributions(path=[str(site)])}
    for item in packages:
        dist = installed.get(item["name"].lower().replace("_", "-"))
        if dist is None or dist.version != item["version"]:
            return False
        record = dist.read_text("RECORD")
        if not record:
            return False
        for row in csv.reader(io.StringIO(record)):
            if len(row) != 3:
                return False
            name, file_hash, size = row
            # Bytecode is generated locally and may be absent with -B/--no-compile.
            if name.endswith(".pyc"):
                continue
            path = Path(dist.locate_file(name))
            try:
                path.resolve().relative_to((root/"runtime").resolve())
            except ValueError:
                return False
            # pip generates launchers locally; two distributions can intentionally
            # own the same console command (modelscope/modelscope-hub). The app
            # uses python.exe and imports, never these generated entry points.
            if path.parent.resolve() == (root/"runtime/Scripts").resolve() and path.suffix == ".exe":
                continue
            if not path.is_file():
                return False
            if size and path.stat().st_size != int(size):
                return False
            if file_hash:
                if not file_hash.startswith("sha256="):
                    return False
                actual = base64.urlsafe_b64encode(bytes.fromhex(sha(path))).rstrip(b"=").decode("ascii")
                if actual != file_hash.partition("=")[2]:
                    return False
    return True


def check_capacity(root):
    """Budget fixed downloads plus extraction and pip working space, without LLM assets."""
    packages = read(root/"assets/python-packages.json")["packages"]
    python_assets = read(root/"assets/python-runtime.json")["assets"]
    models = read(root/"assets/ocr-models.json")
    wheel_bytes = sum(item["bytes"] for item in packages)
    python_bytes = sum(item["size"] for item in python_assets)
    ocr_bytes = sum(item["archive_bytes"] for item in models)
    downloads = [(root/"downloads/wheels"/item["filename"], item["bytes"]) for item in packages]
    downloads += [(root/"downloads/python"/item["name"], item["size"]) for item in python_assets]
    downloads += [(root/"downloads/ocr"/item["source"].rsplit("/", 1)[-1], item["archive_bytes"]) for item in models]
    # Existing cache is verified again by each downloader; this only estimates missing bytes.
    missing_bytes = sum(size if not path.is_file() or path.stat().st_size != size else 0
                        for path, size in downloads)
    # Reserve up to three compressed wheel totals for extraction/reinstallation, two OCR
    # archives, five Python MSI totals, and 1 GiB for the authored acceptance checks.
    # Keeping the reserve on reruns allows a damaged runtime to be repaired safely.
    reserve_bytes = 3*wheel_bytes + 2*ocr_bytes + 5*python_bytes + GIB
    required_bytes = missing_bytes + reserve_bytes
    free = shutil.disk_usage(root).free
    plan = {"schema_version": 1, "download_bytes": python_bytes + wheel_bytes + ocr_bytes,
            "missing_download_bytes": missing_bytes, "extraction_and_work_reserve_bytes": reserve_bytes,
            "required_free_bytes": required_bytes, "observed_free_bytes": free,
            "python_packages_sha256": sha(root/"assets/python-packages.json")}
    write(root/"data/setup-capacity.json", plan)
    print(f"空き容量: {free/GIB:.2f} GiB / 必要量の見積り {required_bytes/GIB:.2f} GiB", flush=True)
    if free < required_bytes:
        raise RuntimeError(f"固定取得物と展開作業に {required_bytes/GIB:.2f} GiB の空き容量が必要です")
    return plan


def check_root_length(root):
    """Count Windows UTF-16 units, including supplementary characters as two units."""
    count = len(str(root).encode("utf-16-le")) // 2
    if count > MAX_ROOT_UTF16:
        raise ValueError(f"アプリの配置先が長すぎます（{count} / {MAX_ROOT_UTF16} UTF-16単位）。"
                         r"フォルダー一式を E:\ROI-OCR などの短い場所へ移して再実行してください。")
    return {"root_utf16_units": count, "max_root_utf16_units": MAX_ROOT_UTF16}


def pip_environment(root):
    # Python -I does not isolate pip's own PIP_* variables or pip.ini files.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    env["PIP_CACHE_DIR"] = str(root/"temp/pip")
    env["TEMP"] = env["TMP"] = str(root/"temp")
    return env


def setup_dependencies(root, offline):
    packages = read(root/"assets/python-packages.json")["packages"]
    wheel_folder = root/"downloads/wheels"
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda item: download(item, wheel_folder, offline), packages))
    lock = root/"data/setup-requirements.lock.txt"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("\n".join(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}" for item in packages)+"\n", encoding="utf-8")
    python = root/"runtime/python.exe"
    marker = root/"data/setup-dependencies.json"
    signature = sha(root/"assets/python-packages.json")
    if not (marker.exists() and read(marker).get("manifest_sha256") == signature and installed_wheels_ok(root, packages)):
        subprocess.run([str(python), "-I", "-B", "-X", "utf8", "-m", "pip", "--isolated", "--no-cache-dir", "install", "--prefix", str(root/"runtime"), "--no-user", "--force-reinstall", "--no-index", "--find-links", str(wheel_folder),
                        "--only-binary", ":all:", "--require-hashes", "--no-compile", "--no-warn-script-location",
                        "--disable-pip-version-check", "-r", str(lock)], env=pip_environment(root), check=True)
    subprocess.run([str(python), "-I", "-B", "-X", "utf8", "-m", "pip", "--isolated", "--no-cache-dir", "check"], env=pip_environment(root), check=True)
    if not installed_wheels_ok(root, packages):
        raise RuntimeError("依存パッケージの版が固定一覧と一致しません")
    write(marker, {"status": "complete", "manifest_sha256": signature, "packages": len(packages)})


def bundled_core_manifest(root):
    scripts = str(Path(__file__).resolve().parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from bundled_core import validate_core_bundle
    return validate_core_bundle(root)


def installed_core_ok(root, item):
    """Compare installed code with the pinned wheel, not a mutable installed RECORD."""
    site = root/"runtime/Lib/site-packages"
    with zipfile.ZipFile(root/item["path"]) as bundle:
        expected_sources = set()
        for member in bundle.infolist():
            if member.filename.endswith("/RECORD"):
                # pip adds its own INSTALLER/direct_url metadata to installed RECORD.
                continue
            path = site.joinpath(*PurePosixPath(member.filename).parts)
            try:
                path.resolve().relative_to((root/"runtime").resolve())
            except ValueError:
                return False
            if path.is_symlink() or not path.is_file() or path.read_bytes() != bundle.read(member):
                return False
            if member.filename.startswith("docuworks_ctypes/"):
                expected_sources.add(member.filename)
        actual_sources = {path.relative_to(site).as_posix()
                          for path in (site/"docuworks_ctypes").rglob("*")
                          if path.is_file() and "__pycache__" not in path.parts}
        return actual_sources == expected_sources


def setup_bundled_core(root):
    """Install the user's MIT Core only from the validated ZIP member, without network."""
    marker = root/"data/setup-core.json"
    write(marker, {"status": "in_progress", "name": "docuworks-ctypes", "version": "1.0.1"})
    try:
        _install_bundled_core(root)
    except (Exception, KeyboardInterrupt) as error:
        write(marker, {"status": "failed", "name": "docuworks-ctypes", "version": "1.0.1",
                       "reason": str(error) or type(error).__name__})
        raise


def _install_bundled_core(root):
    item = bundled_core_manifest(root)
    lock = root/"data/setup-core.lock.txt"
    lock.parent.mkdir(parents=True, exist_ok=True)
    (root/"temp").mkdir(exist_ok=True)
    lock.write_text(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n",
                    encoding="utf-8", newline="\n")
    python = root/"runtime/python.exe"
    if not installed_core_ok(root, item):
        subprocess.run([str(python), "-I", "-B", "-X", "utf8", "-m", "pip", "--isolated",
                        "--no-cache-dir", "install", "--prefix", str(root/"runtime"), "--no-user",
                        "--force-reinstall", "--no-index", "--no-deps", "--find-links",
                        str((root/item["path"]).parent), "--only-binary", ":all:", "--require-hashes",
                        "--no-compile", "--no-warn-script-location", "--disable-pip-version-check",
                        "-r", str(lock)], env=pip_environment(root), check=True)
    if not installed_core_ok(root, item):
        raise RuntimeError("同封Coreの導入結果が固定配布物と一致しません")
    subprocess.run([str(python), "-I", "-B", "-X", "utf8", "-m", "pip", "--isolated",
                    "--no-cache-dir", "check"], env=pip_environment(root), check=True)
    write(root/"data/setup-core.json", {"status": "complete", "name": item["name"],
          "version": item["version"], "wheel_sha256": item["sha256"],
          "manifest_sha256": sha(root/"assets/bundled-core.json"), "source": "bundled-local-wheel"})


def setup_ocr(root, offline):
    original = root/"assets/ocr-models.json"
    models = read(original)
    model_root = root/"models/ocr"
    for item in models:
        item_asset = {"filename": item["source"].rsplit("/",1)[-1], "url": item["source"], "sha256": item["archive_sha256"]}
        archive = download(item_asset, root/"downloads/ocr", offline)
        valid = all((model_root / name).is_file() and sha(model_root/name) == expected for name, expected in item["files"].items())
        if not valid:
            stage = root/"temp"/("o-"+uuid.uuid4().hex[:12])
            stage.mkdir(exist_ok=False)
            extract(archive, stage)
            # Upstream tar directories may carry _infer in their top-level name.
            for name, expected in item["files"].items():
                filename = Path(name).name
                candidates = [path for path in stage.rglob(filename) if path.is_file() and sha(path) == expected]
                if len(candidates) != 1:
                    raise RuntimeError(f"OCR展開結果が固定一覧と一致しません: {name}")
                destination = model_root/name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidates[0], destination)
    model_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(original, model_root/"model-manifest.json")
    for name in ("LICENSE-APACHE-2.0.txt", "NOTICE.txt"):
        notice = root/"licenses/ocr"/name
        if not notice.is_file():
            raise RuntimeError(f"OCRの利用条件文書がありません: {name}")
        shutil.copy2(notice, model_root/name)
    write(root/"data/setup-ocr.json", {"status": "complete", "manifest_sha256": sha(original)})


def check_runtime(root):
    env = pip_environment(root)
    for key, value in {"PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True", "PADDLE_HOME": str(root/"temp/paddle"),
                       "PADDLE_PDX_CACHE_HOME": str(root/"temp/paddlex"), "HF_HOME": str(root/"temp/huggingface"),
                       "PYTHONIOENCODING": "utf-8"}.items():
        env[key] = value
    code = ("import json,sys,tkinter;import paddle; "
            "result={'python':sys.version,'paddle':paddle.__version__,"
            "'cuda':paddle.device.is_compiled_with_cuda(),'gpu_count':paddle.device.cuda.device_count(),"
            "'tk':tkinter.Tcl().eval('info patchlevel')};print(json.dumps(result));"
            "assert result['cuda'] and result['gpu_count']>0, 'NVIDIA GPU / driver is unavailable'")
    subprocess.run([str(root/"runtime/python.exe"), "-I", "-B", "-X", "utf8", "-c", code], env=env, check=True)


def check_application(root):
    """Exercise real OCR and real DocuWorks through the application's own entry point."""
    paths = ("data/setup-real-ocr.json", "data/setup-docuworks.json")
    # A previous successful record may never satisfy a newly failed selfcheck.
    previous = {name: sha(root/name) if (root/name).is_file() else None for name in paths}
    subprocess.run([str(root/"runtime/python.exe"), "-I", "-B", "-X", "utf8", str(root/"source/run.py"),
                    "selfcheck", "--root", str(root)], env=pip_environment(root), cwd=root, check=True)
    reports = {}
    for name in paths:
        path = root/name
        if not path.is_file() or read(path).get("status") != "complete":
            raise RuntimeError(f"自己検査の完了記録がありません: {name}")
        digest = sha(path)
        if previous[name] is not None and digest == previous[name]:
            raise RuntimeError(f"自己検査記録が更新されていません: {name}")
        reports[name] = {"path": name, "sha256": digest}
    return reports


def run_setup(root, offline=False):
    root = Path(root).resolve(strict=True)
    (root/"data").mkdir(exist_ok=True)
    marker = root/"data/setup-complete.json"
    with operation_lock(root):
        setup_id = uuid.uuid4().hex[:12]
        record = {"status": "in_progress", "app_name": APP_NAME, "app_version": APP_VERSION,
                  "setup_id": setup_id, "stage": "root_path",
                  "root_utf16_units": len(str(root).encode("utf-16-le")) // 2,
                  "max_root_utf16_units": MAX_ROOT_UTF16}
        write(marker, record)
        try:
            record.update(check_root_length(root))
            for name in ("temp", "logs", "input", "templates", "exports"):
                (root/name).mkdir(exist_ok=True)
            record["stage"] = "capacity"
            write(marker, record)
            capacity = check_capacity(root)
            bundled_core_manifest(root)
            for stage, title, action in (
                ("dependencies", "[1/5] Pythonライブラリ", lambda: setup_dependencies(root, offline)),
                ("core", "[2/5] 同封Core 1.0.1", lambda: setup_bundled_core(root)),
                ("ocr_models", "[3/5] OCRモデル", lambda: setup_ocr(root, offline)),
                ("gpu", "[4/5] GPU・GUI環境確認", lambda: check_runtime(root)),
            ):
                record["stage"] = stage
                write(marker, record)
                print(title, flush=True)
                action()
            record["stage"] = "selfcheck"
            write(marker, record)
            print("[5/5] 実OCR・DocuWorks作成・矩形読戻し確認", flush=True)
            reports = check_application(root)
            complete = {**record, "status": "complete", "stage": "complete",
                        "bundled_core": read(root/"data/setup-core.json"),
                        "python_packages_sha256": sha(root/"assets/python-packages.json"),
                        "ocr_manifest_sha256": sha(root/"assets/ocr-models.json"),
                        "capacity_record": "data/setup-capacity.json", "checks": reports,
                        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            write(marker, complete)
        except (Exception, KeyboardInterrupt) as error:
            write(marker, {**record, "status": "cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                           "reason": str(error) or type(error).__name__})
            raise
    print("初期設定が完了しました。テンプレート作成.bat・抽出.batを使用できます。", flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="固定資産から範囲OCR専用環境を構築します")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3,13) or os.name != "nt":
        raise RuntimeError("Windows x64 Python 3.13用の初期設定です")
    return run_setup(args.root, args.offline)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("中断しました。初期設定.batを再実行すると検査済み資産を再利用します。", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"初期設定失敗: {error}", file=sys.stderr)
        raise SystemExit(1)
