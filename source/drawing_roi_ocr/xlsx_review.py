"""Read review selections from an XLSX file without evaluating Excel content."""
from __future__ import annotations

import math
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import xml.etree.ElementTree as ET
import zipfile

from .excel import DETAIL_HEADERS

MAX_ARCHIVE_BYTES = 64 * 1024**2
MAX_UNCOMPRESSED_BYTES = 256 * 1024**2
MAX_XML_BYTES = 64 * 1024**2
MAX_MEMBERS = 2048
MAX_ROWS = 200_000
MAX_CELLS = 2_000_000
MAX_SHARED_STRINGS = 1_000_000
MAX_STRING_CHARS = 16_000_000
SHEET_NAMESPACES = {"http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                    "http://purl.oclc.org/ooxml/spreadsheetml/main"}
DOCUMENT_RELS = {"http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                 "http://purl.oclc.org/ooxml/officeDocument/relationships"}
PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
IGNORED_FORMULA_COLUMNS = {"OCR原文", "理由"}
TEXT_COLUMNS = set(DETAIL_HEADERS)-{"ページ", "信頼度", "x_mm", "y_mm", "width_mm", "height_mm"}
CELL_REFERENCE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})\Z")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?\Z")


def _xml(archive, name):
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError(f"Excelの必要な部品がありません: {name}") from exc
    if info.file_size > MAX_XML_BYTES:
        raise ValueError(f"ExcelのXMLが読取り上限を超えています: {name}")
    with archive.open(info) as stream:
        data = stream.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise ValueError("ExcelのXMLが読取り上限を超えています。")
    # Removing NULs for this check also detects UTF-16/32 DTD declarations.
    if re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", data.replace(b"\x00", b""), re.I):
        raise ValueError("DOCTYPE・ENTITYを含むExcelは読み込めません。")
    try:
        return ET.fromstring(data)
    except (ET.ParseError, LookupError, UnicodeError) as exc:
        raise ValueError(f"ExcelのXMLを読み込めません: {name}") from exc


def _namespace(element, expected):
    if not element.tag.startswith("{"):
        raise ValueError("ExcelのXML名前空間が不正です。")
    namespace, name = element.tag[1:].split("}", 1)
    if namespace not in SHEET_NAMESPACES or name != expected:
        raise ValueError(f"Excelの{expected}部品が不正です。")
    return "{" + namespace + "}"


def _target(relationship):
    if relationship.get("TargetMode", "Internal").casefold() != "internal":
        raise ValueError("外部を参照するExcelのシート・文字列部品は読み込めません。")
    target = relationship.get("Target", "")
    if not target or any(char in target for char in ("\\", ":", "?", "#", "\x00")):
        raise ValueError("Excelの部品参照先が不正です。")
    name = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
    if not name.startswith("xl/") or name == "xl/":
        raise ValueError("Excelの部品参照先が範囲外です。")
    return name


def _unescape(text):
    # One pass preserves Excel's escaped literal forms such as _x005F_x0041_.
    text = re.sub(r"_x([0-9a-fA-F]{4})_", lambda match: chr(int(match[1], 16)), text)
    try:
        return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    except UnicodeError as exc:
        raise ValueError("Excelに不正なUnicode文字があります。") from exc


def _rich_text(element, ns):
    if element is None:
        return ""
    # Phonetic rPh/t nodes are annotations, not part of the actual cell value.
    pieces = []
    for child in element:
        if child.tag == ns + "t":
            pieces.append(child.text or "")
        elif child.tag == ns + "r":
            pieces.extend(item.text or "" for item in child.findall(ns + "t"))
    return _unescape("".join(pieces))


def _shared_strings(archive, relationships):
    matches = [rel for rel in relationships.values() if rel.get("Type", "").endswith("/sharedStrings")]
    if len(matches) > 1:
        raise ValueError("Excelの共有文字列部品が重複しています。")
    if not matches:
        return []
    root = _xml(archive, _target(matches[0]))
    ns = _namespace(root, "sst")
    values, characters = [], 0
    for item in root.findall(ns + "si"):
        value = _rich_text(item, ns)
        characters += len(value)
        if len(values) >= MAX_SHARED_STRINGS or characters > MAX_STRING_CHARS:
            raise ValueError("Excelの共有文字列が読取り上限を超えています。")
        values.append(value)
    return values


def _cell(cell, ns, strings):
    formulas = cell.findall(ns + "f")
    if formulas:
        return "", True
    kind = cell.get("t", "n")
    values = cell.findall(ns + "v")
    if len(values) > 1:
        raise ValueError("Excelのセル値が重複しています。")
    raw = "" if not values or values[0].text is None else values[0].text
    if kind == "s":
        if not re.fullmatch(r"[0-9]+", raw) or int(raw) >= len(strings):
            raise ValueError("Excelの共有文字列番号が不正です。")
        return strings[int(raw)], False
    if kind == "inlineStr":
        items = cell.findall(ns + "is")
        if len(items) > 1:
            raise ValueError("Excelのセル値が重複しています。")
        return _rich_text(items[0] if items else None, ns), False
    if kind in ("str", "d", "e"):
        return _unescape(raw), False
    if kind == "b":
        if raw not in ("0", "1"):
            raise ValueError("Excelの真偽値が不正です。")
        return "TRUE" if raw == "1" else "FALSE", False
    if kind != "n":
        raise ValueError("Excelのセル型に対応していません。")
    raw = raw.strip()
    if not raw:
        return "", False
    if not NUMBER.fullmatch(raw):
        raise ValueError("Excelの数値が不正です。")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("Excelの数値には有限の値が必要です。")
    return (int(raw) if re.fullmatch(r"[+-]?[0-9]+", raw) else value), False


