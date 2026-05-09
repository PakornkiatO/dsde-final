"""
ขั้นที่ 2: OCR ทุกไฟล์ PDF ในโฟลเดอร์ data/ (parallel + robust)
================================================================

เปลี่ยนจากเวอร์ชันเดิม:
- รัน OCR แบบ parallel (default 4 workers) — เร็วขึ้น ~4-6x
- Token-bucket rate limiter ที่ระดับ global (1.8 req/s, ไม่เกิน Typhoon limit)
- Timeout 180s + ขยายขึ้นทุก retry
- Retry 6 ครั้ง พร้อม jitter, แยก transient vs terminal error
- Atomic writes — ปลอดภัยแม้กด Ctrl-C
- Track per-page (พลาดหน้าเดียวก็ retry แค่หน้านั้น)
- Resume สมบูรณ์ทุกระดับ

วิธีใช้:
  python step2_ocr_batch.py                  # รันทั้งหมด
  python step2_ocr_batch.py --workers 6      # เพิ่ม concurrency (ถ้าเครือข่าย/CPU ไหว)
  python step2_ocr_batch.py --retry-failed   # รันเฉพาะหน้าที่ค้าง
  python step2_ocr_batch.py --limit 5        # ทดสอบกับ 5 ไฟล์แรก
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)
from pypdf import PdfReader
from tqdm import tqdm
from typhoon_ocr.ocr_utils import prepare_ocr_messages

load_dotenv()

# ─── Config ──────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR    = _ROOT / "data"
OUTPUT_DIR  = _ROOT / "output"
REPORTS_DIR = _ROOT / "reports"
MODEL = "typhoon-ocr"

DEFAULT_WORKERS = 4
RATE_PER_SEC = 1.8                # Typhoon limit คือ 2 req/s, เผื่อ buffer

MAX_RETRIES = 1
BASE_TIMEOUT = 180.0              # OCR หน้ายากๆ ใช้เวลา > 90s ได้
TIMEOUT_GROWTH = 1.4              # 180 → 252 → 353 → 494 → 691 → 968
BACKOFF_BASE = 2.0
BACKOFF_MAX = 60.0

FAILED_LOG = REPORTS_DIR / "ocr_failures.json"
DONE_LOG   = REPORTS_DIR / "ocr_done.json"

_state_lock = threading.Lock()


# ─── Rate limiter (token bucket) ─────────────────────────────────────────
class RateLimiter:
    """จำกัด rate ระดับ global — ทุก thread แชร์กัน"""

    def __init__(self, rate_per_sec: float):
        self.min_interval = 1.0 / rate_per_sec
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_slot - now)
            self.next_slot = max(now, self.next_slot) + self.min_interval
        if wait > 0:
            time.sleep(wait)


_rate_limiter = RateLimiter(RATE_PER_SEC)


# ─── Atomic writes ───────────────────────────────────────────────────────
def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(failures: dict) -> None:
    with _state_lock:
        atomic_write_json(FAILED_LOG, failures)


# ─── Error classification ────────────────────────────────────────────────
TRANSIENT = (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, TRANSIENT):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code >= 500 or exc.status_code == 429 or exc.status_code == 408
    if isinstance(exc, RuntimeError) and "empty OCR response" in str(exc):
        return True
    return False


# ─── OCR client ──────────────────────────────────────────────────────────
_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=os.getenv("TYPHOON_BASE_URL", "https://api.opentyphoon.ai/v1"),
            api_key=os.getenv("TYPHOON_OCR_API_KEY"),
            timeout=BASE_TIMEOUT,
            max_retries=0,  # เราจัดการ retry เอง
        )
    return _client


def ocr_one_page(pdf_path: Path, page_num: int) -> str:
    """OCR หน้าเดียว พร้อม retry + timeout ที่ขยายขึ้นเรื่อยๆ"""
    messages = prepare_ocr_messages(
        pdf_or_image_path=str(pdf_path),
        task_type="v1.5",
        page_num=page_num,
        figure_language="Thai",
    )
    timeout = BASE_TIMEOUT
    last_exc: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        _rate_limiter.acquire()
        try:
            response = get_client().with_options(timeout=timeout).chat.completions.create(
                model=MODEL,
                messages=messages,
                max_tokens=16384,
                temperature=0.1,
                top_p=0.6,
                extra_body={"repetition_penalty": 1.1},
            )
            content = response.choices[0].message.content or ""
            if not content.strip():
                raise RuntimeError("empty OCR response")
            return content
        except Exception as exc:
            last_exc = exc
            if attempt == MAX_RETRIES or not is_transient(exc):
                raise
            wait = min(BACKOFF_MAX, BACKOFF_BASE ** attempt) + random.uniform(0, 1.5)
            tqdm.write(
                f"    ⚠  retry {attempt}/{MAX_RETRIES} "
                f"(timeout={timeout:.0f}s): {type(exc).__name__} → wait {wait:.1f}s"
            )
            time.sleep(wait)
            timeout *= TIMEOUT_GROWTH

    assert last_exc is not None
    raise last_exc


# ─── Job & worker ────────────────────────────────────────────────────────
@dataclass
class PageJob:
    pdf_path: Path
    page: int
    out_file: Path
    rel_str: str          # ใช้เป็น key ใน failures (stable ข้าม run)


@dataclass
class PageResult:
    job: PageJob
    ok: bool
    error: str | None = None


def process_page(job: PageJob) -> PageResult:
    if job.out_file.exists() and job.out_file.stat().st_size > 0:
        return PageResult(job, ok=True)
    try:
        md = ocr_one_page(job.pdf_path, job.page)
        job.out_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(job.out_file, md)
        return PageResult(job, ok=True)
    except Exception as exc:
        return PageResult(job, ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")


# ─── Build job queue ─────────────────────────────────────────────────────
def count_pages(pdf_path: Path) -> int:
    return len(PdfReader(str(pdf_path)).pages)


def build_jobs(
    pdfs: list[Path],
    data_root: Path,
    output_root: Path,
    only_pages: dict[str, list[int]] | None = None,
) -> tuple[list[PageJob], dict[str, int]]:
    """
    สร้าง list ของ page jobs ที่ต้องทำ (ข้ามหน้าที่ทำแล้ว)
    only_pages: ถ้ามี ทำแค่หน้าใน list นี้ (key = rel_str)
    """
    jobs: list[PageJob] = []
    pages_per_pdf: dict[str, int] = {}

    for pdf_path in tqdm(pdfs, desc="🔍 สแกน", unit="pdf", ncols=70):
        try:
            rel = pdf_path.relative_to(data_root)
        except ValueError:
            rel = Path(pdf_path.name)
        rel_str = str(rel)
        out_dir = output_root / rel.parent / rel.stem

        try:
            n_pages = count_pages(pdf_path)
        except Exception as exc:
            tqdm.write(f"❌ อ่าน PDF ไม่ได้: {rel_str} ({type(exc).__name__}: {exc})")
            continue
        pages_per_pdf[rel_str] = n_pages

        target_pages: list[int]
        if only_pages is not None:
            target_pages = only_pages.get(rel_str, [])
        else:
            target_pages = list(range(1, n_pages + 1))

        for page in target_pages:
            if not (1 <= page <= n_pages):
                continue
            out_file = out_dir / f"page_{page:03d}.md"
            if out_file.exists() and out_file.stat().st_size > 0:
                continue
            jobs.append(
                PageJob(pdf_path=pdf_path, page=page, out_file=out_file, rel_str=rel_str)
            )

    return jobs, pages_per_pdf


# ─── Main ────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Batch OCR (parallel, robust)")
    parser.add_argument("--data-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=None, help="ทดสอบกับ N ไฟล์แรก")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"จำนวน parallel workers (default {DEFAULT_WORKERS})")
    parser.add_argument("--retry-failed", action="store_true",
                        help="รันเฉพาะหน้าที่พลาดจาก ocr_failures.json")
    args = parser.parse_args()

    if not os.environ.get("TYPHOON_OCR_API_KEY"):
        sys.exit("❌ ไม่พบ TYPHOON_OCR_API_KEY ใน .env")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    data_root: Path = args.data_dir
    output_root: Path = args.output_dir

    # โหลด failures เดิมเสมอ (จะ merge กับของรอบนี้)
    failures: dict[str, list[int]] = load_json(FAILED_LOG)

    if args.retry_failed:
        if not failures:
            print("✅ ไม่มีหน้าที่ค้างใน ocr_failures.json")
            return
        # รีคอนสตรัค absolute path จาก rel_str + data_root ปัจจุบัน
        pdfs: list[Path] = []
        only_pages: dict[str, list[int]] = {}
        for rel_str, pages in failures.items():
            abs_path = data_root / rel_str
            if abs_path.exists():
                pdfs.append(abs_path)
                only_pages[rel_str] = list(pages)
            else:
                print(f"⚠  ข้าม (ไม่พบไฟล์): {rel_str}")
        total_pages_to_retry = sum(len(v) for v in only_pages.values())
        print(f"🔁 retry {total_pages_to_retry} หน้า ใน {len(pdfs)} ไฟล์")
    else:
        pdfs = sorted(data_root.rglob("*.pdf"))
        only_pages = None
        if not pdfs:
            sys.exit(f"❌ ไม่พบ PDF ใน {data_root}")
        if args.limit:
            pdfs = pdfs[: args.limit]

    print(f"📂 data:    {data_root.resolve()}")
    print(f"📁 output:  {output_root.resolve()}")
    print(f"📄 PDFs:    {len(pdfs)}")
    print(f"🧵 workers: {args.workers}")
    print(f"⏱  rate:    {RATE_PER_SEC} req/s\n")

    jobs, pages_per_pdf = build_jobs(pdfs, data_root, output_root, only_pages)
    print(f"\n📑 หน้าที่ต้อง OCR: {len(jobs)} (ข้ามหน้าที่ทำแล้ว)\n")

    if not jobs:
        print("🎉 ครบแล้วทุกหน้า — ไม่มีอะไรต้องทำ")
        if FAILED_LOG.exists() and not failures:
            FAILED_LOG.unlink()
        return

    completed = 0
    failed_now = 0
    save_every = 25

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_page, j): j for j in jobs}
        bar = tqdm(total=len(jobs), unit="page", desc="OCR", ncols=80)
        try:
            for fut in as_completed(futures):
                result = fut.result()
                job = result.job
                if result.ok:
                    completed += 1
                    # ถ้าเคยพลาด ลบออกจาก failures
                    if job.rel_str in failures and job.page in failures[job.rel_str]:
                        failures[job.rel_str].remove(job.page)
                        if not failures[job.rel_str]:
                            del failures[job.rel_str]
                else:
                    failed_now += 1
                    pages = failures.setdefault(job.rel_str, [])
                    if job.page not in pages:
                        pages.append(job.page)
                    pages.sort()
                    tqdm.write(f"❌ {job.rel_str} หน้า {job.page}: {result.error}")

                bar.update(1)
                bar.set_postfix(ok=completed, fail=failed_now)

                if (completed + failed_now) % save_every == 0:
                    save_state(failures)
        except KeyboardInterrupt:
            tqdm.write("\n⚠  หยุดด้วย Ctrl-C — บันทึก state ก่อนออก…")
            executor.shutdown(wait=False, cancel_futures=True)
            save_state(failures)
            sys.exit(1)
        finally:
            bar.close()

    save_state(failures)

    # อัพเดท done log
    done = load_json(DONE_LOG)
    for rel_str, n_pages in pages_per_pdf.items():
        if rel_str in failures:
            continue
        rel = Path(rel_str)
        out_dir = output_root / rel.parent / rel.stem
        n_done = sum(1 for p in out_dir.glob("page_*.md") if p.stat().st_size > 0)
        if n_done >= n_pages:
            done[rel_str] = {
                "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "pages": n_pages,
            }
    with _state_lock:
        atomic_write_json(DONE_LOG, done)

    # สรุป
    print(f"\n{'=' * 60}")
    print(f"✨ สำเร็จ {completed} หน้า / พลาด {failed_now} หน้า")
    if failures:
        n_left = sum(len(v) for v in failures.values())
        print(f"⚠  ยังเหลือ {n_left} หน้า ใน {len(failures)} ไฟล์")
        print(f"   รัน:  python {Path(sys.argv[0]).name} --retry-failed")
    else:
        if FAILED_LOG.exists():
            FAILED_LOG.unlink()
        print("🎉 ครบทุกหน้า!")


if __name__ == "__main__":
    main()