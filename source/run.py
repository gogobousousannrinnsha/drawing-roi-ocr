"""Console entry point; paths and dependency caches belong to this folder."""
from contextlib import redirect_stderr
from pathlib import Path
import argparse
import os
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))


def choose(items, label, formatter=str):
    if not items:
        raise ValueError(f"{label}がありません。")
    if len(items) == 1:
        return items[0]
    print(label)
    for number, item in enumerate(items, 1):
        print(f"  {number}. {formatter(item)}")
    while True:
        answer = input("番号を入力してください（空欄で中止）: ").strip()
        if not answer:
            raise KeyboardInterrupt
        if answer.isdecimal() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        print("表示された番号を入力してください。")


def selected_result(root, requested=None):
    from drawing_roi_ocr.storage import local_path, read_json
    if requested:
        path = local_path(root, requested)
        return path / "result.json" if path.is_dir() else path
    available = []
    candidates = [p for name in ("r", "runs") for p in (root / "data" / name).glob("*/result.json")]
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True):
        try:
            if read_json(path).get("status") in ("complete", "partial"):
                available.append(path)
        except (OSError, ValueError):
            pass
    return choose(available, "再出力する実行", lambda p: p.parent.name)


def execute(args, root):
    from drawing_roi_ocr import workflow
    from drawing_roi_ocr.storage import local_path
    if args.command == "selfcheck":
        from drawing_roi_ocr.selfcheck import run_selfcheck
        run_selfcheck(root)
        print("OCR・DocuWorksの人工試料による初期確認が完了しました。", flush=True)
        return 0
    command = args.command
    if command in ("template", "extract"):
        options = (["見本から編集用XDWを作成", "編集後のXDWを項目別に分割"] if command == "template"
                   else ["inputから新規抽出", "保存結果からExcelを再出力"])
        picked = choose(options, "操作を選んでください")
        command = ("template-create" if picked == options[0] else "template-split") if command == "template" else (
            "extract-run" if picked == options[0] else "export")
    if command == "template-create":
        samples = workflow.input_files(root / "見本")
        if len(samples) != 1:
            raise ValueError("見本フォルダーには1つだけPDFまたはXDWを置いてください。")
        sample = samples[0]
        pages = workflow.inspect_pages(sample)
        number = args.page if args.page is not None else choose(
            list(range(1, len(pages) + 1)), "テンプレートにするページ")
        workflow.create_template_work(root, sample, number)
        return 0
    if command == "template-split":
        edited = local_path(root, args.edited) if args.edited else None
        workflow.split_template_work(root, edited)
        return 0
    if command == "export":
        from drawing_roi_ocr.excel import export_result
        output = export_result(root, selected_result(root, args.run))
        print(f"新しいExcelを保存しました: {output}", flush=True)
        return 0
    if command == "mark":
        from drawing_roi_ocr.markers import mark_confirmed, workbook_files
        workbook = (Path(args.excel) if args.excel else choose(workbook_files(root), "確認済みを付けたExcel",
                    lambda p: str(p.relative_to(root))))
        mark_confirmed(root, workbook, result_reference=args.run)
        return 0
    folders = sorted((p for p in (root / "templates").iterdir() if p.is_dir()), key=lambda p: p.name)
    template = local_path(root, args.template) if args.template else choose(folders, "テンプレート様式", lambda p: p.name)
    return workflow.extract(root, template)[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description="図面範囲OCR")
    parser.add_argument("command", choices=["template", "extract", "template-create", "template-split",
                                            "extract-run", "export", "selfcheck", "mark"])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--page", type=int)
    parser.add_argument("--template", help="アプリ内のテンプレートフォルダー相対パス")
    parser.add_argument("--edited", help="編集済みXDWのアプリ内相対パス")
    parser.add_argument("--run", help="再出力する保存結果のアプリ内相対パス")
    parser.add_argument("--excel", help="確認済みを付けたExcel（絶対パスまたはアプリ内の相対パス）")
    args = parser.parse_args(argv)
    from drawing_roi_ocr.workflow import ensure_layout
    from drawing_roi_ocr.storage import new_id, require_short_root
    try:
        root = ensure_layout(require_short_root(args.root))
    except ValueError as exc:
        print(str(exc), flush=True)
        return 1
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(root / "temp" / "x")
    os.environ["PADDLE_HOME"] = str(root / "temp" / "d")
    os.environ["TMP"] = os.environ["TEMP"] = str(root / "temp")
    log_path = root / "logs" / (new_id() + ".log")
    with log_path.open("w", encoding="utf-8") as log, redirect_stderr(log):
        try:
            if args.command == "selfcheck":
                return execute(args, root)
            from filelock import FileLock, Timeout
            try:
                with FileLock(root / "data" / "operation.lock", timeout=0):
                    return execute(args, root)
            except Timeout:
                raise RuntimeError("別の処理が実行中です。終了後に再実行してください。") from None
        except (KeyboardInterrupt, EOFError):
            print("中断しました。保存済みの結果は保持されています。", flush=True)
            return 130
        except Exception as exc:
            traceback.print_exc(file=log)
            print(f"処理を完了できませんでした: {exc}", flush=True)
            print(f"詳細記録: {log_path}", flush=True)
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
