"""
Asbuilt — the assistant
=======================

Answers plain-language questions about a contractor's jobs, using only what is
actually in their mail.

Three stages, deliberately separated:

  1. RESOLVE  — which project is the question about? Deterministic, via the
                alias table. "Q690", "PS 690", "690" all resolve the same way.
  2. RETRIEVE — pull candidate messages. Full-text search, scoped to the
                resolved project when there is one.
  3. ANSWER   — Claude reads ONLY the retrieved messages and answers.

The model never sees the database and never searches on its own. It reads a
fixed set of messages and answers from them, or says it doesn't know. That is
what makes the answers checkable: every claim traces to a message the customer
can open.

Setup:
    pip install anthropic
    add ANTHROPIC_API_KEY=sk-ant-... to .env
"""

import os
import re
import sqlite3

import anthropic
from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "asbuilt.db")

MODEL = "claude-sonnet-5"

# How many messages to put in front of the model. Each costs input tokens, so
# this is the main cost dial: ~20 messages is roughly 10-15k tokens per question.
MAX_MESSAGES = 20
BODY_CHARS = 1200          # per message; quoted reply chains get long

# Document text budget. The money answers live in PDFs, so retrieved messages
# carry their attachments' extracted text — but a full submittal package would
# drown the context, so per-document and total caps apply. Documents whose text
# matches the question keep more; the rest get a head-of-file preview.
DOC_CHARS_MATCHED = 4000   # per document that matched the question in FTS
DOC_CHARS_PREVIEW = 800    # per document merely attached to a retrieved message
DOC_TOTAL_CHARS = 30000    # across the whole request

# Answers must be traceable, so the model returns citations as data rather than
# prose we would have to parse. Every id it returns is checked against what we
# actually retrieved before the answer is shown.
ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {
            "type": "string",
            "description": "The answer, in plain language. If the provided "
                           "messages do not contain the answer, say so directly.",
        },
        "message_ids": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "IDs of the messages this answer is based on. "
                           "Empty if the answer is not in the provided messages.",
        },
        "found": {
            "type": "boolean",
            "description": "True only if the messages actually contain the answer.",
        },
    },
    "required": ["answer", "message_ids", "found"],
    "additionalProperties": False,
}

SYSTEM = """You answer questions about a construction subcontractor's projects \
using only the email messages provided to you in each request.

The person asking is a plumbing contractor or their office manager. They are not \
technical. Answer in plain language, the way a competent office manager would — \
short, direct, specific.

Rules:

1. Use ONLY the provided messages. You have no other knowledge of this business. \
If the messages do not answer the question, set found=false and say plainly what \
you could not find. Never guess, never infer beyond what a message states, and \
never fill a gap with what is typically true in construction.

2. Cite every message you used in message_ids. If you state a date, an amount, a \
status, or who said something, the message it came from must be in that list.

3. Prefer the most recent message when messages conflict, and say that the \
situation changed rather than presenting only the latest state.

4. Quote short phrases from the messages when the exact wording matters — \
approvals, dollar figures, directives, deadlines.

5. Do not speculate about what "probably" happened, what the customer should do, \
or what a missing document likely says. Absence of evidence is a finding: report \
it as one.

6. Some messages include <document> blocks — the extracted text of their \
attachments (invoices, contracts, pay applications). Treat that text as part of \
the message and cite the carrying message's id. Document text may be truncated; \
if the answer seems cut off mid-document, say the document holds more detail and \
name the file. A document marked "[scanned document — text not readable]" exists \
but could not be read — name it as a place the answer may live, and never invent \
its contents."""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_project(conn, question):
    """Which project is this question about?

    Deterministic, using the alias table built by resolve_projects.py — the same
    machinery that decided 'Q690' and 'PS 690' are one job. Longest alias first
    so 'Q690' beats a bare '690' that happens to also be an alias.
    """
    rows = conn.execute("""
        SELECT a.alias, a.project_id, p.name
        FROM project_alias a JOIN project p ON p.id = a.project_id
        ORDER BY LENGTH(a.alias) DESC""").fetchall()

    haystack = question.lower()
    for row in rows:
        alias = row["alias"].lower().strip()
        if len(alias) < 3:
            continue
        # Word-boundary match so "690" doesn't fire inside "$4,690".
        if re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", haystack):
            return row["project_id"], row["name"], row["alias"]
    return None, None, None