def _rows(sheet, ns, strings):
    result, seen_rows, seen_cells, previous = [], set(), set(), 0
    data_parts = sheet.findall(ns + "sheetData")
    if len(data_parts) != 1:
        raise ValueError("Excelの確認情報シートにデータがありません。")
    data = data_parts[0]
    for row in data.findall(ns + "row"):
        raw = row.get("r", str(previous + 1))
        if not re.fullmatch(r"[1-9][0-9]{0,6}", raw):
            raise ValueError("Excelの行番号が不正です。")
        number = int(raw)
        if number > 1_048_576 or number in seen_rows:
            raise ValueError("Excelの行番号が範囲外または重複しています。")
        seen_rows.add(number)
        if len(seen_rows) > MAX_ROWS:
            raise ValueError("Excelの行数が読取り上限を超えています。")
        previous, last_column, values = number, 0, {}
        for cell in row.findall(ns + "c"):
            reference = cell.get("r")
            if reference is None:
                column = last_column + 1
            else:
                match = CELL_REFERENCE.fullmatch(reference)
                if match is None or int(match[2]) != number:
                    raise ValueError("Excelのセル参照が不正です。")
                column = 0
                for char in match[1]:
                    column = column*26 + ord(char)-64
            if column > 16384 or (number, column) in seen_cells:
                raise ValueError("Excelのセルが範囲外または重複しています。")
            seen_cells.add((number, column))
            if len(seen_cells) > MAX_CELLS:
                raise ValueError("Excelのセル数が読取り上限を超えています。")
            last_column = column
            values[column] = _cell(cell, ns, strings)
        if any(value != "" or formula for value, formula in values.values()):
            result.append((number, values))
    return sorted(result)


def read_review_rows(path):
    """Return literal review rows keyed by header, preserving physical Excel row numbers."""
    path = Path(path)
    if path.suffix.casefold() != ".xlsx" or path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("64 MiB以内の.xlsxファイルを選んでください。")
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [entry.filename for entry in infos]
            if len(infos) > MAX_MEMBERS or len(names) != len(set(names)):
                raise ValueError("ExcelのZIP部品が上限超過または重複しています。")
            if sum(entry.file_size for entry in infos) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Excelの展開容量が読取り上限を超えています。")
            for entry in infos:
                name = PurePosixPath(entry.filename)
                mode = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.flag_bits & 1 or name.is_absolute() or ".." in name.parts or
                        any(char in entry.filename for char in ("\\", ":", "\x00")) or
                        mode not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise ValueError("安全に読み込めないExcelのZIP部品があります。")
            workbook = _xml(archive, "xl/workbook.xml")
            ns = _namespace(workbook, "workbook")
            sheets = workbook.findall(ns + "sheets/" + ns + "sheet")
            selected = [sheet for sheet in sheets if sheet.get("name") == "確認情報"]
            if len(selected) != 1:
                raise ValueError("確認情報シートがないか、重複しています。")
            if workbook.find(ns + "externalReferences") is not None:
                raise ValueError("外部ブック参照を含むExcelは読み込めません。")
            rels_root = _xml(archive, "xl/_rels/workbook.xml.rels")
            if rels_root.tag != "{" + PACKAGE_RELS + "}Relationships":
                raise ValueError("Excelの部品参照情報が不正です。")
            relationships = {}
            for rel in rels_root.findall("{" + PACKAGE_RELS + "}Relationship"):
                identity = rel.get("Id")
                if not identity or identity in relationships:
                    raise ValueError("Excelの部品参照IDがないか重複しています。")
                relationships[identity] = rel
            identities = [selected[0].get("{" + uri + "}id") for uri in DOCUMENT_RELS]
            identities = [identity for identity in identities if identity is not None]
            rel = relationships.get(identities[0]) if len(identities) == 1 else None
            if rel is None or not rel.get("Type", "").endswith("/worksheet"):
                raise ValueError("確認情報シートの参照先が不正です。")
            sheet = _xml(archive, _target(rel))
            sheet_ns = _namespace(sheet, "worksheet")
            rows = _rows(sheet, sheet_ns, _shared_strings(archive, relationships))
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError("ExcelのZIPを読み込めません。通常の.xlsxで保存してください。") from exc
    if not rows:
        raise ValueError("確認情報シートに見出しがありません。")
    _, headers = rows.pop(0)
    columns = {}
    for column, (value, formula) in headers.items():
        if formula:
            raise ValueError("Excelの見出しに数式は使用できません。")
        if value in DETAIL_HEADERS:
            if value in columns:
                raise ValueError(f"確認情報の見出しが重複しています: {value}")
            columns[value] = column
    missing = set(DETAIL_HEADERS)-columns.keys()
    if missing:
        raise ValueError("確認情報に必要な見出しがありません: " + ", ".join(sorted(missing)))
    records = []
    for number, cells in rows:
        record = {"row": number}
        for name in DETAIL_HEADERS:
            value, formula = cells.get(columns[name], ("", False))
            if formula and name not in IGNORED_FORMULA_COLUMNS:
                raise ValueError(f"確認情報の{number}行「{name}」に数式は使用できません。")
            record[name] = "" if formula else str(value) if name in TEXT_COLUMNS else value
        if any(record[name] != "" for name in DETAIL_HEADERS):
            records.append(record)
    return records
