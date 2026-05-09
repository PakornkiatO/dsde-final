"""
step3_checksum.py — Checksum validation ของบัตรเลือกตั้ง

ตรวจสอง checksum ต่อหนึ่ง PDF:
  1. ballot_total : บัตรดี + บัตรเสีย + บัตรที่ไม่เลือก = บัตรที่ได้รับทั้งหมด
  2. score_total  : ผลรวมคะแนนผู้สมัคร/พรรค = รวมคะแนนทั้งสิ้น

ทำงานระดับ PDF (รวมทุกหน้าก่อน) เพื่อรองรับตารางที่ข้ามหลายหน้า
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from step3_validate import strip_readings

_ROOT       = Path(__file__).resolve().parent.parent
OUTPUT_DIR  = _ROOT / "output"
REPORT_FILE = _ROOT / "reports" / "checksum_report.json"

_THAI_MAP = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_D = r"[0-9๐-๙]+"


def _int(s: str) -> int:
    return int(s.translate(_THAI_MAP))


def _find(pat: re.Pattern, text: str) -> int | None:
    m = pat.search(text)
    return _int(m.group(1)) if m else None


# ─── Ballot summary patterns ──────────────────────────────────────────────
# ใช้ [\s.]* เพื่อข้ามจุดไข่ปลาและช่องว่างระหว่างป้ายกับตัวเลข
_TOTAL_RECV  = re.compile(rf"ได้รับบัตรเลือกตั้ง[\s.]*ทั้งหมด[\s.]*({_D})")
_GOOD_PAT    = re.compile(rf"บัตรดี[\s.]*({_D})")
_SPOILED_PAT = re.compile(rf"บัตรเสีย[\s.]*({_D})")
_BLANK_PAT   = re.compile(rf"ไม่เลือกผู้สมัครผู้ใด[\s.]*({_D})")

# ─── Score table patterns ─────────────────────────────────────────────────
_ROW_RE    = re.compile(r"<tr>(.*?)</tr>")
_CELL_RE   = re.compile(r"<td[^>]*>(.*?)</td>")
_NUM_ONLY  = re.compile(rf"^({_D})$")
# จับ total จากทั้ง 2 format:
#   format 1 (constituency): <td>รวมคะแนนทั้งสิ้น</td><td>699</td>
#   format 2 (party list):   <td>รวมคะแนนทั้งสิ้น 227</td>
_TOTAL_ROW = re.compile(rf"รวมคะแนน[^0-9๐-๙\n]*({_D})")


def _parse_scores(text: str) -> tuple[list[int], int | None]:
    """คืน (list คะแนนแต่ละแถว, stated total) จากตาราง HTML"""
    scores: list[int] = []
    stated_total: int | None = None

    for rm in _ROW_RE.finditer(text):
        row = rm.group(1)
        if "รวมคะแนน" in row:
            m = _TOTAL_ROW.search(row)
            if m:
                stated_total = _int(m.group(1))
        else:
            cells = [c.strip() for c in _CELL_RE.findall(row)]
            # เอาเซลล์ตัวเลขล้วนๆ เซลล์สุดท้าย (= คอลัมน์คะแนน)
            for cell in reversed(cells):
                nm = _NUM_ONLY.match(cell)
                if nm:
                    scores.append(_int(nm.group(1)))
                    break

    return scores, stated_total


# ─── PDF-level checksum ───────────────────────────────────────────────────
def checksum_pdf(pdf_dir: Path) -> list[dict]:
    pages = sorted(pdf_dir.glob("page_*.md"))
    if not pages:
        return []

    # รวมทุกหน้าก่อน — รองรับตารางที่ต่อข้ามหน้า
    combined = "\n".join(
        strip_readings(p.read_text(encoding="utf-8")) for p in pages
    )
    issues: list[dict] = []

    # Checksum 1: ballot total
    total = _find(_TOTAL_RECV,  combined)
    good  = _find(_GOOD_PAT,    combined)
    spoil = _find(_SPOILED_PAT, combined)
    blank = _find(_BLANK_PAT,   combined)

    if all(v is not None for v in [total, good, spoil, blank]):
        calc = good + spoil + blank  # type: ignore[operator]
        if calc != total:
            issues.append({
                "check": "ballot_total",
                "expected": total,
                "calculated": calc,
                "detail": (
                    f"บัตรดี({good}) + บัตรเสีย({spoil}) + ไม่เลือก({blank})"
                    f" = {calc}  ≠  รับมา {total}"
                ),
            })

    # Checksum 2: score total
    scores, stated_total = _parse_scores(combined)
    if scores and stated_total is not None:
        calc = sum(scores)
        if calc != stated_total:
            issues.append({
                "check": "score_total",
                "expected": stated_total,
                "calculated": calc,
                "detail": (
                    f"ผลรวมคะแนน {calc}  ≠  รวมคะแนนทั้งสิ้น {stated_total}"
                    f"  (n={len(scores)} แถว)"
                ),
            })

    return issues


# ─── Find all PDF output dirs ─────────────────────────────────────────────
def find_pdf_dirs(root: Path) -> list[Path]:
    seen: set[Path] = set()
    dirs: list[Path] = []
    for md in root.rglob("page_*.md"):
        d = md.parent
        if d not in seen:
            seen.add(d)
            dirs.append(d)
    return sorted(dirs)


# ─── Main ─────────────────────────────────────────────────────────────────
def main() -> None:
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    pdf_dirs = find_pdf_dirs(OUTPUT_DIR)
    print(f"🔍 ตรวจ checksum {len(pdf_dirs)} PDF...")

    report: dict[str, list[dict]] = {}
    total_issues = 0

    for d in pdf_dirs:
        rel = str(d.relative_to(OUTPUT_DIR))
        issues = checksum_pdf(d)
        if issues:
            report[rel] = issues
            total_issues += len(issues)

    with open(REPORT_FILE, "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)

    flagged = len(report)
    print(f"\n{'='*60}")
    print(f"🔎 พบ {total_issues} จุด ใน {flagged} PDF (จากทั้งหมด {len(pdf_dirs)} PDF)")
    print(f"📄 รายงาน: {REPORT_FILE.resolve()}")

    shown = 0
    for path, issues in report.items():
        if shown >= 15:
            print(f"\n  ... (ดูทั้งหมดใน {REPORT_FILE})")
            break
        print(f"\n  📌 {path}")
        for issue in issues:
            print(f"     [{issue['check']}] {issue['detail']}")
            shown += 1


if __name__ == "__main__":
    main()