def fts_query(question):
    """Turn a natural question into an FTS5 query.

    FTS5 has its own syntax and raw punctuation is a syntax error, so terms are
    quoted individually and OR-ed. Crude, but predictable — and the model only
    ever sees what this returns, so a bad query degrades to 'I don't know'
    rather than to a wrong answer.
    """
    stop = {
        "what", "when", "where", "who", "why", "how", "is", "was", "are", "were",
        "the", "a", "an", "of", "for", "on", "in", "to", "did", "do", "does",
        "we", "our", "us", "i", "my", "me", "it", "that", "this", "and", "or",
        "any", "all", "get", "got", "have", "has", "had", "with", "about",
        "there", "their", "them", "been", "be", "from", "at", "by",
    }
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", question)
    terms = [w for w in words if w.lower() not in stop and len(w) > 2]
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms[:12])


# When a question resolves to a project, this many context slots are RESERVED
# for that project's most recent messages, no matter how well older mail
# matches the words of the question. Learned the hard way: a "is Q690
# finished?" answer was built entirely from a 2025 closeout thread that
# matched "finished" — while a July 2026 "Permit expired" email sat unread.
# Relevance must never be allowed to crowd out recency on status questions.
RECENT_RESERVED = 8


def retrieve(conn, question, project_id=None, limit=MAX_MESSAGES):
    """Pull the messages the model will read.

    Order of passes: (1) the project's most recent mail — reserved slots,
    unconditional; (2) full-text relevance over bodies/subjects; (3) documents
    whose content matches; (4) more recent project mail to fill what's left.
    """
    query = fts_query(question)
    found, seen = [], set()

    if project_id:
        for row in conn.execute("""
            SELECT m.* FROM message m
            JOIN message_project mp ON mp.message_id = m.id
            WHERE mp.project_id = ?
            ORDER BY m.sent_at DESC LIMIT ?""", (project_id, RECENT_RESERVED)):
            seen.add(row["id"])
            found.append(row)

    if query:
        if project_id:
            sql = """
                SELECT m.* FROM message_fts f
                JOIN message m ON m.id = f.rowid
                JOIN message_project mp ON mp.message_id = m.id
                WHERE message_fts MATCH ? AND mp.project_id = ?
                ORDER BY rank LIMIT ?"""
            args = (query, project_id, limit)
        else:
            sql = """
                SELECT m.* FROM message_fts f
                JOIN message m ON m.id = f.rowid
                WHERE message_fts MATCH ?
                ORDER BY rank LIMIT ?"""
            args = (query, limit)
        try:
            for row in conn.execute(sql, args):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    found.append(row)
        except sqlite3.OperationalError:
            pass          # malformed FTS query — fall through to recency

    # Third pass: documents whose CONTENT matches the question, even when the
    # carrying email says nothing useful ("please see attached"). This is what
    # lets "how much was the invoice" find the amount inside the PDF.
    if query:
        doc_sql = """
            SELECT DISTINCT m.* FROM attachment_fts f
            JOIN message_attachment ma ON ma.attachment_id = f.rowid
            JOIN message m ON m.id = ma.message_id
            {join}
            WHERE attachment_fts MATCH ? {scope}
            ORDER BY rank LIMIT ?"""
        if project_id:
            sql = doc_sql.format(
                join="JOIN message_project mp ON mp.message_id = m.id",
                scope="AND mp.project_id = ?")
            args = (query, project_id, limit)
        else:
            sql = doc_sql.format(join="", scope="")
            args = (query, limit)
        try:
            for row in conn.execute(sql, args):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    found.append(row)
        except sqlite3.OperationalError:
            pass

    if project_id and len(found) < limit:
        for row in conn.execute("""
            SELECT m.* FROM message m
            JOIN message_project mp ON mp.message_id = m.id
            WHERE mp.project_id = ?
            ORDER BY m.sent_at DESC LIMIT ?""", (project_id, limit)):
            if row["id"] not in seen:
                seen.add(row["id"])
                found.append(row)
            if len(found) >= limit:
                break

    # Truncate FIRST, in priority order (reserved-recent, then relevance) —
    # then sort chronologically for presentation. Sorting before truncating
    # would silently drop the newest messages whenever passes overfill.
    found = found[:limit]
    found.sort(key=lambda r: r["sent_at"] or "")
    return found


