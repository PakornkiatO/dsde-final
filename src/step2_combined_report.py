"""
step3_combined_report.py — รวม flag จาก text-match และ checksum เข้าด้วยกัน

output structure (combined_report.json):
{
  "<pdf_dir>": {
    "text_match": [
      {"page": "page_003.md", "number": 256, "found": "...", "expected": "..."},
      ...
    ],
    "checksum": [
      {"check": "ballot_total", "expected": 800, "calculated": 799, "detail": "..."},
      ...
    ]
  },
  ...
}
"""
from __future__ import annotations

import json
from pathlib import Path

from step3_validate import validate_file
from step3_checksum import checksum_pdf, find_pdf_dirs

_ROOT       = Path(__file__).resolve().parent.parent
OUTPUT_DIR  = _ROOT / "output"
REPORT_FILE = _ROOT / "reports" / "combined_report.json"


def main() -> None:
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    pdf_dirs = find_pdf_dirs(OUTPUT_DIR)
    print(f"🔍 ตรวจสอบ {len(pdf_dirs)} PDF...")

    report: dict[str, dict] = {}
    n_text = 0
    n_checksum = 0

    for pdf_dir in pdf_dirs:
        rel_dir = str(pdf_dir.relative_to(OUTPUT_DIR))
        entry: dict = {}

        # ── text-match: ตรวจทีละหน้า ──
        text_issues = []
        for page in sorted(pdf_dir.glob("page_*.md")):
            page_issues = validate_file(page)
            for issue in page_issues:
                text_issues.append({"page": page.name, **issue})
        if text_issues:
            entry["text_match"] = text_issues
            n_text += len(text_issues)

        # ── checksum: ตรวจระดับ PDF ──
        cs_issues = checksum_pdf(pdf_dir)
        if cs_issues:
            entry["checksum"] = cs_issues
            n_checksum += len(cs_issues)

        if entry:
            report[rel_dir] = entry

    with open(REPORT_FILE, "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)

    n_text_pages = sum(1 for e in report.values() if e.get("text_match"))
    n_text_issues = sum(len(e["text_match"]) for e in report.values() if e.get("text_match"))
    n_cs_pdfs = sum(1 for e in report.values() if e.get("checksum"))
    flagged = len(report)

    print(f"\n{'='*60}")
    print(f"📋 text_match  : {n_text_issues} จุด  ใน {n_text_pages} PDF  (นับระดับ page issue)")
    print(f"🔢 checksum    : {n_checksum} จุด  ใน {n_cs_pdfs} PDF")
    print(f"📁 PDF ที่มีปัญหา (รวม): {flagged} / {len(pdf_dirs)}  (PDF ที่มีอย่างน้อย 1 จากทั้งสอง)")
    print(f"📄 รายงาน: {REPORT_FILE.resolve()}")

    shown = 0
    for path, entry in report.items():
        if shown >= 15:
            print(f"\n  ... (ดูทั้งหมดใน {REPORT_FILE})")
            break
        print(f"\n  📌 {path}")
        for issue in entry.get("text_match", []):
            print(f"     [text]     {issue['page']}  #{issue['number']}  พบ: \"{issue['found']}\"  คาดหวัง: \"{issue['expected']}\"")
            shown += 1
        for issue in entry.get("checksum", []):
            print(f"     [checksum] {issue['detail']}")
            shown += 1


if __name__ == "__main__":
    main()
