"""
step3_checksum.py — Checksum validation ของบัตรเลือกตั้ง

ตรวจสอง checksum ต่อหนึ่ง PDF:
  1. ballot_total : บัตรดี + บัตรเสีย + บัตรที่ไม่เลือก = บัตรที่ได้รับทั้งหมด
  2. score_total  : ผลรวมคะแนนผู้สมัคร/พรรค = รวมคะแนนทั้งสิ้น

constituency : ตรวจทีละหน้า (แต่ละหน้าเป็น 1 หน่วยเลือกตั้ง/สถานที่อิสระ)
party_list   : รวมทุกหน้าก่อน (ตารางพรรคต่อข้ามหน้า)
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


def _first_score_num(text: str) -> int | None:
    """ดึงหมายเลขแถวแรกในตาราง score (ข้าม header และแถว total)"""
    for rm in _ROW_RE.finditer(text):
        row = rm.group(1)
        if "รวมคะแนน" in row:
            continue
        cells = [c.strip() for c in _CELL_RE.findall(row)]
        if len(cells) >= 2:
            nm = _NUM_ONLY.match(cells[0])
            if nm:
                return _int(nm.group(1))
    return None


def _group_pages(page_files: list[Path]) -> list[list[Path]]:
    """จัดกลุ่มหน้า: ขึ้นกลุ่มใหม่เมื่อพบแถวแรกเป็นหมายเลข 1
    - party list ปกติ (1-34 หน้า 1, 35-57 หน้า 2) → กลุ่มเดียว
    - ล่วงหน้า/overseas (ตารางครบชุดต่อ 2 หน้า × หลายที่) → กลุ่มละ 2 หน้า
    - constituency (ตารางครบในหน้าเดียว) → กลุ่มละ 1 หน้า
    """
    groups: list[list[Path]] = []
    current: list[Path] = []
    for pf in page_files:
        n = _first_score_num(pf.read_text(encoding="utf-8"))
        if n == 1 and current:
            groups.append(current)
            current = []
        current.append(pf)
    if current:
        groups.append(current)
    return groups


_LEADING_NUM = re.compile(rf"({_D})")   # ตัวเลขแรกในเซลล์ (เช่น "19 สิบเก้า" → 19)
_ZERO_CELL   = re.compile(r"^[-—o]$")   # ค่าศูนย์ที่ OCR อาจอ่านเป็นขีด/ตัว o


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
            continue

        cells = [c.strip() for c in _CELL_RE.findall(row)]
        # cells[0] ต้องเป็นตัวเลข (หมายเลขผู้สมัคร/พรรค) ไม่ใช่ header
        if len(cells) < 2 or not _NUM_ONLY.match(cells[0]):
            continue

        # คอลัมน์คะแนนอยู่ท้ายสุด — รองรับทั้ง "342" และ "19 สิบเก้า"
        vote_cell = cells[-1]
        m = _LEADING_NUM.search(vote_cell)
        if m:
            scores.append(_int(m.group(1)))
        elif _ZERO_CELL.match(vote_cell):
            scores.append(0)
        # ถ้า parse ไม่ได้เลย (เช่น OCR เพี้ยนมาก) ข้ามแถวนั้น

    return scores, stated_total


# ─── Single-block checksum ────────────────────────────────────────────────
def _checksum_block(text: str) -> list[dict]:
    """Run both checksums on one block of (already stripped) text."""
    issues: list[dict] = []

    total = _find(_TOTAL_RECV,  text)
    good  = _find(_GOOD_PAT,    text)
    spoil = _find(_SPOILED_PAT, text)
    blank = _find(_BLANK_PAT,   text)

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

    scores, stated_total = _parse_scores(text)
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


# ─── PDF-level checksum ───────────────────────────────────────────────────
def checksum_pdf(pdf_dir: Path) -> list[dict]:
    page_files = sorted(pdf_dir.glob("page_*.md"))
    if not page_files:
        return []

    groups = _group_pages(page_files)
    issues: list[dict] = []

    for group in groups:
        combined = "\n".join(
            strip_readings(p.read_text(encoding="utf-8")) for p in group
        )
        for issue in _checksum_block(combined):
            if len(groups) > 1:
                issue["page"] = group[0].name
            issues.append(issue)

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
