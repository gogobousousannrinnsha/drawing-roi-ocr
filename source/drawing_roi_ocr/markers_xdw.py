"""Write genuine translucent yellow markers to a new verified XDW.

The stroke layout follows the verified 2026-10-13 marker add-on: horizontal
strokes of 1..13 pt cover each box, with inset ends for rounded caps. XDWAPI
annotation coordinates refer to the displayed page, including its rotation.
PDF pages are rasterized once and retained as zero-degree image XDW pages.
"""
from __future__ import annotations

from collections import Counter
import ctypes
from decimal import Decimal, ROUND_HALF_UP
import math
import os
from pathlib import Path
import shutil
import tempfile

from .storage import require_short_path, sha
from .xdw_input import _load_api, _commit_new_file

MM_PER_PT = 25.4 / 72
MAX_STROKES = 20_000
MAX_PAGES = 1000


def _q(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)) / 100


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}には有限の数値が必要です。")
    if positive and value <= 0:
        raise ValueError(f"{label}は0より大きい値にしてください。")
    return float(value)


def _page_metadata(document, number):
    from docuworks_ctypes._raw import types as T
    from docuworks_ctypes.errors import check_result
    info = T.XDW_PAGE_INFO_EX()
    info.nSize = ctypes.sizeof(info)
    check_result(document.raw.XDW_GetPageInformation(document.handle, number,
        ctypes.cast(ctypes.byref(info), ctypes.POINTER(T.XDW_PAGE_INFO))), "XDW_GetPageInformation(markers)")
    if info.nWidth <= 0 or info.nHeight <= 0 or info.nDegree % 90:
        raise ValueError(f"ページ{number}の寸法・回転が不正です。")
    return dict(width_mm=info.nWidth / 100, height_mm=info.nHeight / 100,
                source_rotation=info.nDegree % 360)


def _open(api, path, *, writable=False):
    from docuworks_ctypes import AuthMode, OpenMode
    return api.open_document(require_short_path(path, 255, "DocuWorks作業パス"),
        mode=OpenMode.UPDATE if writable else OpenMode.READONLY, auth=AuthMode.NO_DIALOG)


def _dimensions(document):
    if type(document.page_count) is not int or not 1 <= document.page_count <= MAX_PAGES:
        raise ValueError(f"対象文書のページ数は1～{MAX_PAGES}ページにしてください。")
    return {number: _page_metadata(document, number) for number in range(1, document.page_count + 1)}


def _snapshot(annotation):
    position, size = annotation.position, annotation.size
    return (int(annotation.annotation_type), position.x, position.y,
            None if size is None else (size.width, size.height))


def _validated_rectangles(rectangles_by_page, dimensions):
    if not isinstance(rectangles_by_page, dict):
        raise ValueError("マーカー範囲にはページ番号ごとの辞書が必要です。")
    result = {}
    for number, rectangles in rectangles_by_page.items():
        if type(number) is not int or number not in dimensions:
            raise ValueError(f"対象文書にページ{number}はありません。")
        if not isinstance(rectangles, (list, tuple)):
            raise ValueError("ページごとのマーカー範囲は一覧で指定してください。")
        size = dimensions[number]
        width = _number(size["width_mm"], "ページ幅", positive=True)
        height = _number(size["height_mm"], "ページ高さ", positive=True)
        result[number] = []
        for rectangle in rectangles:
            if not isinstance(rectangle, dict):
                raise ValueError("マーカー範囲は矩形の辞書で指定してください。")
            x, y = (_number(rectangle.get(key), key) for key in ("x", "y"))
            w, h = (_number(rectangle.get(key), key, positive=True) for key in ("width", "height"))
            if x < 0 or y < 0 or x + w > width + 1e-6 or y + h > height + 1e-6:
                raise ValueError(f"ページ{number}のマーカー範囲がページ外です。")
            left, top, right, bottom = _q(x), _q(y), min(_q(x + w), _q(width)), min(_q(y + h), _q(height))
            if right <= left or bottom <= top:
                raise ValueError("マーカー範囲はDocuWorksの1/100mm保存精度より大きくしてください。")
            result[number].append(dict(x=left, y=top, width=right-left, height=bottom-top))
    if not any(result.values()):
        raise ValueError("黄色マーカーを付ける範囲がありません。")
    return result


