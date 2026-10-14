# 第三者資産と利用条件 / Third-party notices

自作の図面範囲OCRアプリ、BAT、取得処理、設定例、説明書はMITです。流用した自作コードの著作権表示を含め、`LICENSE`を保持してください。MITは外部から取得する実行環境やOCRモデルの利用条件を置き換えません。

The application sources and launchers are MIT licensed. Preserve their copyright and license notices. Third-party software and models retain their own licenses.

## 同封するCore / Bundled Core

`bundled/core/docuworks_ctypes-1.0.1-py3-none-any.whl`は自作公開版のDocuWorks Core 1.0.1です。48,173 bytes、SHA-256 `7ea3a6d1623238180ccb400237a753b616dccf72761ba83d97fee1b55f7f5f58`。MIT、Copyright (c) 2026 gogobousousannrinnsha。wheel内のMIT本文・著作権表示・RECORDを保持し、初期設定時に固定内容を検査します。取得由来は`assets/bundled-core.json`です。

This pure Python wrapper is the unchanged public MIT artifact from [dw-ocr v0.8.1](https://github.com/gogobousousannrinnsha/dw-ocr/releases/tag/v0.8.1). Its license is also provided under `licenses/core/`. No DocuWorks product, proprietary SDK, header, or DLL is bundled. XDW reading and template authoring require a compatible, legitimately installed DocuWorks environment on each PC.

## 初期設定で取得するもの / Online setup

固定したPython 3.13.15とTcl/Tkの公式MSIからファイルを取り出し、OSへのインストーラー実行・PATHや通常Pythonの設定変更をせずアプリ内へ配置します。91個の固定Python wheel、Paddle公式PP-OCRv6検出・認識モデルもこのフォルダー内へ取得します。版・公式URL・サイズ・SHAは`assets/`を参照してください。依存は既存OCR構成を維持し、必要性を確認せず機械的に削除しません。

The source ZIP contains no Python runtime, third-party wheel cache, GPU DLL, OCR weight, user drawing, OCR result, Excel output, or execution log. Online setup verifies fixed assets and retains their LICENSE, NOTICE, and package metadata. No language model, Ollama, or other inference server is installed.

| 資産 / Asset | 条件・参照先 / License reference |
|---|---|
| Python | PSF and bundled third-party conditions; [official license](https://docs.python.org/3.13/license.html) |
| Tcl/Tk | Individual Tcl/Tk conditions; [official license](https://www.tcl-lang.org/software/tcltk/license.html) |
| PaddlePaddle / PaddleOCR / PaddleX / PP-OCR | Apache 2.0 and bundled third-party conditions; [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR), [Paddle](https://github.com/PaddlePaddle/Paddle), [PaddleX](https://github.com/PaddlePaddle/PaddleX). Model notice and Apache text are in `licenses/ocr/`. |
| pypdfium2 / PDFium | Their licenses and PDFium third-party notices; [official licensing](https://pypdfium2.readthedocs.io/en/stable/readme.html#licensing) |
| Other Python wheels | Individual conditions, including transitive dependencies; see locked assets and installed `*.dist-info` notices |
| NVIDIA runtime components | Conditions accompanying the fixed CUDA/cuDNN libraries; the app's MIT license does not replace them |

DocuWorksとGPUドライバーは移動先PC側の前提です。フォントはWindows導入済みのものを利用し、フォント本体を同封しません。入力図面や画像は利用者・権利者の条件に従って扱ってください。

## 構築済みフォルダーの再配布 / Redistributing an installed folder

初期設定後のフォルダーはソースZIPと内容が異なります。第三者のコード、GPUライブラリ、OCRモデルについて、実ファイルに対応する許諾・帰属表示・対応ソース等の条件を別に確認してください。個人のPCで公式配布元から取得できることは、そのフォルダー全体の再配布許諾の証明ではありません。

Redistributing a fully installed folder requires a separate audit of its actual contents and all applicable third-party obligations. The source-only package audit does not establish permission to redistribute an installed runtime or GPU libraries.
