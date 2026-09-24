"""
Asbuilt — Folder Census
=======================

Answers two questions about the hand-built folder taxonomy:

  1. HOW MUCH is in each folder?  (how many potentially-labeled examples)
  2. WHEN was it last used?       (how stale, and when did filing stop)

Together these tell you whether the folders are usable training data or a
historical artifact. Read-only: opens every mailbox with readonly=True and
fetches headers only (fast — no message bodies, no attachments).

Usage:
    python folder_census.py                 # census all folders
    python folder_census.py > census.txt    # save it (census.txt is gitignored)
"""

import email
import imaplib
import os
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("IMAP_HOST", "imap.mail.yahoo.com")
USER = os.getenv("IMAP_USER")
PASS = os.getenv("IMAP_PASS")

# Folders that aren't part of the hand-built taxonomy.
SYSTEM_FOLDERS = {"INBOX", "Inbox", "Sent", "Draft", "Drafts", "Trash", "Bulk", "Archive"}

# Parse: (\HasNoChildren) "/" "Folder Name"
LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.*)')


def parse_folder_name(raw_line):
    """Extract a usable folder name from an IMAP LIST response line."""
    match = LIST_RE.match(raw_line)
    if not match:
        return None
    name = match.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name


def message_date(box, msg_id):
    """Fetch just the Date header for one message."""
    try:
        _, data = box.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (DATE)])")
        for part in data:
            if isinstance(part, tuple):
                header = email.message_from_bytes(part[1])
                raw = header.get("Date")
                if raw:
                    return parsedate_to_datetime(raw)
    except Exception:
        pass
    return None


def main():
    if not (USER and PASS):
        raise SystemExit("Set IMAP_USER and IMAP_PASS in .env first.")

    box = imaplib.IMAP4_SSL(HOST, 993)
    box.login(USER, PASS)

    status, raw_folders = box.list()
    if status != "OK":
        raise SystemExit("Could not list folders.")

    folders = []
    for line in raw_folders:
        name = parse_folder_name(line.decode(errors="replace"))
        if name and name not in SYSTEM_FOLDERS:
            folders.append(name)

    print(f"Found {len(folders)} non-system folders. Censusing...\n")

    rows = []
    for i, name in enumerate(folders, 1):
        try:
            # Folder names contain spaces and punctuation — quote them.
            status, _ = box.select(f'"{name}"', readonly=True)
            if status != "OK":
                rows.append((name, None, None, None, "unreadable"))
                continue

            _, data = box.search(None, "ALL")
            ids = data[0].split()
            count = len(ids)

            first = message_date(box, ids[0]) if count else None
            last = message_date(box, ids[-1]) if count else None
            rows.append((name, count, first, last, ""))
        except Exception as exc:
            rows.append((name, None, None, None, f"error: {exc}"))

        if i % 25 == 0:
            print(f"  ...{i}/{len(folders)}")

    box.logout()

    def fmt(dt):
        return dt.strftime("%Y-%m") if isinstance(dt, datetime) else "?"

    print(f"\n{'=' * 78}")
    print(f"{'FOLDER':<44} {'MSGS':>6}  {'FIRST':>8}  {'LAST':>8}")
    print("=" * 78)
    for name, count, first, last, note in sorted(
        rows, key=lambda r: (r[1] is None, -(r[1] or 0))
    ):
        label = (name[:42] + "..") if len(name) > 44 else name
        if note:
            print(f"{label:<44} {note}")
        else:
            print(f"{label:<44} {count:>6}  {fmt(first):>8}  {fmt(last):>8}")

    # Summary
    counted = [r for r in rows if isinstance(r[1], int)]
    total = sum(r[1] for r in counted)
    dated = [r for r in counted if isinstance(r[3], datetime)]
    by_year = {}
    for _, _, _, last, _ in dated:
        by_year[last.year] = by_year.get(last.year, 0) + 1

    print(f"\n{'=' * 78}\nSUMMARY\n{'=' * 78}")
    print(f"  Folders censused:            {len(counted)}")
    print(f"  Total filed messages:        {total}")
    if counted:
        print(f"  Median folder size:          "
              f"{sorted(r[1] for r in counted)[len(counted) // 2]}")
    print("\n  Folders by year of LAST filed message"
          "  (where filing stopped):")
    for year in sorted(by_year):
        bar = "#" * min(by_year[year], 60)
        print(f"    {year}  {by_year[year]:>4}  {bar}")

    print("""
  READ THIS AS:
    - Total filed messages = the ceiling on available labeled examples.
    - The year histogram = when the manual system was actually in use,
      and when it was abandoned.
    - Folders with few messages are probably one-offs, not real categories.
    - Before trusting ANY of it as training data: open 3 folders, read 20
      messages each, and check whether they actually belong. Stale labels
      are only useful if they were accurate when made.
""")


if __name__ == "__main__":
    main()
