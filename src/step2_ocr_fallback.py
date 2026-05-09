"""
step2_ocr_fallback.py — OCR เฉพาะหน้าที่ failed ด้วยโมเดลอื่น
================================================================

ใช้เมื่อ step2_ocr_batch.py --retry-failed ติด 408 timeout ตลอด
อ่านรายการหน้าที่ค้างจาก reports/ocr_failures.json แล้วลอง OCR
ด้วยโมเดลที่ระบุผ่าน --model (default: typhoon-ocr-preview)

วิธีใช้:
  python src/step2_ocr_fallback.py --model typhoon-ocr-preview
  python src/step2_ocr_fallback.py --model <model-name> --workers 2
"""

from __future__ import annotations

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
from tqdm import tqdm
from typhoon_ocr.ocr_utils import prepare_ocr_messages

load_dotenv()

# ─── Paths ────────────────────────────────────────────────────────────────
_ROOT       = Path(__file__).resolve().parent.parent
DATA_DIR    = _ROOT / "data"
OUTPUT_DIR  = _ROOT / "output"
REPORTS_DIR = _ROOT / "reports"
FAILED_LOG  = REPORTS_DIR / "ocr_failures.json"

# ─── Config ───────────────────────────────────────────────────────────────
DEFAULT_MODEL   = "typhoon-ocr-preview"
DEFAULT_WORKERS = 1          # ลด load บน server — เพิ่มได้ถ้า API รับไหว
RATE_PER_SEC    = 1.0        # conservative กว่า step2 เดิม
MAX_RETRIES     = 3
BASE_TIMEOUT    = 300.0
TIMEOUT_GROWTH  = 1.5
BACKOFF_BASE    = 3.0
BACKOFF_MAX     = 90.0

_state_lock = threading.Lock()


# ─── Rate limiter ─────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, rate: float):
        self.min_interval = 1.0 / rate
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


# ─── Atomic writes ────────────────────────────────────────────────────────
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


def save_failures(failures: dict) -> None:
    with _state_lock:
        if failures:
            atomic_write_json(FAILED_LOG, failures)
        elif FAILED_LOG.exists():
            FAILED_LOG.unlink()


# ─── Error classification ─────────────────────────────────────────────────
_TRANSIENT = (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.RemoteProtocolError,
)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT):
        return True
    if isinstance(exc, APIStatusError):
        # รวม 408 เป็น transient เพราะเป็น server-side timeout ที่ retry ได้
        return exc.status_code in (408, 429) or exc.status_code >= 500
    if isinstance(exc, RuntimeError) and "empty OCR response" in str(exc):
        return True
    return False


# ─── OCR ──────────────────────────────────────────────────────────────────
_client: OpenAI | None = None
_client_model: str = ""


def get_client(model: str) -> OpenAI:
    global _client, _client_model
    if _client is None or _client_model != model:
        _client = OpenAI(
            base_url=os.getenv("TYPHOON_BASE_URL", "https://api.opentyphoon.ai/v1"),
            api_key=os.getenv("TYPHOON_OCR_API_KEY"),
            timeout=BASE_TIMEOUT,
            max_retries=0,
        )
        _client_model = model
    return _client


def ocr_one_page(pdf_path: Path, page_num: int, model: str) -> str:
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
            response = (
                get_client(model)
                .with_options(timeout=timeout)
                .chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=16384,
                    temperature=0.1,
                    top_p=0.6,
                    extra_body={"repetition_penalty": 1.1},
                )
            )
            content = response.choices[0].message.content or ""
            if not content.strip():
                raise RuntimeError("empty OCR response")
            return content
        except Exception as exc:
            last_exc = exc
            if attempt == MAX_RETRIES or not is_transient(exc):
                raise
            wait = min(BACKOFF_MAX, BACKOFF_BASE**attempt) + random.uniform(0, 2.0)
            tqdm.write(
                f"    ⚠  retry {attempt}/{MAX_RETRIES} "
                f"(timeout={timeout:.0f}s, {type(exc).__name__}): wait {wait:.1f}s"
            )
            time.sleep(wait)
            timeout *= TIMEOUT_GROWTH

    assert last_exc is not None
    raise last_exc


