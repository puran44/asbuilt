"""
Asbuilt — Document text extraction
==================================

Reads the attachments we've been storing as bytes and pulls out their text, so
the assistant can answer money questions — invoice amounts, contract values,
pay-app totals — that live inside PDFs and never appear in email bodies.

Deliberately local and free: pypdf for PDFs, python-docx for .docx, openpyxl
for .xlsx, plain decode for text files. NO OCR — a scanned PDF is recorded as
status='empty' so we can measure how big the OCR problem actually is before
paying for it.

    pip install pypdf python-docx openpyxl
    python extract_documents.py             # extract everything not yet tried
    python extract_documents.py --report    # coverage stats only
    python extract_documents.py --retry-errors   # re-attempt past failures

Resumable and idempotent: keyed on attachment id, so re-running only touches
attachments with no result yet.
"""

import argparse
import io
import logging
import os
import sqlite3

# pypdf narrates every malformed-PDF quirk it survives ("Multiple definitions
# in dictionary...", "Advanced encoding ... not implemented"). Real-world PDFs
# are dirty; these are shrugs, not failures. Genuine errors still surface as
# status='error' rows, so silence the play-by-play.
logging.getLogger("pypdf").setLevel(logging.ERROR)

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "asbuilt.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")

# Cap stored text per document. A 1,500-page project manual is real (we saw
# them) but its full text would bloat FTS and any model context it lands in.
MAX_CHARS = 200_000

TEXT_EXTS = {".txt", ".csv", ".md", ".log", ".xml", ".htm", ".html", ".json"}


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def extract_pdf(data):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")          # many "secured" PDFs open with no password
        except Exception:
            return "error", None, None
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception:
            pages.append("")            # one bad page shouldn't kill the document
    text = "\n".join(pages).strip()
    return ("ok" if text else "empty"), text, len(reader.pages)


def extract_docx(data):
    import docx
    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    text = "\n".join(p for p in parts if p.strip()).strip()
    return ("ok" if text else "empty"), text, None


def extract_xlsx(data):
    import openpyxl
    book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    parts = []
    for sheet in book.worksheets:
        parts.append(f"[sheet: {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append("\t".join(cells))
    book.close()
    text = "\n".join(parts).strip()
    return ("ok" if text else "empty"), text, None


def extract_plain(data):
    text = data.decode("utf-8", errors="replace").strip()
    return ("ok" if text else "empty"), text, None


def pick_extractor(filename, content_type):
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if name.endswith(".pdf") or "pdf" in ctype:
        return extract_pdf
    if name.endswith(".docx"):
        return extract_docx
    if name.endswith((".xlsx", ".xlsm")):
        return extract_xlsx
    if any(name.endswith(e) for e in TEXT_EXTS) or ctype.startswith("text/"):
        return extract_plain
    return None          # .doc/.xls (legacy binary), images, dwg, ics, ...


def run(conn, retry_errors=False):
    exclude = "('ok','empty','unsupported')" if retry_errors \
        else "('ok','empty','unsupported','error')"
    todo = conn.execute(f"""
        SELECT a.id, a.stored_path, a.content_type, a.size_bytes,
               (SELECT filename FROM message_attachment
                WHERE attachment_id = a.id LIMIT 1) AS filename
        FROM attachment a
        WHERE a.is_inline = 0
          AND a.id NOT IN (SELECT attachment_id FROM attachment_text
                           WHERE status IN {exclude})
        ORDER BY a.id DESC""").fetchall()
    # DESC: newest documents first. People ask about the invoice they sent
    # yesterday, not one from 2021 — recency is the value order, and a freshly
    # ingested attachment should be readable within the first minute of a run.

    print(f"{len(todo)} attachments to process")
    counts = {}
    for i, row in enumerate(todo, 1):
        extractor = pick_extractor(row["filename"], row["content_type"])
        status, text, n_pages = "unsupported", None, None

        if extractor:
            path = os.path.join(HERE, row["stored_path"])
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                status, text, n_pages = extractor(data)
            except Exception:
                status, text = "error", None

        if text and len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]

        conn.execute(
            """INSERT OR REPLACE INTO attachment_text
               (attachment_id, status, text, n_pages, n_chars)
               VALUES (?, ?, ?, ?, ?)""",
            (row["id"], status, text, n_pages, len(text) if text else 0))
        counts[status] = counts.get(status, 0) + 1

        if i % 200 == 0:
            conn.commit()
            print(f"  ...{i}/{len(todo)}  {counts}")
    conn.commit()
    print(f"done: {counts}")


def report(conn):
    print("=" * 62)
    print("DOCUMENT TEXT COVERAGE")
    print("=" * 62)
    total = conn.execute(
        "SELECT COUNT(*) FROM attachment WHERE is_inline=0").fetchone()[0]
    print(f"\n  {total} stored documents\n")
    for r in conn.execute("""
        SELECT status, COUNT(*) n, COALESCE(SUM(n_chars),0) chars
        FROM attachment_text GROUP BY status ORDER BY n DESC"""):
        print(f"  {r['status']:12} {r['n']:6}   ({r['chars']:,} chars)")

    pdf_empty = conn.execute("""
        SELECT COUNT(*) FROM attachment_text t JOIN attachment a ON a.id=t.attachment_id
        WHERE t.status='empty'""").fetchone()[0]
    ok = conn.execute(
        "SELECT COUNT(*) FROM attachment_text WHERE status='ok'").fetchone()[0]
    if ok or pdf_empty:
        print(f"\n  READ THIS AS: {ok} documents are now searchable text.")
        print(f"  {pdf_empty} parsed but had no text layer — those are scans,")
        print(f"  and their count is the honest size of the OCR question.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--retry-errors", action="store_true")
    args = ap.parse_args()
    conn = connect()
    if not args.report:
        run(conn, retry_errors=args.retry_errors)
    report(conn)
    conn.close()


if __name__ == "__main__":
    main()
