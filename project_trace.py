"""
Asbuilt — Project Trace
=======================

Finds every message about one job, wherever it happens to be sitting.

This is the real case: an active project whose email is scattered across an
unsorted inbox. Give it a job identifier ("Q120", an address, a GC name) and
it searches INBOX and Sent, then reports the anatomy of that project's mail —
who is involved, what documents flow, and what the subject lines look like.

Pass SEVERAL identifiers for the same job — that is the point. NYC schools in
particular go by many names: Q690, "P.S. 690", "PS690", the school's real name,
the street address, the SCA job number. The report shows which term found which
message, which measures the alias problem instead of assuming it.

Usage:
    python project_trace.py Q690
    python project_trace.py Q690 "PS 690" "P.S. 690" "690"
    python project_trace.py Q690 "PS 690" --bodies      # also print message text

Read-only throughout.
"""

import email
import imaplib
import os
import sys
from collections import Counter, defaultdict
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("IMAP_HOST", "imap.mail.yahoo.com")
USER = os.getenv("IMAP_USER")
PASS = os.getenv("IMAP_PASS")

# Where to look. Add folder names here if a job also lives somewhere filed.
SEARCH_FOLDERS = ["INBOX", "Sent"]

BODY_PREVIEW_CHARS = 1500
BODIES_TO_PRINT = 6


