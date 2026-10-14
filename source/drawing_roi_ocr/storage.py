from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

def now():
    return datetime.now(timezone.utc).isoformat()

def new_id():
    return datetime.now().strftime("%y%m%d%H%M%S-") + uuid.uuid4().hex[:8]


MAX_ROOT_UNITS = 40
MAX_PATH_UNITS = 240


def path_units(path):
    """Windows path limits count UTF-16 units, including surrogate pairs."""
    return len(str(Path(path).expanduser().resolve()).encode("utf-16-le")) // 2


def require_short_path(path, limit=MAX_PATH_UNITS, label="保存先"):
    path = Path(path).expanduser().resolve()
    length = path_units(path)
    if length > limit:
        raise ValueError(f"{label}のパスが長すぎます（{length}／上限{limit}文字）。"
                         "E:\\ROI-OCRなどの短い場所へ配置し、長いフォルダー名・ファイル名を短くしてください。")
    return path


def require_short_root(root):
    return require_short_path(root, MAX_ROOT_UNITS, "アプリ配置先")

def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def write_json(path, value):
    path = require_short_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = require_short_path(path.with_name("j" + uuid.uuid4().hex[:12] + ".tmp"), label="一時保存先")
    owned = False
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            owned = True
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Antivirus/indexers can briefly hold the complete staging file on
        # Windows. Retry only sharing/lock violations, for at most 2 s of waits.
        for attempt in range(21):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (32, 33) or attempt == 20:
                    raise
                time.sleep(.1)
    finally:
        try:
            if owned:
                temporary.unlink(missing_ok=True)
        except OSError:
            # A locked leftover may remain; cleanup must not hide the write error.
            pass

def local_path(root, relative):
    """Saved references may never escape the portable directory."""
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("保存パスが候補フォルダーの外を指しています。")
    return path

def relative(root, path):
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
