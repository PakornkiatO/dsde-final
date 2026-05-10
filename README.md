# Thai Election Document OCR Pipeline

An end-to-end pipeline to extract structured vote-count data from Thai election PDF documents using the Typhoon OCR API.

The pipeline processes official election result forms (ส.ส. 5-17 and ส.ส. 5-18) for Electoral District 2 (เขตเลือกตั้งที่ 2), covering constituency (แบ่งเขต) and party-list (บัญชีรายชื่อ) ballots across all polling units.

---

## Project Structure

```
project/
├── data/                        # Input PDFs (see "Input data structure" below)
├── output/                      # OCR markdown output (one .md per PDF page)
├── reports/                     # Validation and extraction reports
│   ├── ocr_done.json            # Successfully OCR'd PDFs
│   ├── ocr_failures.json        # Pages that failed OCR (for retry)
│   ├── validation_report.json   # Number–Thai-word mismatches
│   ├── checksum_report.json     # Ballot total / vote total checksum failures
│   ├── combined_report.json     # Merged validation + checksum report
│   └── extracted_data.json      # Final structured data
├── src/
│   ├── step1_ocr_test.py        # Test OCR on a single PDF
│   ├── step2_ocr_batch.py       # Parallel batch OCR for all PDFs
│   ├── step2_ocr_fallback.py    # Fallback OCR for persistent failures
│   ├── step3_validate.py        # Validate number–Thai-word pairs
│   ├── step3_checksum.py        # Validate ballot & vote totals
│   ├── step3_combined_report.py # Merge validation reports
│   └── step4_extract.py         # Extract structured JSON from OCR output
├── requirement.txt
└── .env                         # API key (not committed)
```

---

## Input data structure

```
data/
└── เขตเลือกตั้งที่_2/                        ← Electoral District 2 (Ang Thong)
    │
    ├── ล่วงหน้าในเขต/                         ← Early in-district voting
    │   ├── ส.ส._5-16.pdf                      (constituency)
    │   └── ส.ส._5-16_(บช).pdf                 (party list)
    │
    ├── ล่วงหน้านอกเขตและนอกราชอาณาจักร/      ← Early out-of-district & overseas voting
    │   ├── ส.ส._5-17.pdf
    │   └── ส.ส._5-17_(บช).pdf
    │
    ├── อำเภอไชโย/
    │   ├── ตำบลชัยฤทธิ์/
    │   ├── ตำบลราชสถิตย์/
    │   ├── ตำบลเทวราช/
    │   ├── เทศบาลตำบลเกษไชโย/
    │   └── เทศบาลตำบลไชโย/
    │       └── หน่วยเลือกตั้งที่_N/           ← one folder per polling unit
    │           ├── ส.ส._5-18.pdf              (constituency, election day)
    │           └── ส.ส._5-18_(บช).pdf         (party list, election day)
    │
    ├── อำเภอโพธิ์ทอง/
    │   ├── ตำบลคำหยาด/
    │   ├── ตำบลบางพลับ/
    │   ├── ตำบลบางระกำ/
    │   ├── ตำบลบางเจ้าฉ่า/
    │   ├── ตำบลบ่อแร่/
    │   ├── ตำบลยางช้าย/
    │   ├── ตำบลสามง่าม/
    │   ├── ตำบลหนองแม่ไก่/
    │   ├── ตำบลองครักษ์/
    │   ├── ตำบลอินทประมูล/
    │   ├── ตำบลอ่างแก้ว/
    │   ├── ตำบลโพธิ์รังนก/
    │   ├── เทศบาลตำบลทางพระ/
    │   ├── เทศบาลตำบลม่วงคัน/
    │   ├── เทศบาลตำบลรำมะสัก/
    │   ├── เทศบาลตำบลโคกพุทรา/
    │   └── เทศบาลตำบลโพธิ์ทอง/
    │
    ├── อำเภอแสวงหา/
    │   ├── ตำบลจำลอง/
    │   ├── ตำบลบ้านพราน/
    │   ├── ตำบลวังน้ำเย็น/
    │   ├── ตำบลศรีพราน/
    │   ├── ตำบลสีบัวทอง/
    │   ├── ตำบลห้วยไผ่/
    │   ├── เทศบาลตำบลเพชรเมืองทอง/
    │   └── เทศบาลตำบลแสวงหา/
    │
    ├── อำเภอสามโก้/
    │   ├── ตำบลอบทม/
    │   ├── ตำบลโพธิ์ม่วงพันธ์/
    │   └── เทศบาลตำบลสามโก้/
    │
    └── อำเภอวิเศษชัยชาญ_(เฉพาะ_ต.ม่วงเตี้ย,_ต.สาวร้องไห้)/
        ├── ตำบลม่วงเตี้ย/
        └── ตำบลสาวร้องไห้/
```

**สรุปขนาดข้อมูล**

| รายการ | จำนวน |
|--------|-------|
| อำเภอ | 5 |
| ตำบล / เทศบาลตำบล | 34 |
| หน่วยเลือกตั้ง (วันเลือกตั้ง) | 252 |
| ไฟล์ PDF ทั้งหมด | 507 |

**หมายเหตุ** — ทุก `หน่วยเลือกตั้งที่_N/` มี PDF 2 ไฟล์ (constituency + party list) ยกเว้นหน่วยที่มีข้อมูลไม่ครบ การลงคะแนนล่วงหน้าใช้แบบฟอร์ม 5-16 (ในเขต) และ 5-17 (นอกเขต/ต่างประเทศ) แทน 5-18

