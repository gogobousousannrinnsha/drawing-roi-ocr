"""One visual-page XDW per field; native imports stay operation-local.

The editable background is a flattened image, so every top-level rectangle
is an extraction range. Nested rectangles are never interpreted as page
coordinates. All native writes use short, owned temporary paths.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from decimal import Decimal, ROUND_HALF_UP
import math
import os
from pathlib import Path
import shutil
import tempfile

from .storage import sha, require_short_path
from .xdw_input import _commit_new_file, _load_api, _check_sdk_temp

SDK_PATH_LIMIT = 255
MIN_RECTANGLE_MM = 3.0
MAX_RECTANGLE_MM = 2400.0
PAGE_TOLERANCE_MM = 0.1
RENDER_DPI = 300
MAX_IMAGE_PIXELS = 600_000_000


def _finite(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}には有限の数値が必要です。")
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"{label}には{'正の' if positive else ''}有限の数値が必要です。")
    return float(value)


def _hundredths(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _sdk_path(path):
    return require_short_path(path, limit=SDK_PATH_LIMIT, label="DocuWorks一時パス")


def _source_path(path):
    path = Path(path).expanduser().resolve()
    if path.suffix.casefold() != ".xdw":
        raise ValueError("テンプレートにはXDWファイルを指定してください。")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _destination(path, *, file=False):
    path = require_short_path(Path(path).expanduser().absolute(), label="テンプレートの保存先")
    if os.path.lexists(path):
        raise FileExistsError(f"出力先は既に存在します: {path}")
    if file and path.suffix.casefold() != ".xdw":
        raise ValueError("出力ファイルには.xdwを指定してください。")
    if file:
        require_short_path(path.parent / (".p" + "0" * 8) / "f.xdw",
                           label="テンプレートの一時保存先")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _source_copy(path):
    _check_sdk_temp("s.xdw")
    original_hash = sha(path)
    with tempfile.TemporaryDirectory(prefix="x") as temporary:
        copied = _sdk_path(Path(temporary) / "s.xdw")
        shutil.copyfile(path, copied)
        if sha(copied) != original_hash or sha(path) != original_hash:
            raise RuntimeError("読込中にテンプレートが変更されました。")
        yield copied, original_hash
        if sha(path) != original_hash:
            raise RuntimeError("処理中にテンプレートが変更されました。")


def _geometry(annotation, width, height):
    position, size = annotation.position, annotation.size
    if size is None:
        raise ValueError("矩形の幅・高さを取得できません。")
    rect = {
        "x": _finite(position.x, "矩形のX座標"),
        "y": _finite(position.y, "矩形のY座標"),
        "width": _finite(size.width, "矩形の幅", positive=True),
        "height": _finite(size.height, "矩形の高さ", positive=True),
    }
    if (rect["x"] < 0 or rect["y"] < 0 or
            rect["x"] + rect["width"] > width + 1e-7 or
            rect["y"] + rect["height"] > height + 1e-7):
        raise ValueError("読み取り矩形がページ範囲外です。")
    return rect


def _read_page(document):
    from docuworks_ctypes import AnnotationType
    from docuworks_ctypes._raw import types as T
    from docuworks_ctypes.errors import check_result

    if document.page_count != 1:
        raise ValueError("テンプレートXDWは1ページにしてください。")
    info = T.XDW_PAGE_INFO_EX()
    info.nSize = ctypes.sizeof(info)
    check_result(document.raw.XDW_GetPageInformation(
        document.handle, 1, ctypes.cast(ctypes.byref(info), ctypes.POINTER(T.XDW_PAGE_INFO))),
        "XDW_GetPageInformation(template)")
    width = _finite(info.nWidth / 100, "ページ幅", positive=True)
    height = _finite(info.nHeight / 100, "ページ高さ", positive=True)
    if info.nDegree != 0:
        raise ValueError("テンプレートXDWのページ回転は0度にしてください。")
    page = document.page(1)
    annotations = tuple(page.annotations(recursive=False))
    rectangles = []
    for annotation in annotations:
        if any(child.annotation_type == AnnotationType.RECTANGLE
               for child in annotation.descendants()):
            raise ValueError("グループや付箋内の矩形には対応していません。ページへ直接置いてください。")
        if annotation.annotation_type == AnnotationType.RECTANGLE:
            rectangles.append((annotation, _geometry(annotation, width, height)))
    return {"width_mm": width, "height_mm": height, "rotation": 0}, rectangles, annotations


def _open(api, path, *, writable=False):
    from docuworks_ctypes import AuthMode, OpenMode
    return api.open_document(_sdk_path(path),
                             mode=OpenMode.UPDATE if writable else OpenMode.READONLY,
                             auth=AuthMode.NO_DIALOG)


def _inspect(path, api, *, require_one):
    with _open(api, path) as document:
        page, rectangles, annotations = _read_page(document)
        if require_one and len(rectangles) != 1:
            raise ValueError(f"項目用XDWの読み取り矩形は1個にしてください（現在{len(rectangles)}個）。")
        return page, [rect for _, rect in rectangles], len(annotations)


def inspect_template(path, *, api_factory=None):
    source = _source_path(path)
    _check_sdk_temp("s.xdw")
    api = _load_api(api_factory)
    with _source_copy(source) as (working, digest):
        page, rectangles, _ = _inspect(working, api, require_one=True)
    return {"name": source.stem, "path": str(source), **page,
            "rect_mm": rectangles[0], "sha256": digest}


def load_template_set(folder):
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise NotADirectoryError(folder)
    paths = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.casefold() == ".xdw"),
                   key=lambda p: (p.name.casefold(), p.name))
    if not paths:
        raise ValueError("テンプレートフォルダー直下に項目用XDWがありません。")
    result = [inspect_template(path) for path in paths]
    if any(max(item[key] for item in result) - min(item[key] for item in result)
           > PAGE_TOLERANCE_MM + 1e-8 for key in ("width_mm", "height_mm")):
        raise ValueError("項目用XDWのページ寸法がそろっていません（許容差0.1mm）。")
    if len({item["name"].casefold() for item in result}) != len(result):
        raise ValueError("項目名が重複しています。ファイル名を変更してください。")
    return result


def _fit_axis(start, length, limit):
    start = _finite(start, "矩形位置")
    length = _finite(length, "矩形サイズ", positive=True)
    low, high = max(0.0, start), min(limit, start + length)
    if high <= low:
        raise ValueError("OCR矩形がページと重なりません。")
    span = max(MIN_RECTANGLE_MM, high - low)
    if span > MAX_RECTANGLE_MM or limit < MIN_RECTANGLE_MM:
        raise ValueError("DocuWorksの矩形サイズ制限（3～2400mm）を満たしません。")
    low = max(0.0, min((low + high - span) / 2, limit - span))
    limit_raw = _hundredths(limit)
    span_raw = max(300, _hundredths(span))
    low_raw = max(0, min(_hundredths(low), limit_raw - span_raw))
    return low_raw / 100, span_raw / 100


def _planned_rectangles(regions, page):
    result = []
    for region in regions:
        if not isinstance(region, dict) or not isinstance(region.get("bbox_mm"), dict):
            raise ValueError("OCR領域にはbbox_mmが必要です。")
        box = region["bbox_mm"]
        x, width = _fit_axis(box.get("x"), box.get("width"), page["width_mm"])
        y, height = _fit_axis(box.get("y"), box.get("height"), page["height_mm"])
        result.append(dict(x=x, y=y, width=width, height=height))
    return result


def _same_page(actual, expected):
    if any(_hundredths(actual[key]) != _hundredths(expected[key])
           for key in ("width_mm", "height_mm")) or actual["rotation"] != 0:
        raise RuntimeError("保存後のページ寸法・回転が一致しません。")


def _same_rect(actual, expected):
    if any(abs(actual[key] - expected[key]) > 0.000001 for key in expected):
        raise RuntimeError("保存後の読み取り矩形が一致しません。")


def _publish_file(working, output):
    expected = sha(working)
    require_short_path(output.parent / (".p" + "0" * 8) / "f.xdw",
                       label="テンプレートの一時保存先")
    with tempfile.TemporaryDirectory(prefix=".p", dir=output.parent) as temporary:
        staged = require_short_path(Path(temporary) / "f.xdw", label="テンプレートの一時保存先")
        shutil.copyfile(working, staged)
        if sha(staged) != expected:
            raise RuntimeError("出力コピーのハッシュが一致しません。")
        _commit_new_file(staged, output)
    return expected


def create_editable_xdw(image_path, page, regions, output_path, *, api_factory=None):
    """Flatten a saved 300-dpi visual page and add editable red OCR ranges."""
    from PIL import Image
    from docuworks_ctypes import Color, RectMM
    from docuworks_ctypes._raw import types as T, constants as C
    from docuworks_ctypes.encoding import wchar_buffer
    from docuworks_ctypes.errors import check_result

    source = Path(image_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    _check_sdk_temp("e.xdw")
    output = _destination(output_path, file=True)
    if output.resolve() == source:
        raise ValueError("原画像への書込みはできません。")
    if page.get("rotation", 0) != 0:
        raise ValueError("表示ページ画像は回転適用済みの0度にしてください。")
    dimensions = {key: _hundredths(_finite(page.get(key), key, positive=True)) / 100
                  for key in ("width_mm", "height_mm")}
    if any(not MIN_RECTANGLE_MM <= value <= MAX_RECTANGLE_MM for value in dimensions.values()):
        raise ValueError("画像XDWのページ寸法は3～2400mmにしてください。")
    dimensions["rotation"] = 0
    rectangles = _planned_rectangles(regions, dimensions)
    source_hash = sha(source)
    api = _load_api(api_factory)
    with tempfile.TemporaryDirectory(prefix="x") as temporary:
        folder = Path(temporary)
        bmp, working = _sdk_path(folder / "b.bmp"), _sdk_path(folder / "e.xdw")
        previous_limit = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        try:
            with source.open("rb") as stream, Image.open(stream) as image:
                width, height = image.size
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("画像サイズが上限を超えています。")
                for key, actual, physical in (("width_px", width, dimensions["width_mm"]),
                                              ("height_px", height, dimensions["height_mm"])):
                    if key in page and page[key] != actual:
                        raise ValueError("原画像のpx寸法と保存ページ情報が一致しません。")
                    if abs(actual - math.ceil(physical * RENDER_DPI / 25.4)) > 1:
                        raise ValueError("画像はページ実寸300dpiに一致している必要があります。")
                with image.convert("RGB") as rgb:
                    rgb.save(bmp, format="BMP", dpi=(RENDER_DPI, RENDER_DPI))
        finally:
            Image.MAX_IMAGE_PIXELS = previous_limit
        options = T.XDW_CREATE_OPTION()
        options.nSize = ctypes.sizeof(options)
        options.nFitImage, options.nZoom = C.XDW_CREATE_USERDEF, 100
        options.nWidth = _hundredths(dimensions["width_mm"])
        options.nHeight = _hundredths(dimensions["height_mm"])
        options.nHorPos, options.nVerPos = C.XDW_CREATE_HCENTER, C.XDW_CREATE_VCENTER
        check_result(api.raw.XDW_CreateXdwFromImageFileW(
            wchar_buffer(str(bmp)), wchar_buffer(str(working)), ctypes.byref(options)),
            "XDW_CreateXdwFromImageFileW(template background)")
        background, _, annotation_count = _inspect(working, api, require_one=False)
        _same_page(background, dimensions)
        if annotation_count:
            raise RuntimeError("生成した背景XDWに予期しない注釈があります。")
        if rectangles:
            with _open(api, working, writable=True) as document:
                target = document.page(1)
                for rect in rectangles:
                    target.add_rectangle(RectMM(**rect), border_color=Color.RED,
                                         border_width=1, border_visible=True, fill_visible=False)
                document.save()
        with _open(api, working) as document:
            actual_page, actual_rectangles, annotations = _read_page(document)
            _same_page(actual_page, dimensions)
            if len(actual_rectangles) != len(rectangles) or len(annotations) != len(rectangles):
                raise RuntimeError("保存後の矩形数が一致しません。")
            for (annotation, actual), expected in zip(actual_rectangles, rectangles):
                _same_rect(actual, expected)
                for attribute, expected_value in (("%BorderColor", int(Color.RED)),
                        ("%BorderWidth", 1), ("%BorderStyle", True), ("%FillStyle", False)):
                    if annotation.get_standard_attribute(attribute) != expected_value:
                        raise RuntimeError(f"保存後の矩形スタイルが一致しません: {attribute}")
        if sha(source) != source_hash:
            raise RuntimeError("処理中に原画像が変更されました。")
        digest = _publish_file(working, output)
    return {"path": str(output), "sha256": digest, **dimensions,
            "rectangle_count": len(rectangles)}


def split_template(edited_xdw, output_folder, *, api_factory=None):
    """Keep each edited rectangle in a separate, newly published XDW copy."""
    source = _source_path(edited_xdw)
    _check_sdk_temp("s.xdw")
    output = _destination(output_folder)
    require_short_path(output / "項目_001.xdw", label="分割テンプレートの保存先")
    require_short_path(output.parent / (".s" + "0" * 8) / "d" / "項目_001.xdw",
                       label="分割テンプレートの一時保存先")
    api = _load_api(api_factory)
    with _source_copy(source) as (copied, source_hash), \
            tempfile.TemporaryDirectory(prefix=".s", dir=output.parent) as temporary:
        page, rectangles, _ = _inspect(copied, api, require_one=False)
        if not rectangles:
            raise ValueError("編集用XDWに読み取り矩形がありません。")
        staged = Path(temporary) / "d"
        final_name = f"項目_{len(rectangles):03d}.xdw"
        require_short_path(output / final_name, label="分割テンプレートの保存先")
        require_short_path(staged / final_name, label="分割テンプレートの一時保存先")
        _sdk_path(copied.parent / f"i{len(rectangles)}.xdw")
        staged.mkdir()
        result = []
        for index, expected in enumerate(rectangles):
            working = _sdk_path(copied.parent / f"i{index + 1}.xdw")
            shutil.copyfile(copied, working)
            with _open(api, working, writable=True) as document:
                _, candidates, _ = _read_page(document)
                for number, (annotation, _) in reversed(list(enumerate(candidates))):
                    if number != index:
                        annotation.remove()
                document.save()
            actual_page, actual_rectangles, _ = _inspect(working, api, require_one=True)
            _same_page(actual_page, page)
            _same_rect(actual_rectangles[0], expected)
            name = f"項目_{index + 1:03d}.xdw"
            destination = staged / name
            shutil.copyfile(working, destination)
            digest = sha(working)
            if sha(destination) != digest:
                raise RuntimeError("分割したテンプレートのハッシュが一致しません。")
            result.append({"name": Path(name).stem, "path": str(output / name), **page,
                           "rect_mm": expected, "sha256": digest})
        if sha(source) != source_hash:
            raise RuntimeError("分割中に編集用XDWが変更されました。")
        if os.path.lexists(output):
            raise FileExistsError(output)
        # On the supported Windows host this atomic rename refuses any existing
        # destination, including an empty directory that appeared concurrently.
        os.rename(staged, output)
    return result