def matching_attachment_ids(conn, question, rows):
    """Which attachments on the retrieved messages match the question directly?
    Those earn the larger text budget."""
    query = fts_query(question)
    if not query:
        return set()
    ids = {r["id"] for r in rows}
    if not ids:
        return set()
    placeholders = ",".join("?" * len(ids))
    try:
        return {
            r["rowid"] for r in conn.execute(f"""
                SELECT DISTINCT f.rowid FROM attachment_fts f
                JOIN message_attachment ma ON ma.attachment_id = f.rowid
                WHERE attachment_fts MATCH ? AND ma.message_id IN ({placeholders})
            """, (query, *ids))}
    except sqlite3.OperationalError:
        return set()


# Everything below is noise we were paying to send — and worse, it was crowding
# real content out of the per-message character budget. Measured on 20 Q690
# messages: 49% of raw body text was quoted reply chains, plus ~9.5k chars of
# Proofpoint link-rewriting and ~2.8k of the same signature block repeated.
PROOFPOINT = re.compile(r"https?://urldefense\.proofpoint\.com/\S+")
LONG_URL = re.compile(r"https?://\S{120,}")

# Where a reply stops being new content and starts being history. We retrieve
# the earlier messages separately, so quoting them again is pure duplication.
QUOTE_MARKERS = re.compile(
    r"(?:^|\n)\s*(?:"
    r"On \w+, \w+ \d{1,2}, \d{4}(?: at [\d:]+\s*[AP]M)?[^\n]{0,80}wrote:"
    r"|-{2,}\s*Forwarded Message\s*-{2,}"
    r"|_{10,}"
    r"|From:\s*[^\n]{0,120}\n?\s*(?:Sent|Date):"
    r")", re.I)

# Signature blocks and legal disclaimers are LEARNED per sender by
# learn_boilerplate.py, not hardcoded. An earlier version had this contractor's
# company name and phone number written into a regex — which strips exactly
# nothing for the next customer. Cached here so a question doesn't re-query
# per message.
_boilerplate_cache = {}


def sender_boilerplate(conn, from_email):
    if from_email not in _boilerplate_cache:
        _boilerplate_cache[from_email] = {
            r["line"] for r in conn.execute(
                "SELECT line FROM sender_boilerplate WHERE from_email = ?",
                (from_email,))
        }
    return _boilerplate_cache[from_email]


def clean_body(text, conn=None, from_email=None):
    """Strip boilerplate so the character budget holds actual content.

    Order matters: kill tracking URLs first (they can span a quote marker),
    then cut quoted history, then drop this sender's learned boilerplate lines.
    If cutting history leaves almost nothing, the message WAS the forward —
    keep the forwarded content instead.
    """
    if not text:
        return ""
    text = PROOFPOINT.sub("", text)
    text = LONG_URL.sub("[url]", text)

    match = QUOTE_MARKERS.search(text)
    if match:
        head = text[:match.start()]
        # A bare "Fw:" with no new text — the forward is the message.
        head = head if len(head.strip()) >= 80 else text[match.end():]
        text = head

    if conn is not None and from_email:
        drop = sender_boilerplate(conn, from_email)
        if drop:
            text = "\n".join(
                line for line in text.splitlines()
                if " ".join(line.split()) not in drop)

    return " ".join(text.split())


