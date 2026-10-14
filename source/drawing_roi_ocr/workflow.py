"""Run snapshots for template authoring and region-only extraction."""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import shutil
import time

from . import __version__
from .storage import local_path, new_id, now, read_json, relative, sha, write_json, require_short_path

FOLDERS = ("見本", "input", "templates", "data", "exports", "logs", "temp")
PAGE_TOLERANCE_MM = 0.1
INPUT_EXTENSIONS = (".pdf", ".xdw")


def ensure_layout(root):
    root = Path(root).resolve()
    for name in FOLDERS:
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def input_files(folder):
    folder = Path(folder)
    files = sorted((p for p in folder.iterdir() if p.suffix.lower() in INPUT_EXTENSIONS),
                   key=lambda p: (p.name.casefold(), p.name))
    if not files:
        raise ValueError(f"{folder.name}フォルダー直下にPDFまたはXDWを置いてください。")
    for path in files:
        require_short_path(path, label="入力ファイル")
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or not path.is_file():
            raise ValueError("入力には通常のファイルを置いてください。リンクは使用できません。")
    return files


def inspect_pages(path):
    if Path(path).suffix.lower() == ".xdw":
        from .xdw_input import inspect_xdw
        return inspect_xdw(path)
    from .pdf_ocr import inspect_pdf
    return inspect_pdf(path)


def render_page(path, number, destination):
    if Path(path).suffix.lower() == ".xdw":
        from .xdw_input import render_page as render
    else:
        from .pdf_ocr import render_page as render
    return render(path, number, destination)


