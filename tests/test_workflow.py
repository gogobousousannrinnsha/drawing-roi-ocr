from copy import deepcopy
import json
from pathlib import Path
import zipfile
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

from drawing_roi_ocr import workflow
from drawing_roi_ocr.excel import export_result
from drawing_roi_ocr.roi_ocr import OcrInitializationError
from drawing_roi_ocr.storage import read_json, sha


def scenario(tmp_path, pages=2):
    root = workflow.ensure_layout(tmp_path / "日本語 空白")
    (root / "input" / "sample.pdf").write_bytes(b"authored source")
    folder = root / "templates" / "表題欄"
    folder.mkdir()
    fields = []
    for name in ("Scale", "Title"):
        path = folder / (name + ".xdw")
        path.write_bytes(name.encode())
        fields.append({"name": name, "path": str(path), "sha256": sha(path),
            "width_mm": 100., "height_mm": 100., "rotation": 0,
            "rect_mm": {"x": 10., "y": 10., "width": 30., "height": 20.}})
    page = {"number": 1, "width_mm": 100., "height_mm": 100.,
            "width_px": 100, "height_px": 100, "rotation": 0}
    calls = {"session": 0, "render": 0, "ocr": 0, "close": 0, "images": []}
    class Session:
        def __init__(self, _):
            calls["session"] += 1
        def close(self):
            calls["close"] += 1
    def inspect(_):
        return [{**page, "number": i} for i in range(1, pages + 1)]
    def render(source, number, output):
        calls["render"] += 1
        Image.new("RGB", (100, 100), "white").save(output)
        return {**page, "number": number}
    def recognize(image, metadata, rect, models, scratch, **kwargs):
        calls["ocr"] += 1
        calls["images"].append(id(kwargs["page_image"]))
        crop = Path(scratch) / "crop.png"
        Image.new("RGB", (30, 20), "white").save(crop)
        text = "001-020" if "f0001" in kwargs["crop_id"] else "=HYPERLINK(\"x\")"
        regions = [{"id": kwargs["crop_id"], "text": text, "confidence": 0.,
                    "bbox_mm": dict(rect), "bbox_px": dict(rect)}]
        return regions, {"original": text}, {"crop_path": str(crop), "box_px": [10, 10, 40, 30]}
    args = dict(session_factory=Session, recognize=recognize, template_loader=lambda _: deepcopy(fields),
                inspector=inspect, renderer=render, progress=lambda *a, **k: None)
    return root, folder, args, calls