def format_messages(conn, rows, matched_atts=frozenset()):
    """Render retrieved messages for the model, including their documents' text.

    Attachment filenames alone already carry signal ("...DWQTP.signed.pdf" says
    the plan was signed), but the money answers live in the document CONTENT —
    so extracted text rides along, under a budget: documents that matched the
    question get more room than incidental attachments, and scans are labeled
    as unreadable rather than silently blank.
    """
    parts = []
    doc_budget = DOC_TOTAL_CHARS

    for row in rows:
        atts = conn.execute("""
            SELECT a.id, ma.filename, t.status, t.text
            FROM message_attachment ma
            JOIN attachment a ON a.id = ma.attachment_id
            LEFT JOIN attachment_text t ON t.attachment_id = a.id
            WHERE ma.message_id = ? AND a.is_inline = 0""", (row["id"],)).fetchall()

        body = clean_body(row["body_text"], conn, row["from_email"])[:BODY_CHARS]
        block = [
            f"<message id=\"{row['id']}\">",
            f"date: {(row['sent_at'] or '')[:10]}",
            f"from: {row['from_name'] or ''} <{row['from_email'] or ''}>",
            f"subject: {row['subject'] or '(none)'}",
        ]
        if atts:
            block.append("attachments: " + ", ".join(a["filename"] for a in atts))
        block.append(f"body: {body}")

        for att in atts:
            if att["status"] == "ok" and att["text"] and doc_budget > 0:
                cap = DOC_CHARS_MATCHED if att["id"] in matched_atts \
                    else DOC_CHARS_PREVIEW
                snippet = " ".join(att["text"].split())[:min(cap, doc_budget)]
                doc_budget -= len(snippet)
                block.append(f'<document filename="{att["filename"]}">'
                             f"{snippet}</document>")
            elif att["status"] == "empty":
                block.append(f'<document filename="{att["filename"]}">'
                             f"[scanned document — text not readable]</document>")

        block.append("</message>")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def ask(question, project_id=None):
    """Answer a question. Returns a dict the UI can render directly."""
    conn = connect()

    matched_alias = None
    if project_id is None:
        project_id, project_name, matched_alias = resolve_project(conn, question)
    else:
        row = conn.execute("SELECT name FROM project WHERE id=?",
                           (project_id,)).fetchone()
        project_name = row["name"] if row else None

    rows = retrieve(conn, question, project_id)
    if not rows:
        conn.close()
        return {
            "answer": "I couldn't find any messages about that.",
            "found": False, "sources": [], "project": project_name,
            "matched_alias": matched_alias, "searched": 0,
        }

    matched_atts = matching_attachment_ids(conn, question, rows)
    context = format_messages(conn, rows, matched_atts)
    scope = f"\n\nThese messages are all from the project: {project_name}." \
        if project_name else ""

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        # medium effort: this is retrieval-grounded reading, not hard reasoning.
        # Raise to "high" if answers start missing things buried in long threads.
        output_config={"effort": "medium", "format": {"type": "json_schema",
                                                      "schema": ANSWER_SCHEMA}},
        messages=[{
            "role": "user",
            "content": f"Question: {question}{scope}\n\n"
                       f"Messages available to you:\n\n{context}",
        }],
    )

    import json
    text = next(b.text for b in response.content if b.type == "text")
    parsed = json.loads(text)

    # Citation validation. A cited id the model invented, or one outside what we
    # retrieved, is dropped rather than shown — the customer must be able to
    # open every source. If validation empties the citation list on an answer
    # claiming to have found something, that is a bug worth seeing, not hiding.
    retrieved_ids = {r["id"] for r in rows}
    valid_ids = [i for i in parsed.get("message_ids", []) if i in retrieved_ids]

    sources = []
    for mid in valid_ids:
        row = conn.execute("""
            SELECT id, subject, sent_at, from_name, from_email
            FROM message WHERE id=?""", (mid,)).fetchone()
        if row:
            sources.append(dict(row))

    conn.close()
    return {
        "answer": parsed["answer"],
        "found": parsed["found"] and bool(sources),
        "sources": sources,
        "project": project_name,
        "matched_alias": matched_alias,
        "searched": len(rows),
        "usage": {"input": response.usage.input_tokens,
                  "output": response.usage.output_tokens},
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        raise SystemExit('Usage: python assistant.py "your question"')
    result = ask(" ".join(sys.argv[1:]))
    print(f"\n{result['answer']}\n")
    if result["sources"]:
        print("Sources:")
        for s in result["sources"]:
            print(f"  [{s['id']}] {(s['sent_at'] or '')[:10]}  {s['subject']}")
    if result.get("usage"):
        print(f"\n({result['searched']} messages read, "
              f"{result['usage']['input']} in / {result['usage']['output']} out)")