def _copy_checked(source, destination):
    source, destination = Path(source), Path(destination)
    before = sha(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        shutil.copyfileobj(incoming, outgoing)
    if sha(destination) != before or sha(source) != before:
        raise ValueError("コピー中に入力が変わりました。処理をやり直してください。")
    return before


def _field_records(fields, status="not_processed", reason=""):
    return [{"id": f["id"], "name": f["name"], "rect_mm": deepcopy(f["rect_mm"]),
             "status": status, "reason": reason, "value": "", "regions": []} for f in fields]


def _save_result(folder, result):
    path = folder / "result.json"
    write_json(path, result)
    write_json(folder / "receipt.json", {"schema": 1, "result_sha256": sha(path),
                                         "status": result["status"], "updated_at": now()})
    return path


def extract(root, template_folder, *, session_factory=None, recognize=None,
            template_loader=None, inspector=None, renderer=None, progress=print):
    """A new run every time: no full OCR and no cross-run recognition cache."""
    from PIL import Image
    from .excel import export_result
    from .pdf_ocr import MAX_IMAGE_PIXELS, MAX_PAGES, OCRSession, OCR_PROFILE
    from .roi_ocr import OcrInitializationError, join_regions, recognize_crop
    from .templates import load_template_set
    root = ensure_layout(root)
    paths = input_files(root / "input")
    template_loader = template_loader or load_template_set
    source_fields = template_loader(template_folder)
    if not source_fields:
        raise ValueError("テンプレートに項目がありません。")
    if len(source_fields) > 16382:
        raise ValueError("Excelの項目列上限を超えています。")
    folder = require_short_path(root / "data" / "r" / new_id(), label="抽出結果の保存先")
    folder.mkdir(parents=True, exist_ok=False)
    fields = []
    for ordinal, item in enumerate(source_fields, 1):
        target = folder / "t" / f"f{ordinal:04d}.xdw"
        copied_sha = _copy_checked(item["path"], target)
        if copied_sha != item["sha256"]:
            raise ValueError("検査後にテンプレートが変更されました。")
        fields.append({**item, "id": f"f{ordinal:04d}", "path": relative(root, target)})
    result = {"schema": "drawing-roi-result-v1", "app_version": __version__, "run_id": folder.name,
              "created_at": now(), "status": "running", "ocr_profile": dict(OCR_PROFILE),
              "template": {"name": Path(template_folder).name, "fields": fields}, "pages": [],
              "documents": [], "errors": []}
    write_json(folder / "template.json", result["template"])
    result_path = _save_result(folder, result)
    session = None
    inspector, renderer = inspector or inspect_pages, renderer or render_page
    recognize = recognize or recognize_crop
    session_factory = session_factory or OCRSession
    total_pages = 0
    recognition_finished = False
    try:
        for document_index, source in enumerate(paths, 1):
            doc_id = f"d{document_index:04d}"
            copied = folder / "s" / (doc_id + source.suffix.lower())
            progress(f"原本を保存・検査しています: {source.name}", flush=True)
            source_sha = _copy_checked(source, copied)
            document = {"id": doc_id, "name": source.name, "sha256": source_sha,
                        "source_path": relative(root, copied), "format": source.suffix.lower()[1:]}
            result["documents"].append(document)
            try:
                pages = inspector(copied)
            except Exception as exc:
                result["pages"].append({"document_id": doc_id, "source_name": source.name,
                    "number": None, "status": "failed", "reason": str(exc),
                    "fields": _field_records(fields, "failed", str(exc))})
                _save_result(folder, result)
                continue
            total_pages += len(pages)
            if total_pages > MAX_PAGES:
                raise ValueError(f"1回に処理できるページ数は{MAX_PAGES}までです。")
            for metadata in pages:
                started = time.perf_counter()
                number = metadata["number"]
                page_record = {"document_id": doc_id, "source_name": source.name,
                               "number": number, "status": "running", "fields": _field_records(fields)}
                result["pages"].append(page_record)
                progress(f"ページを確認しています: {source.name} / {number}", flush=True)
                if any(abs(metadata[k] - fields[0][k]) > PAGE_TOLERANCE_MM + 1e-8
                       for k in ("width_mm", "height_mm")):
                    reason = "ページ寸法・向きがテンプレートと一致しません。自動補正は行いません。"
                    page_record.update(status="skipped", reason=reason,
                                       fields=_field_records(fields, "skipped", reason))
                    _save_result(folder, result)
                    continue
                page_folder = folder / f"{doc_id}p{number:04d}"
                page_folder.mkdir(parents=True, exist_ok=False)
                image_path = page_folder / "page.png"
                try:
                    page = renderer(copied, number, image_path)
                    page_record["geometry"] = page
                    page_record["image_path"] = relative(root, image_path)
                    page_record["image_sha256"] = sha(image_path)
                    if session is None:
                        try:
                            session = session_factory(root / "models" / "ocr")
                        except Exception as exc:
                            raise OcrInitializationError(str(exc)) from exc
                    previous_limit = Image.MAX_IMAGE_PIXELS
                    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
                    try:
                        with Image.open(image_path) as page_image:
                            page_image.load()
                            for field in page_record["fields"]:
                                progress(f"範囲OCR: {source.name} / {number} / {field['name']}", flush=True)
                                field_folder = page_folder / field["id"]
                                field_folder.mkdir(exist_ok=False)
                                field_started = time.perf_counter()
                                try:
                                    regions, raw, geometry = recognize(image_path, page, field["rect_mm"],
                                        root / "models" / "ocr", field_folder, session=session,
                                        crop_id=f"{doc_id}:p{number}:{field['id']}", page_image=page_image)
                                    crop_path = Path(geometry.pop("crop_path"))
                                    field.update(status="recognized" if regions else "not_detected",
                                        reason="" if regions else "文字が得られませんでした。原本が空欄かは未確認です。",
                                        regions=regions, value=join_regions(regions), geometry=geometry,
                                        crop_path=relative(root, crop_path), crop_sha256=sha(crop_path))
                                    raw_path = field_folder / "raw.json"
                                    write_json(raw_path, raw)
                                    write_json(field_folder / "ocr.json", {"version": 1, "page": page,
                                        "rect_mm": field["rect_mm"], "geometry": geometry, "regions": regions})
                                    field.update(raw_path=relative(root, raw_path), raw_sha256=sha(raw_path))
                                except (OcrInitializationError, OSError):
                                    raise
                                except Exception as exc:
                                    field.update(status="failed", reason=str(exc), value="", regions=[])
                                finally:
                                    field["seconds"] = time.perf_counter() - field_started
                                _save_result(folder, result)
                    finally:
                        Image.MAX_IMAGE_PIXELS = previous_limit
                    page_record["status"] = "partial" if any(f["status"] == "failed" for f in page_record["fields"]) else "complete"
                except (OcrInitializationError, OSError):
                    raise
                except Exception as exc:
                    page_record.update(status="failed", reason=str(exc),
                                       fields=_field_records(fields, "failed", str(exc)))
                page_record["seconds"] = time.perf_counter() - started
                _save_result(folder, result)
        result["status"] = "partial" if any(p["status"] != "complete" for p in result["pages"]) else "complete"
        result["completed_at"] = now()
        result_path = _save_result(folder, result)
        recognition_finished = True
        write_json(root / "data" / "last-run.json", {"result": relative(root, result_path)})
        excel_path = export_result(root, result_path)
        progress(f"保存と検査が完了しました: {excel_path}", flush=True)
        return (0 if result["status"] == "complete" else 2), result_path, excel_path
    except BaseException as exc:
        if recognition_finished:
            # A workbook failure must leave completed OCR available for export.
            try:
                write_json(folder / "export-error.json", {"created_at": now(),
                    "type": type(exc).__name__, "message": str(exc)})
            except OSError:
                pass
            raise
        result.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", completed_at=now())
        result["errors"].append({"type": type(exc).__name__, "message": str(exc)})
        try:
            _save_result(folder, result)
        except OSError:
            pass
        raise
    finally:
        if session is not None:
            session.close()


def create_template_work(root, sample, number=1, *, progress=print):
    from .ocr_image import render_ocr_image
    from .pdf_ocr import recognize_page
    from .templates import create_editable_xdw
    root = ensure_layout(root)
    sample = Path(sample)
    if sample.suffix.lower() not in INPUT_EXTENSIONS or not sample.is_file():
        raise ValueError("見本にはPDFまたはXDWを指定してください。")
    pages = inspect_pages(sample)
    if type(number) is not int or not 1 <= number <= len(pages):
        raise ValueError("見本ページ番号が範囲外です。")
    folder = require_short_path(root / "data" / "w" / new_id(), label="テンプレート作業先")
    folder.mkdir(parents=True, exist_ok=False)
    copied = folder / ("source" + sample.suffix.lower())
    source_sha = _copy_checked(sample, copied)
    record = {"schema": "drawing-roi-template-work-v1", "status": "running", "created_at": now(),
              "source_name": sample.name, "source_sha256": source_sha, "source_page": number}
    write_json(folder / "work.json", record)
    write_json(root / "data" / "last-template-work.json", {"work": relative(root, folder / "work.json")})
    try:
        progress("見本ページを画像化しています。", flush=True)
        page = render_page(copied, number, folder / "page.png")
        progress("見本の全文OCRを実行しています。", flush=True)
        regions, raw = recognize_page(folder / "page.png", page, root / "models" / "ocr", folder / "s")
        ocr = {"version": 1, "page": page, "regions": regions}
        write_json(folder / "ocr.json", ocr)
        write_json(folder / "raw.json", raw)
        progress("白紙の確認PNGを作成しています。", flush=True)
        image_report = render_ocr_image(ocr, folder / "OCR確認.png")
        progress("画像XDWと編集用矩形を作成しています。", flush=True)
        xdw = create_editable_xdw(folder / "page.png", page, regions, folder / "編集用.xdw")
        record.update(status="complete", completed_at=now(), page=page,
                      region_count=len(regions), image_report=image_report,
                      editable_xdw=relative(root, folder / "編集用.xdw"), xdw=xdw)
        # Do not retain PC-specific absolute paths in saved work metadata.
        if "path" in record["xdw"]:
            record["xdw"]["path"] = relative(root, folder / "編集用.xdw")
        write_json(folder / "work.json", record)
        write_json(root / "data" / "last-template-work.json", {"work": relative(root, folder / "work.json")})
        progress(f"Viewerで矩形を編集・保存してください: {folder / '編集用.xdw'}", flush=True)
        return folder
    except BaseException as exc:
        record.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", reason=str(exc))
        write_json(folder / "work.json", record)
        raise


def split_template_work(root, edited=None, *, progress=print):
    from .templates import split_template
    root = ensure_layout(root)
    if edited is None:
        pointer = read_json(root / "data" / "last-template-work.json")
        work_path = local_path(root, pointer["work"])
        work = read_json(work_path)
        if work.get("status") != "complete":
            raise ValueError("完了したテンプレート作業がありません。")
        edited = local_path(root, work["editable_xdw"])
    output = root / "templates" / ("T_" + new_id())
    fields = split_template(edited, output)
    progress(f"分割しました。項目名へ改名してください: {output}", flush=True)
    return output, fields