def test_region_only_snapshot_shared_engine_and_literal_excel(tmp_path, monkeypatch):
    import drawing_roi_ocr.pdf_ocr as original
    monkeypatch.setattr(original, "recognize_page", lambda *a, **k: pytest.fail("full OCR called"))
    root, folder, args, calls = scenario(tmp_path)
    before = sha(root / "input" / "sample.pdf")
    code, result_path, excel_path = workflow.extract(root, folder, **args)
    assert code == 0
    assert calls["render"] == 2 and calls["ocr"] == 4
    assert calls["session"] == calls["close"] == 1
    assert calls["images"][0] == calls["images"][1]
    assert calls["images"][2] == calls["images"][3]
    assert sha(root / "input" / "sample.pdf") == before
    result = read_json(result_path)
    assert result["status"] == "complete"
    assert all(not Path(f["path"]).is_absolute() for f in result["template"]["fields"])
    assert result["pages"][0]["fields"][0]["regions"][0]["confidence"] == 0
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(excel_path) as workbook:
        metadata = ET.fromstring(workbook.read("xl/workbook.xml"))
        assert [s.attrib["name"] for s in metadata.findall("s:sheets/s:sheet", ns)] == ["抽出結果", "確認情報"]
        strings = ["".join(s.itertext()) for s in ET.fromstring(workbook.read("xl/sharedStrings.xml"))]
        cells = {c.attrib["r"]: c for c in ET.fromstring(workbook.read("xl/worksheets/sheet1.xml")).findall(".//s:c", ns)}
        assert strings[int(cells["C2"].find("s:v", ns).text)] == "001-020"
        assert strings[int(cells["D2"].find("s:v", ns).text)] == '=HYPERLINK("x")'
        assert cells["D2"].attrib["t"] == "s" and cells["D2"].find("s:f", ns) is None
    original_sha = sha(excel_path)
    newer = export_result(root, result_path)
    assert newer != excel_path and sha(excel_path) == original_sha
    assert sha(newer)  # independent output, not an overwrite
    (root / result["pages"][0]["fields"][0]["crop_path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="画像"):
        export_result(root, result_path)


def test_dimension_mismatch_does_not_start_engine(tmp_path):
    root, folder, args, calls = scenario(tmp_path)
    args["inspector"] = lambda _: [{"number": 1, "width_mm": 100.11, "height_mm": 100.}]
    code, path, excel = workflow.extract(root, folder, **args)
    assert code == 2 and excel.exists()
    assert calls["render"] == calls["session"] == calls["ocr"] == 0
    assert read_json(path)["pages"][0]["fields"][0]["status"] == "skipped"


def test_field_failure_continues_and_empty_is_not_detected(tmp_path):
    root, folder, args, calls = scenario(tmp_path, pages=1)
    successful = args["recognize"]
    def partly_failed(*a, **k):
        if "f0001" in k["crop_id"]:
            raise ValueError("malformed OCR payload")
        _, raw, geo = successful(*a, **k)
        return [], raw, geo
    args["recognize"] = partly_failed
    code, path, _ = workflow.extract(root, folder, **args)
    assert code == 2
    first, second = read_json(path)["pages"][0]["fields"]
    assert first["status"] == "failed"
    assert second["status"] == "not_detected" and second["value"] == ""
    assert calls["close"] == 1


@pytest.mark.parametrize("error", [OSError("disk full"), OcrInitializationError("GPU unavailable")])
def test_common_errors_stop_and_preserve_run(tmp_path, error):
    root, folder, args, calls = scenario(tmp_path)
    args["recognize"] = lambda *a, **k: (_ for _ in ()).throw(error)
    with pytest.raises(type(error)):
        workflow.extract(root, folder, **args)
    result_path = next((root / "data" / "r").glob("*/result.json"))
    assert read_json(result_path)["status"] == "failed"
    assert not list((root / "exports").glob("*/*.xlsx"))
    assert calls["render"] == calls["close"] == 1


def test_session_constructor_failure_is_common_error(tmp_path):
    root, folder, args, calls = scenario(tmp_path)
    args["session_factory"] = lambda _: (_ for _ in ()).throw(ValueError("model SHA mismatch"))
    with pytest.raises(OcrInitializationError, match="SHA"):
        workflow.extract(root, folder, **args)
    assert calls["render"] == 1


def test_source_failure_still_has_reasoned_excel_row(tmp_path):
    root, folder, args, calls = scenario(tmp_path)
    args["inspector"] = lambda _: (_ for _ in ()).throw(ValueError("password protected"))
    code, path, _ = workflow.extract(root, folder, **args)
    assert code == 2
    page = read_json(path)["pages"][0]
    assert page["number"] is None and page["fields"][0]["reason"] == "password protected"
    assert calls["session"] == 0


def test_reexecution_never_reuses_ocr_or_overwrites(tmp_path):
    root, folder, args, calls = scenario(tmp_path, pages=1)
    first = workflow.extract(root, folder, **args)
    first_sha = sha(first[1])
    second = workflow.extract(root, folder, **args)
    assert first[1] != second[1] and first[2] != second[2]
    assert calls["ocr"] == 4 and calls["session"] == 2
    assert sha(first[1]) == first_sha


def test_reexport_rejects_result_tampering(tmp_path):
    root, folder, args, _ = scenario(tmp_path, pages=1)
    _, path, _ = workflow.extract(root, folder, **args)
    payload = read_json(path)
    payload["pages"][0]["fields"][0]["value"] = "changed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="SHA"):
        export_result(root, path)


def test_workbook_failure_keeps_completed_ocr_reexportable(tmp_path, monkeypatch):
    import drawing_roi_ocr.excel as excel
    root, folder, args, calls = scenario(tmp_path, pages=1)
    original_export = excel.export_result
    monkeypatch.setattr(excel, "export_result", lambda *a: (_ for _ in ()).throw(OSError("workbook disk full")))
    with pytest.raises(OSError, match="workbook"):
        workflow.extract(root, folder, **args)
    path = next((root / "data" / "r").glob("*/result.json"))
    assert read_json(path)["status"] == "complete"
    assert (path.parent / "export-error.json").is_file()
    monkeypatch.setattr(excel, "export_result", original_export)
    assert export_result(root, path).is_file()
    assert calls["ocr"] == 2


def test_failed_new_template_cannot_silently_split_old_work(tmp_path, monkeypatch):
    from drawing_roi_ocr.storage import write_json, local_path
    root = workflow.ensure_layout(tmp_path / "app")
    old = root / "data" / "template-work" / "old" / "work.json"
    write_json(old, {"status": "complete", "editable_xdw": "data/old.xdw"})
    write_json(root / "data" / "last-template-work.json", {"work": "data/template-work/old/work.json"})
    sample = root / "見本" / "sample.pdf"
    sample.write_bytes(b"sample")
    monkeypatch.setattr(workflow, "inspect_pages", lambda _: [{"number": 1}])
    monkeypatch.setattr(workflow, "render_page", lambda *a: (_ for _ in ()).throw(ValueError("render failed")))
    with pytest.raises(ValueError, match="render"):
        workflow.create_template_work(root, sample, progress=lambda *a, **k: None)
    pointer = read_json(root / "data" / "last-template-work.json")
    assert pointer["work"] != "data/template-work/old/work.json"
    assert read_json(local_path(root, pointer["work"]))["status"] == "failed"
    with pytest.raises(ValueError, match="完了"):
        workflow.split_template_work(root, progress=lambda *a, **k: None)
