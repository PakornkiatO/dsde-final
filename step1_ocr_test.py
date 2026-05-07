"""
ขั้นที่ 1: ทดสอบ Typhoon OCR กับเอกสารผลการเลือกตั้ง 1 ไฟล์
================================================================

เป้าหมาย:
- เชื่อมต่อ Typhoon API ให้ได้
- รัน OCR ทุกหน้าของ PDF 1 ไฟล์
- บันทึก raw markdown ลงโฟลเดอร์ output/ เพื่อตรวจคุณภาพ
  ก่อนออกแบบ parser ในขั้นต่อไป

ก่อนรันครั้งแรก:
  1) pip install typhoon-ocr pypdf python-dotenv
  2) Linux:  sudo apt-get install -y poppler-utils
     macOS:  brew install poppler
  3) สร้างไฟล์ .env ในโฟลเดอร์เดียวกับสคริปต์ มีเนื้อหาแบบนี้:
        TYPHOON_OCR_API_KEY=sk-xxxxxxxx
     (สมัครฟรีที่ https://opentyphoon.ai)
     อย่าลืมเพิ่ม .env ไว้ใน .gitignore กัน key หลุดเข้า git

วิธีใช้:
  python step1_ocr_test.py /path/to/election.pdf
หรือแก้ค่า PDF_PATH ด้านล่างแล้วรัน python step1_ocr_test.py เฉยๆ
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pypdf import PdfReader
from typhoon_ocr import ocr_document

# โหลดตัวแปรจากไฟล์ .env (ถ้ามี) เข้าไปยัง os.environ
# จะมองหา .env ที่ working directory ปัจจุบันก่อน
load_dotenv()


# ============ ปรับ config ตรงนี้ ============
PDF_PATH = "election_doc.pdf"   # ใส่ path ไฟล์ PDF (หรือส่งทาง argv)
OUTPUT_DIR = "output"            # โฟลเดอร์เก็บ markdown ราย-หน้า
MODEL = "typhoon-ocr"            # v1.5 (default, recommended)
RATE_LIMIT_DELAY = 0.6           # วินาทีระหว่าง request — Typhoon จำกัด 2 req/s
MAX_RETRIES = 3                  # ลอง retry หากเจอ error ชั่วคราว
# ============================================


def ocr_one_page(pdf_path: Path, page_num: int) -> str:
    """เรียก Typhoon OCR กับหน้าที่ระบุ พร้อม retry แบบ exponential backoff"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return ocr_document(
                pdf_or_image_path=str(pdf_path),
                page_num=page_num,
                model=MODEL,
            )
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise
            wait = 2 ** attempt  # 2, 4, 8 วินาที
            print(f"      ⚠️  {type(e).__name__}: {e} → รอ {wait}s แล้วลองใหม่")
            time.sleep(wait)
    return ""  # never reached


def ocr_pdf_all_pages(pdf_path: Path, output_dir: Path) -> None:
    if not pdf_path.exists():
        sys.exit(f"❌ ไม่พบไฟล์: {pdf_path}")
    if not os.environ.get("TYPHOON_OCR_API_KEY"):
        sys.exit(
            "❌ ไม่พบ TYPHOON_OCR_API_KEY\n"
            "   กรุณาสร้างไฟล์ .env ในโฟลเดอร์เดียวกับสคริปต์ แล้วเพิ่มบรรทัด:\n"
            "   TYPHOON_OCR_API_KEY=sk-xxxxxxxx"
        )

    # นับหน้า
    try:
        n_pages = len(PdfReader(str(pdf_path)).pages)
    except Exception as e:
        sys.exit(f"❌ อ่าน PDF ไม่ได้: {e}")

    out_dir = output_dir / pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"📄 {pdf_path.name}  ({n_pages} หน้า)")
    print(f"📁 เซฟไปที่: {out_dir}/\n")

    failed: list[int] = []
    for page in range(1, n_pages + 1):
        out_file = out_dir / f"page_{page:03d}.md"

        # skip ถ้ามีอยู่แล้ว (เผื่อรันซ้ำ)
        if out_file.exists() and out_file.stat().st_size > 0:
            print(f"  ⏭  หน้า {page:>3}: มีไฟล์แล้ว — ข้าม")
            continue

        print(f"  🔍 หน้า {page:>3}/{n_pages} …", end=" ", flush=True)
        try:
            md = ocr_one_page(pdf_path, page)
            out_file.write_text(md, encoding="utf-8")
            preview = md.replace("\n", " ")[:60]
            print(f"✅ {len(md):>5} chars  | {preview}…")
        except Exception as e:
            print(f"❌ {type(e).__name__}: {e}")
            failed.append(page)

        time.sleep(RATE_LIMIT_DELAY)

    # สรุป
    print()
    done = n_pages - len(failed)
    print(f"✨ เสร็จ {done}/{n_pages} หน้า")
    if failed:
        print(f"⚠️  หน้าที่พลาด: {failed}")
        print("   → ลองรันสคริปต์เดิมอีกครั้ง (หน้าที่สำเร็จแล้วจะถูกข้าม)")


def main() -> None:
    pdf = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(PDF_PATH)
    ocr_pdf_all_pages(pdf, Path(OUTPUT_DIR))


if __name__ == "__main__":
    main()