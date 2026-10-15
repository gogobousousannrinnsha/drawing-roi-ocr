"""Minimal OOXML fixtures exercise saved Excel variants without an Excel dependency."""
from copy import deepcopy
import hashlib
import xml.etree.ElementTree as ET
import zipfile

import pytest

from drawing_roi_ocr.excel import DETAIL_HEADERS, write_excel
from drawing_roi_ocr import xlsx_review as reader

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def letters(number):
    answer = ""
    while number:
        number, remainder = divmod(number-1, 26)
        answer = chr(65+remainder)+answer
    return answer


def cell(reference, value, *, formula=None, kind=None):
    element = ET.Element("{"+NS+"}c", {"r": reference})
    if formula is not None:
        ET.SubElement(element, "{"+NS+"}f").text = formula
        ET.SubElement(element, "{"+NS+"}v").text = "確認済み"
    elif kind == "s":
        element.set("t", "s")
        ET.SubElement(element, "{"+NS+"}v").text = str(value)
    elif isinstance(value, (int, float)) or kind == "n":
        ET.SubElement(element, "{"+NS+"}v").text = str(value)
    else:
        element.set("t", "inlineStr")
        inline = ET.SubElement(element, "{"+NS+"}is")
        ET.SubElement(inline, "{"+NS+"}t").text = str(value)
    return element


def row(number, values, formulas=None):
    element = ET.Element("{"+NS+"}row", {"r": str(number)})
    for column, value in enumerate(values, 1):
        element.append(cell(f"{letters(column)}{number}", value,
                            formula=(formulas or {}).get(column)))
    return element


def sheet(headers=None, values=None):
    root = ET.Element("{"+NS+"}worksheet")
    data = ET.SubElement(root, "{"+NS+"}sheetData")
    data.append(row(1, DETAIL_HEADERS if headers is None else headers))
    if values is not None:
        data.append(row(2, values))
    return root


def example():
    return {"原本ファイル": "日本語 drawing.xdw", "ページ": "1", "項目": "Title", "状態": "確認済み",
            "理由": "", "OCR原文": "first line\nsecond line", "信頼度": "0.98",
            "x_mm": 12.25, "y_mm": 18.5, "width_mm": 30.0, "height_mm": 4.0,
            "切り出し画像": "data/r/run/d0001p0001/f0001/c0001.png", "根拠ID": "p1:d0001:p1:f0001:r000001"}


def book(path, worksheet=None, *, strings=None, target="worksheets/renamed-review.xml", external=False,
         workbook_xml=None, extras=None):
    if worksheet is None:
        record = example()
        worksheet = sheet(values=[record[name] for name in DETAIL_HEADERS])
    workbook = ET.Element("{"+NS+"}workbook")
    sheets = ET.SubElement(workbook, "{"+NS+"}sheets")
    ET.SubElement(sheets, "{"+NS+"}sheet", {"name": "確認情報", "sheetId": "7", "{"+REL+"}id": "rId42"})
    rels = ET.Element("{"+PKG+"}Relationships")
    attrs = {"Id": "rId42", "Type": REL+"/worksheet", "Target": target}
    if external:
        attrs["TargetMode"] = "External"
    ET.SubElement(rels, "{"+PKG+"}Relationship", attrs)
    content = {"xl/workbook.xml": workbook_xml or ET.tostring(workbook, encoding="utf-8"),
               "xl/worksheets/renamed-review.xml": ET.tostring(worksheet, encoding="utf-8")}
    if strings is not None:
        sst = ET.Element("{"+NS+"}sst")
        for value in strings:
            item = ET.SubElement(sst, "{"+NS+"}si")
            ET.SubElement(item, "{"+NS+"}t").text = value
        content["xl/custom-strings.xml"] = ET.tostring(sst, encoding="utf-8")
        ET.SubElement(rels, "{"+PKG+"}Relationship", {"Id": "rId8", "Type": REL+"/sharedStrings", "Target": "custom-strings.xml"})
    content["xl/_rels/workbook.xml.rels"] = ET.tostring(rels, encoding="utf-8")
    content.update(extras or {})
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in content.items():
            archive.writestr(name, data)
    return path


