"""
ขั้นที่ 2: OCR ทุกไฟล์ PDF ในโฟลเดอร์ data/ แบบ batch
==========================================================

สคริปต์นี้สแกน data/ หา PDF ทั้งหมด แล้วรัน Typhoon OCR ทุกไฟล์
โดยเก็บผล markdown ไว้ใน output/ โดย mirror โครงสร้างโฟลเดอร์จาก data/

ตัวอย่าง:
  data/เขตเลือกตั้งที่_2/อำเภอไชโย/ตำบลชัยฤทธิ์/หน่วยเลือกตั้งที่ 1/ส.ส. 5-18.pdf
  → output/เขตเลือกตั้งที่_2/อำเภอไชโย/ตำบลชัยฤทธิ์/หน่วยเลือกตั้งที่ 1/ส.ส. 5-18/page_001.md

วิธีใช้:
  python step2_ocr_batch.py                     # ประมวลผลทุกไฟล์ใน data/
  python step2_ocr_batch.py --limit 10          # ทดสอบกับ 10 ไฟล์แรก
  python step2_ocr_batch.py --data-dir /path    # กำหนด data folder
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

from dotenv import load_dotenv
from pypdf import PdfReader
from tqdm import tqdm
from typhoon_ocr import ocr_document

load_dotenv()

DATA_DIR = Path("data")
OUTPUT_DIR = Path("output")
MODEL = "typhoon-ocr"
RATE_LIMIT_DELAY = 0.6   # 2 req/s limit from Typhoon
MAX_RETRIES = 1
PAGE_TIMEOUT = 180        # seconds before treating a page call as hung
FAILED_LOG = Path("ocr_failures.json")


def ocr_one_page(pdf_path: Path, page_num: int) -> str:
    for attempt in range(1, MAX_RETRIES + 1):
        # Fresh executor each attempt so a stuck thread never blocks the next call.
        # (ThreadPoolExecutor threads can't be killed; abandoning the executor lets
        # the OS eventually reap the hung thread while we move on.)
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(
                ocr_document,
                pdf_or_image_path=str(pdf_path),
                page_num=page_num,
                model=MODEL,
            )
            return future.result(timeout=PAGE_TIMEOUT)
        except FuturesTimeout:
            executor.shutdown(wait=False)   # abandon the hung thread
            if attempt == MAX_RETRIES:
                raise TimeoutError(f"หน้า {page_num} ไม่ตอบสนองใน {PAGE_TIMEOUT}s ({MAX_RETRIES} ครั้ง)")
            tqdm.write(f"    ⏱  timeout หน้า {page_num} ครั้งที่ {attempt}/{MAX_RETRIES} – ลองใหม่")
        except Exception as exc:
            executor.shutdown(wait=False)
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt
            tqdm.write(f"    ⚠  retry {attempt}/{MAX_RETRIES} – {type(exc).__name__}: {exc} (wait {wait}s)")
            time.sleep(wait)
        else:
            executor.shutdown(wait=False)
    return ""


def output_dir_for(pdf_path: Path, data_root: Path, output_root: Path) -> Path:
    rel = pdf_path.relative_to(data_root)
    return output_root / rel.parent / rel.stem


def count_pages(pdf_path: Path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def is_already_done(out_dir: Path, n_pages: int) -> bool:
    if not out_dir.exists():
        return False
    done = sum(
        1 for p in out_dir.glob("page_*.md") if p.stat().st_size > 0
    )
    return done >= n_pages


def ocr_pdf(
    pdf_path: Path,
    out_dir: Path,
    rel_path: str,
    file_idx: int,
    total: int,
) -> list[int]:
    """Process one PDF. Returns list of failed page numbers."""
    prefix = f"[{file_idx}/{total}]"

    try:
        n_pages = count_pages(pdf_path)
    except Exception as exc:
        tqdm.write(f"{prefix} ❌ อ่าน PDF ไม่ได้: {rel_path}  ({type(exc).__name__}: {exc})")
        return [-1]

    if is_already_done(out_dir, n_pages):
        tqdm.write(f"{prefix} ⏭  ข้าม (ครบแล้ว): {rel_path}")
        return []

    tqdm.write(f"{prefix} ▶  เริ่ม: {rel_path}  ({n_pages} หน้า)")
    out_dir.mkdir(parents=True, exist_ok=True)
    failed: list[int] = []
    t0 = time.monotonic()

    page_bar = tqdm(
        range(1, n_pages + 1),
        desc="  หน้า",
        unit="p",
        leave=False,
        ncols=70,
        disable=False,
    )
    for page in page_bar:
        out_file = out_dir / f"page_{page:03d}.md"
        if out_file.exists() and out_file.stat().st_size > 0:
            page_bar.set_postfix_str("ข้าม")
            continue

        page_bar.set_postfix_str("กำลัง OCR…")
        try:
            md = ocr_one_page(pdf_path, page)
            out_file.write_text(md, encoding="utf-8")
            page_bar.set_postfix_str(f"✓ {len(md)} chars")
        except Exception as exc:
            tqdm.write(f"  {prefix} ❌ หน้า {page}: {type(exc).__name__}: {exc}")
            failed.append(page)
            page_bar.set_postfix_str("❌ failed")

        time.sleep(RATE_LIMIT_DELAY)

    elapsed = time.monotonic() - t0
    status = f"❌ พลาด {len(failed)} หน้า" if failed else "✅ สำเร็จ"
    tqdm.write(f"{prefix} {status}: {rel_path}  ({elapsed:.0f}s)")
    return failed


def load_failures() -> dict:
    if FAILED_LOG.exists():
        return json.loads(FAILED_LOG.read_text(encoding="utf-8"))
    return {}


def save_failures(failures: dict) -> None:
    FAILED_LOG.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch OCR ไฟล์ election PDF ทั้งหมด")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=None, help="ประมวลผลแค่ N ไฟล์แรก (ทดสอบ)")
    parser.add_argument("--retry-failed", action="store_true", help="ลองใหม่เฉพาะไฟล์ที่พลาดจาก ocr_failures.json")
    args = parser.parse_args()

    if not os.environ.get("TYPHOON_OCR_API_KEY"):
        sys.exit(
            "❌ ไม่พบ TYPHOON_OCR_API_KEY\n"
            "   สร้างไฟล์ .env แล้วเพิ่ม: TYPHOON_OCR_API_KEY=sk-xxxxxxxx"
        )

    data_root: Path = args.data_dir
    output_root: Path = args.output_dir

    if args.retry_failed:
        prev = load_failures()
        if not prev:
            print("ไม่มีไฟล์ที่ค้างอยู่ใน ocr_failures.json")
            return
        pdfs = [Path(p) for p in prev.keys()]
        print(f"🔁 retry {len(pdfs)} ไฟล์ที่พลาดก่อนหน้า")
    else:
        pdfs = sorted(data_root.rglob("*.pdf"))
        if not pdfs:
            sys.exit(f"❌ ไม่พบ PDF ใดเลยใน {data_root}")
        if args.limit:
            pdfs = pdfs[: args.limit]

    print(f"📂 data:   {data_root.resolve()}")
    print(f"📁 output: {output_root.resolve()}")
    print(f"📄 PDF ทั้งหมด: {len(pdfs)} ไฟล์\n")

    all_failures: dict[str, list[int]] = load_failures() if args.retry_failed else {}
    session_failures: dict[str, list[int]] = {}

    total = len(pdfs)
    with tqdm(enumerate(pdfs, 1), total=total, unit="file", desc="ไฟล์ทั้งหมด", disable=False) as file_bar:
        for file_idx, pdf_path in file_bar:
            try:
                rel_for_output = pdf_path.relative_to(data_root)
            except ValueError:
                rel_for_output = Path(pdf_path.name)

            rel_str = str(rel_for_output)
            file_bar.set_description(rel_str[-55:] if len(rel_str) > 55 else rel_str)

            out_dir = output_root / rel_for_output.parent / rel_for_output.stem
            failed = ocr_pdf(pdf_path, out_dir, rel_str, file_idx, total)

            if failed:
                session_failures[str(pdf_path)] = failed
                all_failures[str(pdf_path)] = failed
            elif str(pdf_path) in all_failures:
                del all_failures[str(pdf_path)]

    # สรุปผล
    fail_count = len(session_failures)
    print(f"\n{'='*60}")
    print(f"✨ เสร็จ {total - fail_count}/{total} ไฟล์")
    if session_failures:
        print(f"⚠  ไฟล์ที่พลาด ({fail_count}):")
        for p, pages in session_failures.items():
            print(f"   {p}  →  หน้า {pages}")
        save_failures(all_failures)
        print(f"\n💾 บันทึกรายการพลาดไว้ที่ {FAILED_LOG}")
        print("   รันใหม่ด้วย --retry-failed เพื่อลองอีกครั้ง")
    else:
        if FAILED_LOG.exists() and not all_failures:
            FAILED_LOG.unlink()
            print("🗑  ลบ ocr_failures.json (ครบทุกไฟล์แล้ว)")


if __name__ == "__main__":
    main()
