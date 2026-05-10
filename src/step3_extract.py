"""
step4_extract.py — Extract structured data from OCR markdown files
output: reports/extracted_data.json

Schema per PDF:
  constituency (5-17 / 5-18):
    pdf_path, form_code, form_type, district,
    pages: [{page,
             ballots_received, ballots_good, ballots_spoiled, ballots_blank,
             votes_total, candidates: [{number, name, party, votes}]}]

  party_list (5-17บช / 5-18บช):
    pdf_path, form_code, form_type, district,
    pages: [{page, votes_total, parties: [{number, name, votes}]}]
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT       = Path(__file__).resolve().parent.parent
OUTPUT_DIR  = _ROOT / "output"
REPORTS_DIR = _ROOT / "reports"

_THAI_MAP = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def to_int(s: str) -> int | None:
    try:
        return int(s.strip().translate(_THAI_MAP))
    except (ValueError, AttributeError):
        return None


def first_num(s: str) -> int | None:
    """ดึงตัวเลขแรกที่พบในข้อความ (ข้ามคำอ่าน)"""
    m = re.search(r"[0-9๐-๙]+", s)
    return to_int(m.group()) if m else None


# ─── Ballot summary patterns ──────────────────────────────────────────────
_D = r"[0-9๐-๙]+"
_PAT_RECEIVED = re.compile(rf"ได้รับบัตรเลือกตั้ง[\s.]*ทั้งหมด[\s.]*({_D})")
_PAT_GOOD     = re.compile(rf"บัตรดี[\s.]*({_D})")
_PAT_SPOILED  = re.compile(rf"บัตรเสีย[\s.]*({_D})")
_PAT_BLANK      = re.compile(rf"ไม่เลือกผู้สมัครผู้ใด[\s.]*({_D})")
_PAT_BLANK_PTY  = re.compile(rf"ไม่เลือกบัญชีรายชื่อของพรรคการเมืองใด[\s.]*({_D})")
_PAT_TOTAL    = re.compile(rf"รวมคะแนน[^0-9๐-๙\n]*({_D})")


def _find(pat: re.Pattern, text: str) -> int | None:
    m = pat.search(text)
    return to_int(m.group(1)) if m else None


# ─── Table parsing ────────────────────────────────────────────────────────
_ROW_RE  = re.compile(r"<tr>(.*?)</tr>")
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>")


def _clean(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def parse_score_table(text: str, is_party_list: bool) -> tuple[list[dict], int | None]:
    entries: list[dict] = []
    total: int | None = None

    for rm in _ROW_RE.finditer(text):
        row = rm.group(1)

        if "รวมคะแนน" in row:
            m = _PAT_TOTAL.search(row)
            if m:
                total = to_int(m.group(1))
            continue

        cells = [_clean(c) for c in _CELL_RE.findall(row)]
        if len(cells) < 2:
            continue

        row_num = to_int(cells[0])
        if row_num is None:
            continue  # header row

        if is_party_list and len(cells) >= 3:
            entries.append({
                "number": row_num,
                "name":   cells[1],
                "votes":  first_num(cells[2]),
            })
        elif not is_party_list and len(cells) >= 4:
            entries.append({
                "number": row_num,
                "name":   cells[1],
                "party":  cells[2],
                "votes":  first_num(cells[3]),
            })

    return entries, total


# ─── Path metadata ────────────────────────────────────────────────────────
def parse_path_meta(pdf_dir: Path) -> dict:
    form_dir    = pdf_dir.name
    is_party    = bool(re.search(r"บช|บข|บซ|บฃ", form_dir))
    fc_match    = re.search(r"5[-_]1[78]", form_dir)
    form_code   = fc_match.group().replace("_", "-") if fc_match else None

    parts = pdf_dir.parts
    district_part = next((p for p in parts if "เขตเลือกตั้งที่" in p), "")
    dm = re.search(r"(\d+)", district_part)
    district = int(dm.group(1)) if dm else None

    return {
        "pdf_path":  str(pdf_dir.relative_to(OUTPUT_DIR)),
        "form_code": form_code,
        "form_type": "party_list" if is_party else "constituency",
        "district":  district,
    }


# ─── Extract one PDF ──────────────────────────────────────────────────────
def extract_pdf(pdf_dir: Path) -> dict:
    page_files = sorted(pdf_dir.glob("page_*.md"))
    meta       = parse_path_meta(pdf_dir)
    is_pty     = meta["form_type"] == "party_list"
    result     = dict(meta)

    pages_out: list[dict] = []
    for pf in page_files:
        text           = pf.read_text(encoding="utf-8")
        entries, total = parse_score_table(text, is_pty)

        blank_pat = _PAT_BLANK_PTY if is_pty else _PAT_BLANK
        ballots = {
            "ballots_received": _find(_PAT_RECEIVED, text),
            "ballots_good":     _find(_PAT_GOOD,     text),
            "ballots_spoiled":  _find(_PAT_SPOILED,  text),
            "ballots_blank":    _find(blank_pat,      text),
        }
        has_ballots = any(v is not None for v in ballots.values())
        if not entries and not has_ballots:
            continue

        page_entry: dict = {"page": pf.stem, "votes_total": total}
        page_entry.update(ballots)
        if is_pty:
            page_entry["parties"]    = entries
        else:
            page_entry["candidates"] = entries

        pages_out.append(page_entry)

    result["pages"] = pages_out
    return result


# ─── Helpers ──────────────────────────────────────────────────────────────
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
    out_file = REPORTS_DIR / "extracted_data.json"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    pdf_dirs = find_pdf_dirs(OUTPUT_DIR)
    print(f"🔍 Extract จาก {len(pdf_dirs)} PDF...")

    results: list[dict] = []
    errors = 0
    for d in pdf_dirs:
        try:
            results.append(extract_pdf(d))
        except Exception as exc:
            print(f"❌ {d.relative_to(OUTPUT_DIR)}: {exc}")
            errors += 1

    out_file.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n✅ Extract สำเร็จ {len(results)} PDF  (error {errors})")
    print(f"📄 {out_file}\n")

    # แสดงตัวอย่าง 1 constituency + 1 party_list
    for ftype in ("constituency", "party_list"):
        sample = next((r for r in results if r["form_type"] == ftype), None)
        if sample:
            print(f"{'─'*60}")
            print(f"ตัวอย่าง [{ftype}]")
            print(json.dumps(sample, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
