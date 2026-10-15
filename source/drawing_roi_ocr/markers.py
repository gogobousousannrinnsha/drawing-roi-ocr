"""Mark reviewed Excel rows on new documents, using frozen extraction evidence."""
from __future__ import annotations

from collections import defaultdict
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from .storage import local_path, new_id, now, read_json, relative, require_short_path, sha, write_json
from . import __version__

CONFIRMED = "確認済み"
COORDINATES = ("x", "y", "width", "height")


def workbook_files(root):
    """Edited copies may be renamed, but Excel lock files are never inputs."""
    return sorted((p for p in (Path(root) / "exports").rglob("*.xlsx")
                   if p.is_file() and not p.name.startswith("~$") and not (p.parent / "markers.json").is_file()),
                  key=lambda p: (p.stat().st_mtime_ns, str(p)), reverse=True)


def _run_from_crop(value):
    text = str(value or "").replace("\\", "/")
    parts = PurePosixPath(text).parts
    if len(parts) < 4 or parts[0] != "data" or parts[1] not in ("r", "runs") or ".." in parts:
        raise ValueError("切り出し画像の保存パスが不正です。元のExcelで状態列だけを変更してください。")
    return "/".join(parts[:3]) + "/result.json"


def _saved_result(root, workbook, rows, requested=None):
    references = {_run_from_crop(row["切り出し画像"]) for row in rows if row.get("切り出し画像")}
    if len(references) > 1:
        raise ValueError("複数の抽出実行の行が混在しています。1回の出力Excelごとに処理してください。")
    receipt = None
    sidecar = workbook.parent / "export.json"
    if sidecar.is_file():
        receipt = read_json(sidecar)
        references.add(str(receipt["result"]).replace("\\", "/"))
    if requested is not None:
        references.add(relative(root, local_path(root, requested)))
    if len(references) != 1:
        raise ValueError("Excelに対応する保存結果を特定できません。元のexportsフォルダーで処理してください。")
    result_path = local_path(root, references.pop())
    if (result_path.name != "result.json" or not any(result_path.is_relative_to(Path(root) / "data" / name)
                                                    for name in ("r", "runs"))):
        raise ValueError("保存結果がこのアプリのdata内にありません。")
    result_hash = sha(result_path)
    seal = read_json(result_path.parent / "receipt.json")
    if seal["result_sha256"] != result_hash or receipt is not None and receipt["result_sha256"] != result_hash:
        raise ValueError("保存結果のSHAが一致しません。処理を中止しました。")
    result = read_json(result_path)
    if result.get("schema") != "drawing-roi-result-v1" or result.get("status") not in ("complete", "partial"):
        raise ValueError("完了または一部失敗の保存結果だけを使用できます。")
    return result_path, result_hash, result


def _page_number(value):
    if isinstance(value, bool):
        raise ValueError("ページ番号が不正です。")
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError("ページ番号が不正です。") from None
    if not math.isfinite(number) or not number.is_integer() or number < 1:
        raise ValueError("ページ番号が不正です。")
    return int(number)


def _canonical_rows(result):
    index = {}
    for page in result["pages"]:
        if page.get("number") is None:
            continue
        for field in page["fields"]:
            for region in field.get("regions", []) or [None]:
                key = (page["source_name"], int(page["number"]), field["name"],
                       field.get("crop_path", "").replace("\\", "/"), region["id"] if region else "")
                if key in index:
                    raise ValueError("保存結果の根拠IDが重複しています。")
                index[key] = (page, field, region)
    return index


def _selected_rows(root, rows, result):
    index = _canonical_rows(result)
    selected = {}
    row_count = 0
    for row in rows:
        if str(row.get("状態", "")).strip() != CONFIRMED:
            continue
        row_count += 1
        label = f"確認情報の{row['row']}行"
        try:
            key = (str(row["原本ファイル"]), _page_number(row["ページ"]), str(row["項目"]),
                   str(row.get("切り出し画像", "")).replace("\\", "/"), str(row.get("根拠ID", "")))
            if key not in index:
                raise ValueError("原本・ページ・項目・切り出し画像・根拠IDが保存結果と一致しません。")
            page, field, region = index[key]
            if (page.get("status") not in ("complete", "partial") or
                    field["status"] not in ("recognized", "not_detected") or not field.get("crop_path")):
                raise ValueError("読み取り画像が完成していない行には付与できません。")
            rect = dict(region["bbox_mm"] if region else field["rect_mm"])
            for coordinate in COORDINATES:
                actual = row[coordinate + "_mm"]
                if isinstance(actual, bool):
                    raise ValueError("座標が不正です。")
                actual = float(actual)
                if not math.isfinite(actual) or abs(actual - rect[coordinate]) > 0.00011:
                    raise ValueError("座標が保存結果と一致しません。状態列だけを変更してください。")
            crop = local_path(root, field["crop_path"])
            if sha(crop) != field["crop_sha256"]:
                raise ValueError("切り出し画像のSHAが一致しません。")
            if key not in selected:
                selected[key] = {"document_id": page["document_id"], "page": page["number"],
                    "source_name": page["source_name"], "field": field["name"], "region_id": key[-1],
                    "rect_mm": rect, "rows": []}
            selected[key]["rows"].append(row["row"])
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise ValueError(f"{label}: {exc}") from exc
    if not row_count:
        raise ValueError("状態が「確認済み」の行がありません。Excelを保存してから実行してください。")
    return list(selected.values()), row_count


