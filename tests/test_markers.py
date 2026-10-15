from copy import deepcopy
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
import zipfile

import pytest

from drawing_roi_ocr.excel import write_excel
from drawing_roi_ocr.markers import mark_confirmed, workbook_files
from drawing_roi_ocr.storage import read_json, sha, write_json
from drawing_roi_ocr.workflow import ensure_layout

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def edit_cells(path, edits):
    with zipfile.ZipFile(path) as z:
        contents = {name: z.read(name) for name in z.namelist()}
    sheet = ET.fromstring(contents["xl/worksheets/sheet2.xml"])
    cells = {cell.attrib["r"]: cell for cell in sheet.findall(".//s:c", NS)}
    for address, value in edits.items():
        if address not in cells:
            number = int("".join(c for c in address if c.isdecimal()))
            row = sheet.find(f"s:sheetData/s:row[@r='{number}']", NS)
            cell = ET.SubElement(row, "{" + NS["s"] + "}c", {"r": address})
        else:
            cell = cells[address]
            cell.clear()
            cell.set("r", address)
        cell.set("t", "inlineStr")
        inline = ET.SubElement(cell, "{" + NS["s"] + "}is")
        ET.SubElement(inline, "{" + NS["s"] + "}t").text = value
    contents["xl/worksheets/sheet2.xml"] = ET.tostring(sheet, encoding="utf-8", xml_declaration=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, content in contents.items():
            z.writestr(name, content)


def fixture(tmp_path, *, legacy=False):
    root = ensure_layout(tmp_path / "app")
    run = root / "data" / ("runs" if legacy else "r") / "fixture-run"
    run.mkdir(parents=True)
    rect = {"x": 10., "y": 20., "width": 30., "height": 8.}
    docs, pages = [], []
    for i, extension in enumerate((".pdf", ".xdw"), 1):
        doc_id = f"d{i:04d}"
        source = run / (doc_id + extension)
        source.write_bytes(b"authored immutable " + extension.encode())
        docs.append({"id": doc_id, "name": "sample" + extension, "sha256": sha(source),
                     "source_path": source.relative_to(root).as_posix()})
        fields = []
        for j, name in enumerate(("Title", "Blank"), 1):
            crop = run / f"{doc_id}f{j}.png"
            crop.write_bytes(b"authored crop")
            fields.append({"id": f"f{j:04d}", "name": name, "rect_mm": rect,
                "status": "recognized" if j == 1 else "not_detected", "reason": "", "value": "001" if j == 1 else "",
                "regions": [{"id": f"{doc_id}:r1", "bbox_mm": rect, "text": "001", "confidence": 0.9}] if j == 1 else [],
                "crop_path": crop.relative_to(root).as_posix(), "crop_sha256": sha(crop)})
        pages.append({"document_id": doc_id, "source_name": "sample" + extension,
                      "number": 1, "status": "complete", "fields": fields})
    result = {"schema": "drawing-roi-result-v1", "status": "complete", "documents": docs, "pages": pages,
              "template": {"fields": [{"id": "f0001", "name": "Title"}, {"id": "f0002", "name": "Blank"}]}}
    result_path = run / "result.json"
    write_json(result_path, result)
    write_json(run / "receipt.json", {"result_sha256": sha(result_path)})
    excel = root / "exports/e/抽出結果.xlsx"
    write_excel(excel, result)
    write_json(excel.parent / "export.json", {"result": result_path.relative_to(root).as_posix(), "result_sha256": sha(result_path)})
    calls = []
    def annotate(app, source, destination, rectangles_by_page, **kwargs):
        calls.append((Path(source).name, deepcopy(rectangles_by_page)))
        destination.write_bytes(b"authored verified marked document")
        return {"marker_count": 2, "output_path": str(destination)}
    return root, result_path, excel, calls, annotate


@pytest.mark.parametrize("legacy", [False, True])
def test_only_confirmed_rows_bind_saved_evidence_without_changing_inputs(tmp_path, legacy):
    root, saved, excel, calls, annotate = fixture(tmp_path, legacy=legacy)
    edit_cells(excel, {"D2": "確認済み", "D5": "確認済み", "F2": "Excel内だけで訂正"})
    protected = [p for p in (root / "data").rglob("*") if p.is_file()] + [excel]
    before = {p: sha(p) for p in protected}
    output = mark_confirmed(root, excel, annotator=annotate, progress=lambda *a, **k: None)
    report = read_json(output / "markers.json")
    assert report["confirmed_rows"] == report["unique_rectangles"] == 2
    assert [c[0] for c in calls] == ["d0001.pdf", "d0002.xdw"]
    assert [d["output"] for d in report["documents"]] == ["d0001_sample.xdw", "d0002_sample.xdw"]
    assert report["selected"][0]["region_id"] == "d0001:r1"
    assert report["selected"][1]["region_id"] == ""  # confirmed empty field: whole ROI
    assert all(sha(p) == digest for p, digest in before.items())
    assert sha(output / "review.xlsx") == sha(excel)
    assert all(not d["verification"]["output_path"].startswith(str(root)) for d in report["documents"])
    # Re-run always starts from frozen originals and creates an independent folder.
    again = mark_confirmed(root, excel, annotator=annotate, progress=lambda *a, **k: None)
    assert again != output and sha(output / "review.xlsx") == sha(excel)
    assert workbook_files(root) == [excel]


def test_renamed_external_copy_without_sidecar_uses_crop_run_reference(tmp_path):
    root, saved, excel, calls, annotate = fixture(tmp_path)
    edited = tmp_path / "確認 日本語.xlsx"
    shutil.copyfile(excel, edited)
    edit_cells(edited, {"D2": "確認済み"})
    output = mark_confirmed(root, edited, annotator=annotate, progress=lambda *a, **k: None)
    assert read_json(output / "markers.json")["source_result"] == saved.relative_to(root).as_posix()


def test_sorted_and_duplicate_review_rows_do_not_duplicate_markers(tmp_path, monkeypatch):
    from drawing_roi_ocr import xlsx_review
    root, saved, excel, calls, annotate = fixture(tmp_path)
    edit_cells(excel, {"D2": "確認済み"})
    rows = xlsx_review.read_review_rows(excel)
    duplicated = {**rows[0], "row": 99}
    monkeypatch.setattr(xlsx_review, "read_review_rows", lambda _: [*reversed(rows), duplicated])
    output = mark_confirmed(root, excel, annotator=annotate, progress=lambda *a, **k: None)
    report = read_json(output / "markers.json")
    assert report["confirmed_rows"] == 2 and report["unique_rectangles"] == report["duplicate_rows"] == 1
    assert len(calls) == 1 and len(calls[0][1][1]) == 1
    assert report["selected"][0]["rows"] == [2, 99]


@pytest.mark.parametrize("edits", [{"H2": "999"}, {"B2": "2"}, {"M2": "foreign-id"},
    {"L2": "data/r/foreign/crop.png"}, {"A2": "other.pdf"}, {"C2": "other field"}])
def test_bad_identifiers_or_coordinates_never_reach_writer(tmp_path, edits):
    root, saved, excel, calls, annotate = fixture(tmp_path)
    edit_cells(excel, {"D2": "確認済み", **edits})
    with pytest.raises(ValueError):
        mark_confirmed(root, excel, annotator=annotate)
    assert not calls and not list((root / "exports").glob("m*"))


@pytest.mark.parametrize("target", ["result", "source", "crop"])
def test_tampered_saved_evidence_is_rejected(tmp_path, target):
    root, saved, excel, calls, annotate = fixture(tmp_path)
    edit_cells(excel, {"D2": "確認済み"})
    result = read_json(saved)
    corrupt = {"result": saved, "source": root / result["documents"][0]["source_path"],
               "crop": root / result["pages"][0]["fields"][0]["crop_path"]}[target]
    corrupt.write_bytes(b"changed")
    with pytest.raises(ValueError):
        mark_confirmed(root, excel, annotator=annotate)
    assert not calls


def test_no_confirmed_rows_create_nothing(tmp_path):
    root, saved, excel, calls, annotate = fixture(tmp_path)
    with pytest.raises(ValueError, match="確認済み"):
        mark_confirmed(root, excel, annotator=annotate)
    assert not calls and not list((root / "exports").glob("m*"))


def test_later_document_failure_publishes_no_partial_folder(tmp_path):
    root, saved, excel, calls, annotate = fixture(tmp_path)
    edit_cells(excel, {"D2": "確認済み", "D4": "確認済み"})
    protected = sha(excel), sha(saved)
    def fail(app, source, destination, rectangles_by_page, **kwargs):
        if source.suffix == ".xdw":
            raise RuntimeError("authored SDK save failure")
        return annotate(app, source, destination, rectangles_by_page, **kwargs)
    with pytest.raises(RuntimeError, match="SDK"):
        mark_confirmed(root, excel, annotator=fail, progress=lambda *a, **k: None)
    assert len(calls) == 1
    assert not list((root / "exports").glob("m*")) and not list((root / "exports").glob("s-*"))
    assert (sha(excel), sha(saved)) == protected


def test_workbook_changed_during_writing_does_not_publish(tmp_path):
    root, saved, excel, calls, annotate = fixture(tmp_path)
    edit_cells(excel, {"D2": "確認済み"})
    def changing(*args, **kwargs):
        result = annotate(*args, **kwargs)
        edit_cells(excel, {"D2": "取得・未確認"})
        return result
    with pytest.raises(ValueError, match="処理中にExcel"):
        mark_confirmed(root, excel, annotator=changing, progress=lambda *a, **k: None)
    assert not list((root / "exports").glob("m*"))
