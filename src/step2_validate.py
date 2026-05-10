"""
ขั้นที่ 3: Validate OCR output
เช็คว่าตัวเลขตรงกับคำอ่านภาษาไทยในวงเล็บหรือไม่
ถ้าไม่ตรง → flag ลง validation_report.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_ROOT       = Path(__file__).resolve().parent.parent
OUTPUT_DIR  = _ROOT / "output"
REPORT_FILE = _ROOT / "reports" / "validation_report.json"

# ─── Thai digit → Arabic ──────────────────────────────────────────────────

_THAI_DIGIT_MAP = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def to_arabic(s: str) -> int | None:
    try:
        return int(s.translate(_THAI_DIGIT_MAP))
    except ValueError:
        return None


# ─── Number → Thai text ───────────────────────────────────────────────────

_ONES = ["", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]


def num_to_thai(n: int) -> str:
    """แปลงตัวเลขเป็นคำอ่านภาษาไทย canonical"""
    if n == 0:
        return "ศูนย์"

    parts: list[str] = []
    remaining = n

    for value, name in [
        (1_000_000, "ล้าน"),
        (100_000, "แสน"),
        (10_000, "หมื่น"),
        (1_000, "พัน"),
        (100, "ร้อย"),
    ]:
        if remaining >= value:
            d = remaining // value
            remaining %= value
            parts.append(_ONES[d] + name)

    if remaining >= 10:
        d = remaining // 10
        remaining %= 10
        if d == 1:
            parts.append("สิบ")
        elif d == 2:
            parts.append("ยี่สิบ")
        else:
            parts.append(_ONES[d] + "สิบ")

    if remaining > 0:
        # เอ็ด ใช้กับหลักหน่วย=1 เมื่อมีหลักอื่นนำหน้า
        parts.append("เอ็ด" if remaining == 1 and n > 1 else _ONES[remaining])

    return "".join(parts)


# ─── Extraction ───────────────────────────────────────────────────────────

# จับคู่: ตัวเลข → (ช่องว่าง/คำเสริม ≤20 ตัว) → (คำอ่านภาษาไทย)
_PAIR_RE = re.compile(
    r"(?<!\=\")(?<!\=')([0-9๐-๙]+)"  # ตัวเลข — ไม่นับถ้าอยู่ใน HTML attribute (colspan="2")
    r"[^(（\n\d]{0,20}?"              # gap เช่น " บัตร " (non-greedy, ไม่ข้ามบรรทัด)
    r"[（(]\s*"                        # เปิดวงเล็บ
    r"([฀-๿][฀-๿\s]*?)"              # คำอ่านภาษาไทย
    r"\s*[)）]"                        # ปิดวงเล็บ
)


# คำย่อรูปแบบเอกสารที่ตามหลังตัวเลข — ไม่ใช่คำอ่านตัวเลข ข้ามไปได้เลย
# รวม variant ที่ OCR อาจอ่านผิด: บช, บข, บซ, บฃ
_SKIP_TEXTS = {"บช", "บข", "บซ", "บฃ"}


def strip_readings(text: str) -> str:
    """ลบคำอ่านภาษาไทยในวงเล็บที่ตามหลังตัวเลขออก เหลือแค่ตัวเลข
    เช่น  "699 บัตร ( หกร้อยเก้าสิบเก้า )"  →  "699 บัตร"
    ใช้เป็น preprocessing ก่อนทำ checksum
    """
    def _replace(m: re.Match) -> str:
        # คืนเฉพาะส่วนตัวเลข ตัดวงเล็บ+คำอ่านออก
        # ถ้าคำใน skip list → เก็บวงเล็บ+ข้อความไว้เพราะไม่ใช่คำอ่านตัวเลข
        raw_reading = re.sub(r"\s+", "", m.group(2))
        if raw_reading in _SKIP_TEXTS:
            return m.group(0)
        full = m.group(0)
        num_end = m.start(1) - m.start() + len(m.group(1))
        return full[:num_end]

    return _PAIR_RE.sub(_replace, text)


def extract_pairs(text: str) -> list[tuple[int, str, str]]:
    """คืน list ของ (ตัวเลข, คำอ่านดิบ, คำอ่านหลัง normalize)"""
    results = []
    for m in _PAIR_RE.finditer(text):
        num_str, raw = m.group(1), m.group(2)
        n = to_arabic(num_str)
        if n is None:
            continue
        normalized = re.sub(r"\s+", "", raw)
        if normalized in _SKIP_TEXTS:
            continue
        results.append((n, raw.strip(), normalized))
    return results


# ─── Validate one file ────────────────────────────────────────────────────

# คำศัพท์ที่ประกอบกันเป็นคำอ่านตัวเลขภาษาไทยได้ (ยาวกว่าก่อน เพื่อ greedy match)
# รวม ล้อย เผื่อ OCR สลับ ร↔ล แต่จะถูก normalize ก่อนเปรียบเทียบอยู่แล้ว
_VALID_NUM_RE = re.compile(
    r"^(?:ศูนย์|หนึ่ง|ยี่สิบ|สอง|สาม|สี่|ห้า|หก|เจ็ด|แปด|เก้า"
    r"|สิบ|เอ็ด|ร้อย|ล้อย|พัน|หมื่น|แสน|ล้าน|ลบ)+$"
)

_TRAILING_WORDS = re.compile(r"(คะแนน|บัตร|คน)$")


def _strip_trailing(s: str) -> str:
    """ตัดคำต่อท้ายที่ไม่ใช่ส่วนของตัวเลข เช่น คะแนน, บัตร"""
    return _TRAILING_WORDS.sub("", s)


def _ro_la_normalize(s: str) -> str:
    """แทน ล→ร เพื่อ ignore การสลับ ร/ล จาก OCR"""
    return s.replace("ล", "ร")


def validate_file(md_path: Path) -> list[dict]:
    text = md_path.read_text(encoding="utf-8")
    pairs = extract_pairs(text)
    issues = []
    for n, raw, normalized in pairs:
        expected = num_to_thai(n)
        cleaned = _strip_trailing(normalized)
        # ข้ามถ้าข้อความไม่ได้ประกอบด้วยคำอ่านตัวเลขล้วนๆ (เช่น OCR อ่านลายมือผิดเป็นคำอื่น)
        if not _VALID_NUM_RE.match(cleaned):
            continue
        if cleaned != expected and _ro_la_normalize(cleaned) != _ro_la_normalize(expected):
            issues.append({
                "number": n,
                "found": raw,
                "expected": expected,
            })
    return issues


# ─── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    md_files = sorted(OUTPUT_DIR.rglob("*.md"))
    print(f"🔍 ตรวจสอบ {len(md_files)} ไฟล์...")

    report: dict[str, list[dict]] = {}
    total_issues = 0

    for md_path in md_files:
        rel = str(md_path.relative_to(OUTPUT_DIR))
        issues = validate_file(md_path)
        if issues:
            report[rel] = issues
            total_issues += len(issues)

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    flagged_files = len(report)
    print(f"\n{'=' * 60}")
    print(f"🔎 พบ {total_issues} จุดที่ไม่ตรง ใน {flagged_files} ไฟล์ (จากทั้งหมด {len(md_files)} ไฟล์)")
    print(f"📄 รายงานเต็ม: {REPORT_FILE.resolve()}")

    # แสดงตัวอย่าง 15 อันดับแรก
    shown = 0
    for path, issues in report.items():
        if shown >= 15:
            print(f"\n  ... (ดูทั้งหมดใน {REPORT_FILE})")
            break
        print(f"\n  📌 {path}")
        for issue in issues:
            print(f"     {issue['number']:>6}  พบ: \"{issue['found']}\"  |  คาดหวัง: \"{issue['expected']}\"")
            shown += 1
            if shown >= 15:
                break


if __name__ == "__main__":
    main()