def test_reads_actual_export_regions_blank_row_and_preserves_file(tmp_path):
    rect = {"x": 1.25, "y": 2.5, "width": 3.0, "height": 4.0}
    field = {"id": "f0001", "name": "_x0041_", "status": "recognized", "rect_mm": rect,
             "value": "a\nb", "crop_path": "data/r/run/c.png", "regions": [
                 {"id": "r1", "text": "a", "confidence": .99, "bbox_mm": rect},
                 {"id": "r2", "text": "b", "confidence": .95, "bbox_mm": rect}]}
    empty = {**field, "id": "f0002", "name": "Blank", "status": "not_detected", "value": "", "regions": []}
    result = {"template": {"fields": [field, empty]}, "pages": [{"source_name": "原本.xdw", "number": 1, "fields": [field, empty]}]}
    path = tmp_path/"actual.xlsx"
    write_excel(path, result)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    rows = reader.read_review_rows(path)
    assert [record["row"] for record in rows] == [2, 3, 4]
    assert [record["根拠ID"] for record in rows] == ["r1", "r2", ""]
    assert rows[0]["項目"] == "_x0041_"
    assert rows[2]["状態"] == "未検出" and rows[2]["x_mm"] == 1.25
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_column_order_sorted_rows_blank_insertions_and_numeric_cells(tmp_path):
    headers = list(reversed(DETAIL_HEADERS))
    record = example()
    record["ページ"], record["信頼度"] = 2, .98
    worksheet = sheet(headers)
    data = worksheet.find("{"+NS+"}sheetData")
    data.append(row(7, [record[name] for name in headers]))
    data.append(row(3, ["" for _ in headers]))
    record2 = {**record, "項目": "Scale", "根拠ID": "other"}
    data.append(row(4, [record2[name] for name in headers]))
    parsed = reader.read_review_rows(book(tmp_path/"sorted.xlsx", worksheet))
    assert [item["row"] for item in parsed] == [4, 7]
    assert parsed[0]["項目"] == "Scale" and parsed[1]["ページ"] == 2 and parsed[1]["信頼度"] == .98


def test_shared_rich_strings_and_inline_rich_text_exclude_phonetics(tmp_path):
    record = example()
    worksheet = sheet(values=[record[name] for name in DETAIL_HEADERS])
    cells = worksheet.find("{"+NS+"}sheetData").findall("{"+NS+"}row")[1]
    status = cells[3]
    status.clear()
    status.attrib.update(r="D2", t="s")
    ET.SubElement(status, "{"+NS+"}v").text = "0"
    original = cells[5]
    original.clear()
    original.attrib.update(r="F2", t="inlineStr")
    inline = ET.SubElement(original, "{"+NS+"}is")
    for text in ("first ", "second"):
        run = ET.SubElement(inline, "{"+NS+"}r")
        ET.SubElement(run, "{"+NS+"}t").text = text
    sst = ET.Element("{"+NS+"}sst")
    item = ET.SubElement(sst, "{"+NS+"}si")
    for tag, text in (("r", "確認"), ("rPh", "カクニン"), ("r", "済み")):
        run = ET.SubElement(item, "{"+NS+"}"+tag)
        ET.SubElement(run, "{"+NS+"}t").text = text
    path = book(tmp_path/"rich.xlsx", worksheet, strings=["unused"], extras={"xl/custom-strings.xml": ET.tostring(sst)})
    parsed = reader.read_review_rows(path)[0]
    assert parsed["状態"] == "確認済み" and parsed["OCR原文"] == "first second"


def test_excel_escaped_literals_and_surrogate_pairs(tmp_path):
    record = example()
    record["項目"] = "_x005F_x0041_"
    record["原本ファイル"] = "_xD83D__xDE00_.xdw"
    parsed = reader.read_review_rows(book(tmp_path/"escape.xlsx", sheet(values=[record[n] for n in DETAIL_HEADERS])))[0]
    assert parsed["項目"] == "_x0041_" and parsed["原本ファイル"] == "\U0001f600.xdw"


@pytest.mark.parametrize("headers", [DETAIL_HEADERS[:-1], DETAIL_HEADERS+["項目"]])
def test_missing_or_duplicate_required_headers_rejected(tmp_path, headers):
    with pytest.raises(ValueError, match="見出し"):
        reader.read_review_rows(book(tmp_path/"headers.xlsx", sheet(headers)))


def test_duplicate_cell_rejected(tmp_path):
    worksheet = sheet(values=list(example().values()))
    data = worksheet.find("{"+NS+"}sheetData")
    data[1].append(deepcopy(data[1][0]))
    with pytest.raises(ValueError, match="重複"):
        reader.read_review_rows(book(tmp_path/"duplicate.xlsx", worksheet))


def test_duplicate_row_rejected(tmp_path):
    worksheet = sheet(values=list(example().values()))
    data = worksheet.find("{"+NS+"}sheetData")
    data.append(deepcopy(data[1]))
    with pytest.raises(ValueError, match="重複"):
        reader.read_review_rows(book(tmp_path/"duplicate-row.xlsx", worksheet))


@pytest.mark.parametrize("name", [name for name in DETAIL_HEADERS if name not in {"OCR原文", "理由"}])
def test_formula_in_selection_identity_or_geometry_rejected(tmp_path, name):
    worksheet = sheet(values=list(example().values()))
    index = DETAIL_HEADERS.index(name)
    target = worksheet.find("{"+NS+"}sheetData")[1]
    target.remove(target[index])
    target.insert(index, cell(f"{letters(index+1)}2", "", formula='"確認済み"'))
    with pytest.raises(ValueError, match="数式"):
        reader.read_review_rows(book(tmp_path/"formula.xlsx", worksheet))