def _strokes(rectangles_by_page):
    strokes = []
    for number in sorted(rectangles_by_page):
        for rectangle_index, box in enumerate(rectangles_by_page[number], 1):
            count = max(1, math.ceil(box["height"] / (13 * MM_PER_PT)))
            if count + len(strokes) > MAX_STROKES:
                raise ValueError(f"黄色マーカーは1文書{MAX_STROKES}個までです。")
            width_pt = min(13, max(1, math.ceil(box["height"] / count / MM_PER_PT)))
            thickness = width_pt * MM_PER_PT
            y_start = box["y"] + min(box["height"], thickness) / 2
            y_end = box["y"] + box["height"] - min(box["height"], thickness) / 2
            inset = max(0, min(box["width"] / 2 - .005, thickness / 2))
            x1, x2 = _q(box["x"] + inset), _q(box["x"] + box["width"] - inset)
            if x2 <= x1:
                x1, x2 = box["x"], _q(box["x"] + box["width"])
            for index in range(count):
                y = y_start if count == 1 else y_start + (y_end-y_start) * index / (count-1)
                strokes.append(dict(page=number, rectangle_index=rectangle_index,
                                    width_pt=width_pt, points=((x1, _q(y)), (x2, _q(y)))))
    return strokes


def _pdf_dimensions(path):
    from .pdf_ocr import inspect_pdf
    pages = inspect_pdf(path)
    return {page["number"]: dict(width_mm=page["width_mm"], height_mm=page["height_mm"],
                                 source_rotation=page["source_rotation"]) for page in pages}


def _pdf_to_xdw(api, source, folder, dimensions, progress):
    from .pdf_ocr import render_page
    from .templates import create_editable_xdw
    from docuworks_ctypes.encoding import wchar_buffer
    from docuworks_ctypes.errors import check_result
    target = folder / "d.xdw"
    for number in sorted(dimensions):
        progress(f"PDFを画像XDWへ変換しています: {number}/{len(dimensions)}ページ", flush=True)
        image, part = folder / "p.png", folder / "p.xdw"
        page = render_page(source, number, image)
        create_editable_xdw(image, page, [], part, api_factory=lambda: api)
        if number == 1:
            shutil.copyfile(part, target)
        else:
            with _open(api, target, writable=True) as document:
                check_result(document.raw.XDW_InsertDocumentW(document.handle, number,
                    wchar_buffer(str(require_short_path(part, 255, "DocuWorks作業パス"))), None),
                    "XDW_InsertDocumentW(markers)")
                document.save()
        image.unlink()
        part.unlink()
    return target


def _verify(document, dimensions, before, planned):
    from docuworks_ctypes import AnnotationType, Color
    if _dimensions(document) != dimensions:
        raise RuntimeError("保存後のページ数・寸法・回転が一致しません。")
    grouped = {}
    for stroke in planned:
        grouped.setdefault(stroke["page"], []).append(stroke)
    checked = []
    for number, original in before.items():
        annotations = tuple(document.page(number).annotations(recursive=False))
        expected = grouped.get(number, [])
        if len(annotations) != len(original) + len(expected):
            raise RuntimeError(f"ページ{number}の保存後のマーカー数が一致しません。")
        if [_snapshot(annotation) for annotation in annotations[:len(original)]] != original:
            raise RuntimeError(f"ページ{number}の既存注釈が変化しました。")
        for index, (annotation, stroke) in enumerate(zip(annotations[len(original):], expected), len(original)+1):
            if annotation.annotation_type != AnnotationType.MARKER:
                raise RuntimeError("保存した注釈がマーカーではありません。")
            for attribute, value in (("%BorderColor", int(Color.YELLOW)),
                    ("%BorderWidth", stroke["width_pt"]), ("%BorderTransparent", True)):
                if annotation.get_standard_attribute(attribute) != value:
                    raise RuntimeError(f"保存後の黄色マーカー設定が一致しません: {attribute}")
            points = annotation.get_standard_attribute("%Points")
            if len(points) != 2 or any(abs(p.x-x) > .010001 or abs(p.y-y) > .010001
                                      for p, (x, y) in zip(points, stroke["points"])):
                raise RuntimeError("保存後のマーカー位置が一致しません。")
            checked.append({**stroke, "annotation_index": index})
    return checked


