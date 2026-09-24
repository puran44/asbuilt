"""
Asbuilt — Learn per-sender boilerplate
======================================

Finds the lines each sender repeats in nearly everything they write — signature
blocks, corporate legal disclaimers, certification footers, "Sent from my
iPhone" — so the assistant stops paying to send them and stops letting them
crowd real content out of the context budget.

Learned from the corpus, never hardcoded. An earlier version had one
contractor's company name and phone number written into a regex; that strips
exactly nothing for the next customer. This runs per mailbox and adapts.

    python learn_boilerplate.py            # learn, then report
    python learn_boilerplate.py --report   # show what was learned

Run it after ingest. Cheap (pure SQL + counting), no API calls.
"""

import argparse
import os
import re
import sqlite3
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "asbuilt.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")

# A sender needs this many messages before repetition means anything. Below it,
# two messages sharing a line is coincidence, not boilerplate.
MIN_MESSAGES = 4

# Share of a sender's messages a line must appear in to count as boilerplate.
# 0.5 is deliberately conservative: real content occasionally repeats (status
# updates, recurring requests), and wrongly stripping content is worse than
# paying for a few extra tokens.
MIN_RATIO = 0.5

# Ignore very short lines. "Thanks" repeating is harmless and cheap; the win is
# in address blocks, phone rows, and disclaimer paragraphs.
MIN_LINE_LEN = 22

# Sample cap per sender — enough to establish a pattern without scanning a
# decade of mail from a chatty counterparty.
SAMPLE = 60

QUOTE_MARKERS = re.compile(
    r"(?:^|\n)\s*(?:"
    r"On \w+, \w+ \d{1,2}, \d{4}(?: at [\d:]+\s*[AP]M)?[^\n]{0,80}wrote:"
    r"|-{2,}\s*Forwarded Message\s*-{2,}"
    r"|_{10,}"
    r"|From:\s*[^\n]{0,120}\n?\s*(?:Sent|Date):"
    r")", re.I)


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def own_text(body):
    """Just what this sender wrote — quoted history cut off.

    Counting lines from quoted chains would find other people's signatures
    inside this sender's mail and attribute them to the wrong person.
    """
    if not body:
        return ""
    match = QUOTE_MARKERS.search(body)
    return body[:match.start()] if match else body


def learn(conn):
    conn.execute("DELETE FROM sender_boilerplate")

    senders = conn.execute("""
        SELECT from_email, COUNT(*) n FROM message
        WHERE from_email IS NOT NULL AND from_email != ''
        GROUP BY from_email HAVING n >= ?""", (MIN_MESSAGES,)).fetchall()

    print(f"{len(senders)} senders with >= {MIN_MESSAGES} messages")
    learned = 0

    for i, sender in enumerate(senders, 1):
        rows = conn.execute("""
            SELECT body_text FROM message
            WHERE from_email = ? AND body_text IS NOT NULL AND body_text != ''
            ORDER BY sent_at DESC LIMIT ?""", (sender["from_email"], SAMPLE)).fetchall()
        if len(rows) < MIN_MESSAGES:
            continue

        # Count DISTINCT messages each line appears in — not raw occurrences, so
        # a line repeated three times inside one email still counts once.
        seen_in = Counter()
        for row in rows:
            lines = {
                " ".join(line.split())
                for line in own_text(row["body_text"]).splitlines()
                if len(" ".join(line.split())) >= MIN_LINE_LEN
            }
            seen_in.update(lines)

        cutoff = max(MIN_MESSAGES - 1, int(len(rows) * MIN_RATIO))
        for line, count in seen_in.items():
            if count >= cutoff:
                conn.execute(
                    """INSERT OR REPLACE INTO sender_boilerplate
                       (from_email, line, n_messages) VALUES (?, ?, ?)""",
                    (sender["from_email"], line, count))
                learned += 1

        if i % 200 == 0:
            conn.commit()
            print(f"  ...{i}/{len(senders)}")

    conn.commit()
    print(f"learned {learned} boilerplate lines")


def report(conn):
    total = conn.execute("SELECT COUNT(*) FROM sender_boilerplate").fetchone()[0]
    senders = conn.execute(
        "SELECT COUNT(DISTINCT from_email) FROM sender_boilerplate").fetchone()[0]
    chars = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(line) * n_messages), 0) FROM sender_boilerplate"
    ).fetchone()[0]

    print(f"\n{'=' * 62}")
    print(f"  {total} boilerplate lines across {senders} senders")
    print(f"  ~{chars:,} characters of repeated text now strippable")
    print("=" * 62)

    print("\nBIGGEST OFFENDERS (chars x repetitions):")
    for r in conn.execute("""
        SELECT from_email, line, n_messages,
               LENGTH(line) * n_messages AS weight
        FROM sender_boilerplate ORDER BY weight DESC LIMIT 12"""):
        line = r["line"][:64].encode("ascii", "replace").decode()
        print(f"  x{r['n_messages']:<4} {r['from_email'][:34]:34} {line}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    conn = connect()
    if not args.report:
        learn(conn)
    report(conn)
    conn.close()


if __name__ == "__main__":
    main()