---

## Pipeline Overview

```
data/ (PDFs)
    │
    ▼
Step 1  step1_ocr_test.py       — smoke-test OCR on one PDF
    │
    ▼
Step 2  step2_ocr_batch.py      — batch OCR all PDFs (parallel, resumable)
         step2_ocr_fallback.py  — retry persistent failures with alt model
    │
    ▼  output/ (markdown files)
    │
    ▼
Step 3  step3_validate.py       — check number ↔ Thai-word consistency
         step3_checksum.py      — verify ballot totals & vote totals
         step3_combined_report.py — merge both reports
    │
    ▼  reports/combined_report.json
    │
    ▼
Step 4  step4_extract.py        — extract structured data → JSON
    │
    ▼  reports/extracted_data.json
```

---

## Setup

### Prerequisites

- Python 3.10+
- [Poppler](https://poppler.freedesktop.org/) (required by `pypdf` for PDF rendering):
  - macOS: `brew install poppler`
  - Ubuntu/Debian: `sudo apt-get install -y poppler-utils`

### Install dependencies

```bash
pip install -r requirement.txt
```

### API key

Create a `.env` file in the project root:

```
TYPHOON_OCR_API_KEY=sk-xxxxxxxx
```

Get a free API key at [opentyphoon.ai](https://opentyphoon.ai). Add `.env` to `.gitignore` to keep the key out of git history.

---

## Usage

Run scripts from the project root with the `dsde` conda environment (or whichever environment has the dependencies installed).

### Step 1 — Test OCR on a single PDF

```bash
conda run -n dsde python src/step1_ocr_test.py path/to/election.pdf
```

OCR output (one `.md` per page) is saved to `output/`.

### Step 2 — Batch OCR all PDFs

```bash
# Run all PDFs with 4 parallel workers (default)
conda run -n dsde python src/step2_ocr_batch.py

# Adjust concurrency
conda run -n dsde python src/step2_ocr_batch.py --workers 6

# Test on first 5 files only
conda run -n dsde python src/step2_ocr_batch.py --limit 5

# Retry only failed pages from ocr_failures.json
conda run -n dsde python src/step2_ocr_batch.py --retry-failed
```

The script is **resumable** — already-completed pages are skipped. Progress is tracked in `reports/ocr_done.json` and `reports/ocr_failures.json`.

#### Fallback for persistent timeouts

If some pages remain stuck after retries, try a different model:

```bash
conda run -n dsde python src/step2_ocr_fallback.py --model typhoon-ocr-preview
conda run -n dsde python src/step2_ocr_fallback.py --model typhoon-ocr-preview --workers 2
```

### Step 3 — Validate OCR quality

**Text-match validation** — checks that each number in the document matches its Thai-word spelling in parentheses (e.g. `699 (หกร้อยเก้าสิบเก้า)`):

```bash
conda run -n dsde python src/step3_validate.py
# → reports/validation_report.json
```

**Checksum validation** — verifies two arithmetic constraints per polling unit:
1. `ballots_good + ballots_spoiled + ballots_blank = ballots_received`
2. `sum(candidate/party votes) = votes_total`

```bash
conda run -n dsde python src/step3_checksum.py
# → reports/checksum_report.json
```

**Combined report** — merges both checks into one file:

```bash
conda run -n dsde python src/step3_combined_report.py
# → reports/combined_report.json
```

### Step 4 — Extract structured data

```bash
conda run -n dsde python src/step4_extract.py
# → reports/extracted_data.json
```

#### Output schema

Each entry in `extracted_data.json` corresponds to one PDF:

```json
{
  "pdf_path": "เขตเลือกตั้งที่_2/อำเภอไชโย/.../ส.ส._5-18",
  "form_code": "5-18",
  "form_type": "constituency",
  "district": 2,
  "pages": [
    {
      "page": "page_001",
      "ballots_received": 699,
      "ballots_good": 680,
      "ballots_spoiled": 12,
      "ballots_blank": 7,
      "votes_total": 680,
      "candidates": [
        {"number": 1, "name": "...", "party": "...", "votes": 342}
      ]
    }
  ]
}
```

For party-list forms (`form_type: "party_list"`), `candidates` is replaced by `parties` and each entry has `{number, name, votes}`.

---

## Document types

| Form code | Suffix | Type | Description |
|-----------|--------|------|-------------|
| 5-17 | — | `constituency` | Constituency (แบ่งเขต) result, advance voting |
| 5-17 | บช | `party_list` | Party-list (บัญชีรายชื่อ) result, advance voting |
| 5-18 | — | `constituency` | Constituency result, election day |
| 5-18 | บช | `party_list` | Party-list result, election day |

---

## Reports reference

| File | Content |
|------|---------|
| `ocr_done.json` | PDFs fully OCR'd (with timestamp and page count) |
| `ocr_failures.json` | Pages that failed — keyed by relative PDF path, value is list of page numbers |
| `validation_report.json` | Number–Thai-word mismatches per page |
| `checksum_report.json` | Ballot-total and vote-total checksum failures per PDF |
| `combined_report.json` | Both validation types merged (`text_match` + `checksum` per PDF) |
| `extracted_data.json` | Final structured vote data for all PDFs |