def decode_str(raw):
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def get_body_text(msg):
    """Best-effort plain-text body."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", errors="replace"
                    )
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", errors="replace"
        )
    except Exception:
        return ""


def main():
    terms = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_bodies = "--bodies" in sys.argv
    if not terms:
        raise SystemExit(
            'Usage: python project_trace.py "Q690" ["PS 690" ...] [--bodies]'
        )

    if not (USER and PASS):
        raise SystemExit("Set IMAP_USER and IMAP_PASS in .env first.")

    box = imaplib.IMAP4_SSL(HOST, 993)
    box.login(USER, PASS)

    print(f'Searching {len(terms)} identifier(s) across '
          f'{", ".join(SEARCH_FOLDERS)} ...\n')

    # Keyed by Message-ID so the same email found by two different aliases
    # counts once — the same dedupe the real ingester will need.
    by_id = {}
    term_hits = {t: 0 for t in terms}

    for folder in SEARCH_FOLDERS:
        status, _ = box.select(f'"{folder}"', readonly=True)
        if status != "OK":
            print(f"  (could not open {folder}, skipping)")
            continue

        for term in terms:
            # TEXT searches headers and body — broader than SUBJECT alone, which
            # matters because job numbers often appear only in the body or a PDF.
            status, data = box.search(None, "TEXT", f'"{term}"')
            if status != "OK":
                print(f"  (search failed for {term!r} in {folder})")
                continue

            ids = data[0].split()
            term_hits[term] += len(ids)
            print(f"  {folder:6} {term!r:22} {len(ids)} matches")

            for msg_id in ids:
                _, raw = box.fetch(msg_id, "(RFC822)")
                if not raw or not isinstance(raw[0], tuple):
                    continue
                msg = email.message_from_bytes(raw[0][1])

                key = msg.get("Message-ID") or f"{folder}:{msg_id}"
                if key in by_id:
                    by_id[key]["found_by"].add(term)
                    continue

                try:
                    when = parsedate_to_datetime(msg.get("Date"))
                except Exception:
                    when = None

                attachments = []
                for part in msg.walk():
                    filename = part.get_filename()
                    if filename:
                        attachments.append(decode_str(filename))

                by_id[key] = {
                    "folder": folder,
                    "date": when,
                    "from": decode_str(msg.get("From")),
                    "to": decode_str(msg.get("To")),
                    "subject": decode_str(msg.get("Subject")),
                    "attachments": attachments,
                    "body": get_body_text(msg),
                    "found_by": {term},
                }

    box.logout()
    hits = list(by_id.values())

    if not hits:
        print(f'\nNo messages found for: {", ".join(terms)}')
        print("Try a different identifier: the street address, the GC name,")
        print("or a distinctive word from the project title.")
        return

    hits.sort(key=lambda h: (h["date"] is None, h["date"]))

    def section(title):
        print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")

    section(f"TIMELINE — {len(hits)} messages")
    dated = [h for h in hits if h["date"]]
    if dated:
        print(f"  {dated[0]['date']:%Y-%m-%d} to {dated[-1]['date']:%Y-%m-%d}"
              f"  ({(dated[-1]['date'] - dated[0]['date']).days} days)\n")

    for h in hits:
        stamp = f"{h['date']:%Y-%m-%d}" if h["date"] else "??????????"
        flag = "^" if h["folder"] == "Sent" else " "
        clip = 78
        print(f"  {stamp} {flag} {h['subject'][:clip]}")
        if h["attachments"]:
            for name in h["attachments"]:
                print(f"             + {name}")
    print("\n  (^ = outgoing, from the Sent folder)")

    if len(terms) > 1:
        section("ALIAS OVERLAP  (how bad is the naming problem?)")
        print(f"  {len(hits)} unique messages found by {len(terms)} identifiers.\n")
        print("  Raw hits per identifier (sums to more than the unique count")
        print("  wherever aliases co-occur in the same message):")
        for term, count in sorted(term_hits.items(), key=lambda x: -x[1]):
            print(f"    {count:5}  {term!r}")

        combos = Counter(frozenset(h["found_by"]) for h in hits)
        print("\n  Messages by which identifiers appeared in them:")
        for combo, count in combos.most_common():
            names = " + ".join(sorted(combo))
            print(f"    {count:5}  {names}")
        print("""
  READ THIS AS:
    Messages found by only ONE identifier are the hard cases — a parser
    keyed on a different alias would miss them entirely. Messages found by
    several aliases at once are the easy ones: they let the system LEARN
    that those strings mean the same job, which is how the alias table
    gets built from evidence rather than from guessing.""")

    section("WHO IS INVOLVED  (sizes the entity-resolution problem)")
    senders = Counter(h["from"] for h in hits)
    for who, count in senders.most_common():
        print(f"  {count:4}  {who[:70]}")

    section("DOCUMENTS THAT FLOWED")
    kinds = Counter()
    names = []
    for h in hits:
        for name in h["attachments"]:
            names.append(name)
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "none"
            kinds[ext] += 1
    if names:
        for ext, count in kinds.most_common():
            print(f"  {count:4}  .{ext}")
        print("\n  Filenames (the naming conventions your parser must handle):")
        for name in names:
            print(f"    {name}")
    else:
        print("  No attachments on any message in this project.")

    section("SUBJECT-LINE CONVENTIONS")
    print("  Does the job identifier appear consistently? Is there a numbering")
    print("  scheme (RFI-014, SUB-22-05)? This decides how hard classification is.\n")
    for subject in dict.fromkeys(h["subject"] for h in hits):
        print(f"  {subject[:90]}")

    if show_bodies:
        section(f"MESSAGE BODIES  (first {BODIES_TO_PRINT}, truncated)")
        for h in hits[:BODIES_TO_PRINT]:
            stamp = f"{h['date']:%Y-%m-%d}" if h["date"] else "??"
            print(f"\n--- {stamp} | {h['from'][:50]} | {h['subject'][:60]}")
            body = " ".join(h["body"].split())
            print(f"    {body[:BODY_PREVIEW_CHARS]}")

    section("WHAT TO LOOK FOR")
    print("""
  1. Do RFIs/submittals arrive as PDF attachments, inline text, or both?
  2. Does the job number appear in the subject every time, or only sometimes?
  3. How many distinct companies and people touch one job?
  4. Are documents named consistently, or is it IMG_4471.jpg?
  5. Do threads stay threaded, or does each message start fresh?

  These five answers ARE the parser spec.
""")


if __name__ == "__main__":
    main()