def mark_confirmed(root, workbook, *, result_reference=None, annotator=None, progress=print):
    """No OCR, Excel writeback, or editing of frozen results is performed."""
    from .xlsx_review import read_review_rows
    if annotator is None:
        from .markers_xdw import annotate_document
        annotator = annotate_document
    root = Path(root).resolve()
    workbook = Path(workbook)
    workbook = require_short_path(workbook if workbook.is_absolute() else root / workbook, label="確認Excel")
    if workbook.suffix.lower() != ".xlsx" or not workbook.is_file() or workbook.name.startswith("~$"):
        raise ValueError("保存済みの.xlsxファイルを指定してください。")
    workbook_hash = sha(workbook)
    rows = read_review_rows(workbook)
    result_path, result_hash, result = _saved_result(root, workbook, rows, result_reference)
    selected, confirmed_count = _selected_rows(root, rows, result)
    documents = {doc["id"]: doc for doc in result["documents"]}
    groups = defaultdict(lambda: defaultdict(list))
    for item in selected:
        groups[item["document_id"]][item["page"]].append(item["rect_mm"])
    inputs = {}
    for doc_id in groups:
        if doc_id not in documents or not re.fullmatch(r"d[0-9]{4}", doc_id):
            raise ValueError("保存結果の原本対応がありません。")
        doc = documents[doc_id]
        source = local_path(root, doc["source_path"])
        if sha(source) != doc["sha256"]:
            raise ValueError(f"保存原本のSHAが一致しません: {doc['name']}")
        inputs[doc_id] = source
    output = require_short_path(root / "exports" / ("m" + new_id()), label="マーカー出力先")
    if os.path.lexists(output):
        raise FileExistsError("既存の出力フォルダーは上書きしません。")
    output.parent.mkdir(parents=True, exist_ok=True)
    progress(f"確認済み{confirmed_count}行・{len(selected)}範囲・{len(groups)}文書を処理します。", flush=True)
    with tempfile.TemporaryDirectory(prefix="s-", dir=output.parent) as stage_name:
        stage = Path(stage_name)
        reviewed_copy = stage / "review.xlsx"
        with workbook.open("rb") as incoming, reviewed_copy.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing)
        if sha(reviewed_copy) != workbook_hash or sha(workbook) != workbook_hash:
            raise ValueError("処理中にExcelが変更されました。保存して閉じてから再実行してください。")
        outputs = []
        for doc_id, pages in groups.items():
            doc = documents[doc_id]
            name = doc_id + "_" + Path(doc["name"]).stem + ".xdw"
            try:
                require_short_path(output / name)
                destination = require_short_path(stage / name)
            except ValueError:
                name = doc_id + ".xdw"
                destination = require_short_path(stage / name)
            progress(f"黄色マーカーを作成・保存・再確認しています: {doc['name']}", flush=True)
            verified = annotator(root, inputs[doc_id], destination, dict(pages), progress=progress)
            if not destination.is_file() or sha(inputs[doc_id]) != doc["sha256"]:
                raise ValueError("保存原本の保持または出力の検査に失敗しました。")
            verified = {**verified, "output_path": name}
            outputs.append({"document_id": doc_id, "source_name": doc["name"],
                "source_sha256": doc["sha256"], "output": name, "sha256": sha(destination),
                "rectangle_count": sum(map(len, pages.values())), "verification": verified})
        if sha(workbook) != workbook_hash or sha(result_path) != result_hash:
            raise ValueError("処理中にExcelまたは保存結果が変更されました。")
        # Check every source again before publishing the complete output folder.
        if any(sha(path) != documents[doc_id]["sha256"] for doc_id, path in inputs.items()):
            raise ValueError("処理中に保存原本が変更されました。")
        report = {"schema": "drawing-roi-confirmed-markers-v1", "app_version": __version__, "created_at": now(), "status": "complete",
            "source_result": relative(root, result_path), "source_result_sha256": result_hash,
            "reviewed_excel": "review.xlsx", "reviewed_excel_name": workbook.name,
            "reviewed_excel_sha256": workbook_hash, "confirmed_rows": confirmed_count,
            "unique_rectangles": len(selected), "duplicate_rows": confirmed_count - len(selected),
            "selected": selected, "documents": outputs}
        write_json(stage / "markers.json", report)
        lines = [f"確認済み {confirmed_count}行 / {len(selected)}範囲", "原本とExcelは変更していません。", ""]
        lines.extend(doc["source_name"] + " → " + doc["output"] for doc in outputs)
        (stage / "マーカー結果.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.rename(stage, output)
    progress(f"マーカー付きXDWを保存しました: {output}", flush=True)
    return output