def test_formula_in_ignored_text_is_not_evaluated_or_returned(tmp_path):
    worksheet = sheet(values=list(example().values()))
    target = worksheet.find("{"+NS+"}sheetData")[1]
    for index in (4, 5):
        target.remove(target[index])
        target.insert(index, cell(f"{letters(index+1)}2", "", formula='HYPERLINK("https://example.invalid", "text")'))
    parsed = reader.read_review_rows(book(tmp_path/"ignored.xlsx", worksheet))[0]
    assert parsed["理由"] == parsed["OCR原文"] == "" and parsed["状態"] == "確認済み"


@pytest.mark.parametrize("target,external", [("https://example.invalid/sheet.xml", True), ("../escape.xml", False)])
def test_external_or_escaping_sheet_relation_rejected(tmp_path, target, external):
    with pytest.raises(ValueError):
        reader.read_review_rows(book(tmp_path/"external.xlsx", target=target, external=external))


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_doctype_and_entities_rejected_including_utf16(tmp_path, encoding):
    xml = f'<?xml version="1.0" encoding="{encoding}"?><!DOCTYPE workbook [<!ENTITY x "x">]><workbook xmlns="{NS}"/>'
    with pytest.raises(ValueError, match="DOCTYPE"):
        reader.read_review_rows(book(tmp_path/"dtd.xlsx", workbook_xml=xml.encode(encoding)))


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE_BYTES", "MAX_UNCOMPRESSED_BYTES", "MAX_XML_BYTES", "MAX_MEMBERS"])
def test_resource_limits_reject_before_processing(tmp_path, monkeypatch, limit):
    path = book(tmp_path/"large.xlsx")
    monkeypatch.setattr(reader, limit, 1)
    with pytest.raises(ValueError):
        reader.read_review_rows(path)


def test_duplicate_zip_members_rejected(tmp_path):
    path = book(tmp_path/"duplicate-parts.xlsx")
    with zipfile.ZipFile(path, "a") as archive, pytest.warns(UserWarning):
        archive.writestr("xl/workbook.xml", "duplicate")
    with pytest.raises(ValueError, match="重複"):
        reader.read_review_rows(path)


def test_shared_string_out_of_range_rejected(tmp_path):
    worksheet = sheet(values=list(example().values()))
    cells = worksheet.find("{"+NS+"}sheetData")[1]
    cells.remove(cells[3])
    cells.insert(3, cell("D2", 99, kind="s"))
    with pytest.raises(ValueError, match="共有文字列"):
        reader.read_review_rows(book(tmp_path/"index.xlsx", worksheet, strings=["確認済み"]))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e9999"])
def test_invalid_numeric_cells_rejected(tmp_path, value):
    worksheet = sheet(values=list(example().values()))
    cells = worksheet.find("{"+NS+"}sheetData")[1]
    cells.remove(cells[7])
    cells.insert(7, cell("H2", value, kind="n"))
    with pytest.raises(ValueError, match="数値"):
        reader.read_review_rows(book(tmp_path/"number.xlsx", worksheet))


def test_absolute_internal_sheet_target_supported(tmp_path):
    assert reader.read_review_rows(book(tmp_path/"absolute.xlsx", target="/xl/worksheets/renamed-review.xml"))[0]["状態"] == "確認済み"


def test_formula_header_rejected(tmp_path):
    worksheet = sheet(values=list(example().values()))
    header = worksheet.find("{"+NS+"}sheetData")[0]
    header.remove(header[3])
    header.insert(3, cell("D1", "", formula='"状態"'))
    with pytest.raises(ValueError, match="見出し.*数式"):
        reader.read_review_rows(book(tmp_path/"header-formula.xlsx", worksheet))


def test_workbook_external_references_rejected(tmp_path):
    workbook = ET.Element("{"+NS+"}workbook")
    sheets = ET.SubElement(workbook, "{"+NS+"}sheets")
    ET.SubElement(sheets, "{"+NS+"}sheet", {"name": "確認情報", "{"+REL+"}id": "rId42"})
    ET.SubElement(workbook, "{"+NS+"}externalReferences")
    with pytest.raises(ValueError, match="外部ブック"):
        reader.read_review_rows(book(tmp_path/"external-book.xlsx", workbook_xml=ET.tostring(workbook)))


def test_duplicate_sheet_data_rejected(tmp_path):
    worksheet = sheet(values=list(example().values()))
    worksheet.append(deepcopy(worksheet[0]))
    with pytest.raises(ValueError):
        reader.read_review_rows(book(tmp_path/"data-parts.xlsx", worksheet))


def test_omitted_row_and_cell_references_are_inferred(tmp_path):
    worksheet = sheet(values=list(example().values()))
    for element in worksheet.iter():
        element.attrib.pop("r", None)
    parsed = reader.read_review_rows(book(tmp_path/"inferred.xlsx", worksheet))
    assert parsed[0]["row"] == 2 and parsed[0]["状態"] == "確認済み"


def test_text_columns_keep_the_string_api_for_numeric_excel_cells(tmp_path):
    values = example()
    values["項目"] = 123
    values["理由"] = 7
    parsed = reader.read_review_rows(book(tmp_path/"text-types.xlsx", sheet(values=list(values.values()))))[0]
    assert parsed["項目"] == "123" and parsed["理由"] == "7"
