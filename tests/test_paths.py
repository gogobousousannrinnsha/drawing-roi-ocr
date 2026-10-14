from pathlib import Path
from types import SimpleNamespace
import shutil

import pytest

from drawing_roi_ocr import storage
from drawing_roi_ocr.excel import export_result, write_excel


def test_windows_root_budget_counts_utf16_surrogate_pairs():
    root = Path("E:/") / ("x" * 37)
    assert storage.path_units(root) == 40
    assert storage.require_short_root(root) == root.resolve()
    with pytest.raises(ValueError, match="ROI-OCR"):
        storage.require_short_root(Path(str(root) + "x"))
    with pytest.raises(ValueError):
        storage.require_short_root(Path("E:/") / ("x" * 36 + "😀"))


def test_full_path_budget_and_short_ids():
    root = Path("E:/") / ("x" * 37)
    identifier = storage.new_id()
    assert len(identifier) == 21
    path = root / "data" / "r" / identifier / "d0001p0001" / "f0001" / "c123456789abc.png"
    assert storage.path_units(path) <= 110
    assert storage.require_short_path(path) == path.resolve()
    too_long = root / ("y" * 201)
    with pytest.raises(ValueError, match="240"):
        storage.require_short_path(too_long)


def test_atomic_json_temp_collision_preserves_existing_files(tmp_path, monkeypatch):
    target = tmp_path / "result.json"
    target.write_text('original', encoding="utf-8")
    temporary = tmp_path / ("j" + "a" * 12 + ".tmp")
    temporary.write_text('another operation', encoding="utf-8")
    monkeypatch.setattr(storage.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    with pytest.raises(FileExistsError):
        storage.write_json(target, {"new": True})
    assert target.read_text(encoding="utf-8") == 'original'
    assert temporary.read_text(encoding="utf-8") == 'another operation'


def test_atomic_excel_temp_collision_preserves_existing_file(tmp_path, monkeypatch):
    import drawing_roi_ocr.excel as excel
    temporary = tmp_path / ("e" + "a" * 12 + ".xlsx")
    temporary.write_bytes(b"another operation")
    monkeypatch.setattr(excel.uuid, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    with pytest.raises(FileExistsError):
        write_excel(tmp_path / "result.xlsx", {})
    assert temporary.read_bytes() == b"another operation"


def test_old_runs_layout_is_still_reexportable(tmp_path):
    from test_workflow import scenario
    from drawing_roi_ocr import workflow
    root, folder, args, _ = scenario(tmp_path, pages=1)
    _, result_path, _ = workflow.extract(root, folder, **args)
    old_folder = root / "data" / "runs" / "20261014-010203-123456-abcdef"
    shutil.copytree(result_path.parent, old_folder)
    result = storage.read_json(result_path)
    def rewrite(value):
        if isinstance(value, dict):
            return {k: rewrite(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        if isinstance(value, str):
            return value.replace(storage.relative(root, result_path.parent), storage.relative(root, old_folder))
        return value
    storage.write_json(old_folder / "result.json", rewrite(result))
    storage.write_json(old_folder / "receipt.json", {"result_sha256": storage.sha(old_folder / "result.json")})
    assert export_result(root, old_folder / "result.json").is_file()