# ─── Job ──────────────────────────────────────────────────────────────────
@dataclass
class PageJob:
    pdf_path: Path
    page: int
    out_file: Path
    rel_str: str
    model: str


@dataclass
class PageResult:
    job: PageJob
    ok: bool
    error: str | None = None


def process_page(job: PageJob) -> PageResult:
    if job.out_file.exists() and job.out_file.stat().st_size > 0:
        return PageResult(job, ok=True)
    try:
        md = ocr_one_page(job.pdf_path, job.page, job.model)
        job.out_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(job.out_file, md)
        return PageResult(job, ok=True)
    except Exception as exc:
        return PageResult(job, ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")


# ─── Main ─────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="OCR fallback ด้วยโมเดลอื่น")
    parser.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"ชื่อโมเดล (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKERS,
        help=f"จำนวน parallel workers (default {DEFAULT_WORKERS})",
    )
    args = parser.parse_args()

    if not os.environ.get("TYPHOON_OCR_API_KEY"):
        sys.exit("❌ ไม่พบ TYPHOON_OCR_API_KEY ใน .env")

    failures: dict[str, list[int]] = load_json(FAILED_LOG)
    if not failures:
        print("✅ ไม่มีหน้าที่ค้างใน ocr_failures.json")
        return

    total_pages = sum(len(v) for v in failures.values())
    print(f"📂 data:    {DATA_DIR}")
    print(f"📁 output:  {OUTPUT_DIR}")
    print(f"🤖 model:   {args.model}")
    print(f"🧵 workers: {args.workers}")
    print(f"📋 failed:  {total_pages} หน้า ใน {len(failures)} PDF\n")

    # สร้าง job list
    jobs: list[PageJob] = []
    skipped_not_found: int = 0
    for rel_str, pages in failures.items():
        pdf_path = DATA_DIR / rel_str
        if not pdf_path.exists():
            print(f"⚠  ข้าม (ไม่พบไฟล์): {rel_str}")
            skipped_not_found += 1
            continue
        rel = Path(rel_str)
        out_dir = OUTPUT_DIR / rel.parent / rel.stem
        for page in pages:
            out_file = out_dir / f"page_{page:03d}.md"
            if out_file.exists() and out_file.stat().st_size > 0:
                continue
            jobs.append(PageJob(pdf_path, page, out_file, rel_str, args.model))

    if not jobs:
        if skipped_not_found > 0:
            print(f"\n⚠  ไม่มี job เพราะไม่พบไฟล์ PDF {skipped_not_found} ไฟล์ — ocr_failures.json ยังคงไว้")
        else:
            print("🎉 ทุกหน้าใน failures มีผลแล้ว — ลบ ocr_failures.json")
            FAILED_LOG.unlink(missing_ok=True)
        return

    print(f"📑 หน้าที่ต้อง OCR: {len(jobs)}\n")

    completed = failed_now = 0
    save_every = 10

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process_page, j): j for j in jobs}
        bar = tqdm(total=len(jobs), unit="page", desc="OCR fallback", ncols=80)
        try:
            for fut in as_completed(futures):
                result = fut.result()
                job = result.job
                if result.ok:
                    completed += 1
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
                    save_failures(failures)
        except KeyboardInterrupt:
            tqdm.write("\n⚠  หยุดด้วย Ctrl-C — บันทึก state…")
            executor.shutdown(wait=False, cancel_futures=True)
            save_failures(failures)
            sys.exit(1)
        finally:
            bar.close()

    save_failures(failures)

    print(f"\n{'='*60}")
    print(f"✨ สำเร็จ {completed} หน้า / พลาด {failed_now} หน้า")
    if failures:
        n_left = sum(len(v) for v in failures.values())
        print(f"⚠  ยังเหลือ {n_left} หน้า — ลองโมเดลอื่นหรือตรวจสอบไฟล์")
    else:
        print("🎉 ครบทุกหน้า!")


if __name__ == "__main__":
    main()