def _publish(working, destination):
    expected = sha(working)
    with tempfile.TemporaryDirectory(prefix=".p", dir=destination.parent) as temporary:
        staged = require_short_path(Path(temporary) / "d.xdw", label="マーカー出力の一時保存先")
        shutil.copyfile(working, staged)
        if sha(staged) != expected:
            raise RuntimeError("マーカー出力コピーのハッシュが一致しません。")
        _commit_new_file(staged, destination)
    return expected


def annotate_document(root, source, destination, rectangles_by_page, *, progress=print):
    """Add selected page-mm boxes to a new XDW; never modify the source."""
    source = Path(source).expanduser().resolve()
    destination = require_short_path(destination, label="黄色マーカーの保存先")
    if source.suffix.casefold() not in (".pdf", ".xdw") or not source.is_file():
        raise ValueError("マーカー元文書にはPDFまたはXDWファイルを指定してください。")
    if destination.suffix.casefold() != ".xdw":
        raise ValueError("黄色マーカーの保存先には.xdwを指定してください。")
    if os.path.lexists(destination):
        raise FileExistsError(destination)
    if source == destination:
        raise ValueError("原本文書への上書きはできません。")
    temp_root = require_short_path(Path(root).resolve() / "temp", label="マーカー作業フォルダー")
    for name in ("s.xdw", "s.pdf", "d.xdw", "p.xdw", "p.png"):
        require_short_path(temp_root / ("x" + "0" * 8) / name, 255, "DocuWorks作業パス")
    require_short_path(destination.parent / (".p" + "0" * 8) / "d.xdw", label="マーカー出力の一時保存先")
    source_hash = sha(source)
    temp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="x", dir=temp_root) as temporary:
        folder = Path(temporary)
        copied = folder / ("s" + source.suffix.casefold())
        shutil.copyfile(source, copied)
        if sha(copied) != source_hash or sha(source) != source_hash:
            raise RuntimeError("コピー中に原本文書が変更されました。")
        api = _load_api()
        if source.suffix.casefold() == ".pdf":
            dimensions = _pdf_dimensions(copied)
            rectangles = _validated_rectangles(rectangles_by_page, dimensions)
            planned = _strokes(rectangles)
            working = _pdf_to_xdw(api, copied, folder, dimensions, progress)
            expected_dimensions = {number: {**size, "width_mm": _q(size["width_mm"]),
                "height_mm": _q(size["height_mm"]), "source_rotation": 0}
                for number, size in dimensions.items()}
        else:
            working = copied
            with _open(api, working) as document:
                dimensions = _dimensions(document)
            rectangles = _validated_rectangles(rectangles_by_page, dimensions)
            planned = _strokes(rectangles)
            expected_dimensions = dimensions
        with _open(api, working) as document:
            actual = _dimensions(document)
            if actual != expected_dimensions:
                raise RuntimeError("画像XDWのページ寸法・回転が元の表示ページと一致しません。")
            before = {number: [_snapshot(a) for a in document.page(number).annotations(recursive=False)]
                      for number in actual}
        from docuworks_ctypes import Color, PointMM
        progress(f"黄色マーカーを配置しています: {len(planned)}個", flush=True)
        with _open(api, working, writable=True) as document:
            for index, stroke in enumerate(planned, 1):
                document.page(stroke["page"]).add_marker([PointMM(x, y) for x, y in stroke["points"]],
                    border_color=Color.YELLOW, border_width=stroke["width_pt"], border_transparent=True)
                if index % 50 == 0:
                    progress(f"黄色マーカー: {index}/{len(planned)}個", flush=True)
            progress("マーカー文書を保存しています。", flush=True)
            document.save()
        progress("保存した文書のページと黄色マーカーを再読取り確認しています。", flush=True)
        with _open(api, working) as document:
            checked = _verify(document, expected_dimensions, before, planned)
        if sha(source) != source_hash:
            raise RuntimeError("処理中に原本文書が変更されました。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        output_hash = _publish(working, destination)
    return dict(status="complete", source_format=source.suffix.casefold()[1:],
                source_sha256=source_hash, output_path=str(destination), output_sha256=output_hash,
                page_count=len(expected_dimensions),
                source_pages=dimensions,
                pages={number: dict(width_mm=size["width_mm"], height_mm=size["height_mm"],
                                    output_rotation=size["source_rotation"])
                       for number, size in expected_dimensions.items()},
                rectangle_count=sum(len(items) for items in rectangles.values()),
                marker_count=len(checked), markers_by_page=dict(Counter(item["page"] for item in checked)),
                verified_markers=checked, original_preserved=True)
