"""Local setup checks with authored fixtures, separate from drawing accuracy."""
from pathlib import Path
import time

from .storage import new_id, now, relative, write_json


def run_selfcheck(root):
    from PIL import Image, ImageDraw, ImageFont
    from .pdf_ocr import OCRSession
    from .roi_ocr import join_regions, recognize_crop
    from .templates import create_editable_xdw, inspect_template
    root = Path(root).resolve()
    run_id = new_id()
    folder = root / "data" / "c" / run_id
    folder.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    ocr_record = {"run_id": run_id, "created_at": now(), "status": "in_progress", "scope": "authored_fixture"}
    sdk_record = dict(ocr_record)
    write_json(root / "data" / "setup-real-ocr.json", ocr_record)
    write_json(root / "data" / "setup-docuworks.json", sdk_record)
    try:
        font_folder = Path(__import__("os").environ.get("SystemRoot", r"C:\Windows")) / "Fonts"
        font_path = next((font_folder / n for n in ("arial.ttf", "meiryo.ttc", "YuGothR.ttc")
                          if (font_folder / n).is_file()), None)
        if font_path is None:
            raise FileNotFoundError("初期動作確認に使用できるWindowsフォントがありません。")
        page = {"number": 1, "width_mm": 210.0, "height_mm": 297.0,
                "width_px": 2481, "height_px": 3508, "rotation": 0}
        image_path = folder / "check.png"
        rect = {"x": 15.0, "y": 15.0, "width": 80.0, "height": 20.0}
        with Image.new("RGB", (2481, 3508), "white") as image:
            ImageDraw.Draw(image).text((220, 215), "001-020", font=ImageFont.truetype(str(font_path), 72), fill="black")
            image.save(image_path, dpi=(300, 300))
        session = OCRSession(root / "models" / "ocr")
        try:
            regions, raw, geometry = recognize_crop(image_path, page, rect, root / "models" / "ocr",
                                                    folder / "ocr", session=session, crop_id="setup")
        finally:
            session.close()
        value = join_regions(regions)
        write_json(folder / "raw.json", raw)
        if value != "001-020":
            raise RuntimeError(f"OCRの初期確認値が一致しません。期待=001-020 / 結果={value!r}")
        ocr_record.update(status="complete", expected="001-020", actual=value,
                          output=relative(root, folder), seconds=time.perf_counter() - started)
        write_json(root / "data" / "setup-real-ocr.json", ocr_record)
        xdw = folder / "check.xdw"
        create_editable_xdw(image_path, page, [{"bbox_mm": rect}], xdw)
        checked = inspect_template(xdw)
        sdk_record.update(status="complete", output=relative(root, xdw),
                          width_mm=checked["width_mm"], height_mm=checked["height_mm"],
                          rotation=checked["rotation"], rect_mm=checked["rect_mm"],
                          seconds=time.perf_counter() - started)
        write_json(root / "data" / "setup-docuworks.json", sdk_record)
        return {"run_id": run_id, "status": "complete", "scope": "authored_fixture"}
    except BaseException as exc:
        if ocr_record["status"] != "complete":
            ocr_record.update(status="failed", reason=str(exc))
            write_json(root / "data" / "setup-real-ocr.json", ocr_record)
        sdk_record.update(status="failed", reason=str(exc))
        write_json(root / "data" / "setup-docuworks.json", sdk_record)
        raise
