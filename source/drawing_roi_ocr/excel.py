"""Two-sheet, literal-string export from an immutable run snapshot."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import uuid

from .storage import local_path, new_id, read_json, relative, sha, write_json, now, require_short_path

STATES = {"recognized": "取得・未確認", "not_detected": "未検出", "failed": "処理失敗",
          "skipped": "対象外", "not_processed": "未処理"}
DETAIL_HEADERS = ["原本ファイル", "ページ", "項目", "状態", "理由", "OCR原文", "信頼度",
                  "x_mm", "y_mm", "width_mm", "height_mm", "切り出し画像", "根拠ID"]


def _text(sheet, row, col, text, style=None):
    if not isinstance(text, str):
        text = str(text)
    if len(text) > 32767:
        raise ValueError("Excelのセル文字数上限を超えました。元の結果JSONは保持されています。")
    if any(ord(c) < 32 and c not in "\t\r\n" for c in text):
        raise ValueError("Excelで表現できない文字があります。元の結果JSONは保持されています。")
    if row >= 1048576 or col >= 16384:
        raise ValueError("Excelの行・列上限を超えました。元の結果JSONは保持されています。")
    sheet.write_string(row, col, text, style)


def write_excel(path, result):
    import xlsxwriter
    path = require_short_path(path, label="Excel保存先")
    if os.path.lexists(path):
        raise FileExistsError("既存のExcelは上書きしません。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = require_short_path(path.with_name("e" + uuid.uuid4().hex[:12] + ".xlsx"), label="Excel一時保存先")
    owned = False
    try:
        with temporary.open("xb"):
            owned = True
        with xlsxwriter.Workbook(temporary, {"strings_to_numbers": False,
                "strings_to_formulas": False, "strings_to_urls": False}) as workbook:
            main = workbook.add_worksheet("抽出結果")
            detail = workbook.add_worksheet("確認情報")
            header = workbook.add_format({"bold": True, "bg_color": "#D9EAF7"})
            wrap = workbook.add_format({"text_wrap": True, "valign": "top"})
            numeric = workbook.add_format({"num_format": "0.0000"})
            main_headers = ["原本ファイル", "ページ"] + [f["name"] for f in result["template"]["fields"]]
            for sheet, headers in ((main, main_headers), (detail, DETAIL_HEADERS)):
                sheet.freeze_panes(1, 2)
                for col, value in enumerate(headers):
                    _text(sheet, 0, col, value, header)
                sheet.set_column(0, 0, 36)
                sheet.set_column(1, 1, 8)
                sheet.set_column(2, len(headers) - 1, 28, wrap)
            detail.set_column(7, 10, 14, numeric)
            detail_count = 0
            for row, page in enumerate(result["pages"], 1):
                _text(main, row, 0, page["source_name"])
                _text(main, row, 1, "" if page.get("number") is None else str(page["number"]))
                by_id = {f["id"]: f for f in page["fields"]}
                for col, template in enumerate(result["template"]["fields"], 2):
                    field = by_id[template["id"]]
                    _text(main, row, col, field.get("value", ""), wrap)
                    regions = field.get("regions", [])
                    for region in regions or [None]:
                        detail_count += 1
                        rect = region["bbox_mm"] if region else field["rect_mm"]
                        values = [page["source_name"], "" if page.get("number") is None else str(page["number"]),
                                  field["name"], STATES[field["status"]], field.get("reason", ""),
                                  region["text"] if region else "", "" if region is None else str(region["confidence"]),
                                  *[rect[k] for k in ("x", "y", "width", "height")],
                                  field.get("crop_path", ""), region["id"] if region else ""]
                        for dcol, value in enumerate(values):
                            if 7 <= dcol <= 10:
                                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                                    raise ValueError("確認情報の座標は有限の数値である必要があります。")
                                detail.write_number(detail_count, dcol, value, numeric)
                            else:
                                _text(detail, detail_count, dcol, value, wrap)
            main.autofilter(0, 0, len(result["pages"]), len(main_headers) - 1)
            detail.autofilter(0, 0, detail_count, len(DETAIL_HEADERS) - 1)
        if os.path.lexists(path):
            raise FileExistsError("既存のExcelは上書きしません。")
        os.rename(temporary, path)
    finally:
        if owned:
            temporary.unlink(missing_ok=True)
    return {"pages": len(result["pages"]), "detail_rows": detail_count}


def export_result(root, result_path):
    root = Path(root).resolve()
    result_path = Path(result_path).resolve(strict=True)
    if not any(result_path.is_relative_to(root / "data" / name) for name in ("r", "runs")):
        raise ValueError("再出力対象はこのアプリのdata内の保存結果を指定してください。")
    receipt_path = result_path.parent / "receipt.json"
    receipt = read_json(receipt_path)
    if receipt["result_sha256"] != sha(result_path):
        raise ValueError("保存結果のSHAが一致しません。破損した結果からは出力しません。")
    result = read_json(result_path)
    if result.get("schema") != "drawing-roi-result-v1" or result["status"] not in ("complete", "partial"):
        raise ValueError("完了または一部失敗の保存結果だけを再出力できます。")
    for page in result["pages"]:
        for field in page["fields"]:
            if field.get("crop_path"):
                crop = local_path(root, field["crop_path"])
                if sha(crop) != field["crop_sha256"]:
                    raise ValueError("切り出し画像が保存時から変わっています。")
    folder = root / "exports" / new_id()
    folder.mkdir(parents=True, exist_ok=False)
    output = folder / "抽出結果.xlsx"
    counts = write_excel(output, result)
    write_json(folder / "export.json", {"created_at": now(), "result": relative(root, result_path),
        "result_sha256": sha(result_path), "excel_sha256": sha(output), **counts})
    return output
