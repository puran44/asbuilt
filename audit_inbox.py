"""
Asbuilt — Inbox Audit
=====================

The first real test of the product thesis: does a plumbing contractor's inbox
contain enough structure to build a project record from?

This connects to an IMAP mailbox (Yahoo by default), samples recent messages,
and reports on what's actually in there. It writes NOTHING and changes NOTHING —
it opens the mailbox read-only.

SETUP
-----
1. In Yahoo Mail: Settings > More Settings > Mailboxes > confirm IMAP is on.
2. At https://login.yahoo.com/account/security > "Generate app password".
   (Yahoo blocks plain-password IMAP logins; the app password is the sanctioned path.)
3. Create a file named `.env` next to this script:

       IMAP_HOST=imap.mail.yahoo.com
       IMAP_USER=hisaddress@yahoo.com
       IMAP_PASS=the-16-char-app-password

4. pip install python-dotenv
5. python audit_inbox.py

NEVER commit .env. Add it to .gitignore before your first commit.
"""

import email
import imaplib
import os
import re
from collections import Counter
from email.header import decode_header, make_header

from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("IMAP_HOST", "imap.mail.yahoo.com")
USER = os.getenv("IMAP_USER")
PASS = os.getenv("IMAP_PASS")
SAMPLE_SIZE = 400  # most recent N messages
MAILBOX = os.getenv("IMAP_MAILBOX", "INBOX")  # override to audit "Sent" or a subfolder

# Platform senders worth detecting: if these show up, structured parsing is easy.
PLATFORM_PATTERNS = {
    "Procore": r"procore\.com",
    "Autodesk/ACC": r"autodesk\.com|buildingconnected",
    "Buildertrend": r"buildertrend\.com",
    "PlanGrid": r"plangrid\.com",
    "Bluebeam": r"bluebeam\.com",
    "DocuSign": r"docusign",
}

# Construction vocabulary — rough signal for how much project content is here.
KEYWORDS = [
    "rfi", "submittal", "change order", "co #", "punch", "as-built", "asbuilt",
    "pay app", "payment application", "requisition", "lien", "waiver",
    "shop drawing", "transmittal", "addendum", "asi", "coordination",
    "t&m", "time and material", "backcharge", "retainage", "closeout",
]

ATTACHMENT_KINDS = {
    "pdf": [".pdf"],
    "image": [".jpg", ".jpeg", ".png", ".heic", ".gif"],
    "office": [".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"],
    "drawing": [".dwg", ".dxf", ".rvt", ".ifc"],
}


def decode(raw):
    """Email headers arrive in assorted encodings; normalize to str."""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def classify_attachment(filename):
    lower = filename.lower()
    for kind, exts in ATTACHMENT_KINDS.items():
        if any(lower.endswith(e) for e in exts):
            return kind
    return "other"


def main():
    if not (USER and PASS):
        raise SystemExit("Set IMAP_USER and IMAP_PASS in .env first. See docstring.")

    print(f"Connecting to {HOST} as {USER} ...")
    box = imaplib.IMAP4_SSL(HOST, 993)
    box.login(USER, PASS)

    # List every folder IMAP exposes. Linked external accounts (e.g. a Gmail
    # address connected inside Yahoo) sometimes appear here as their own folder.
    print("\nFolders visible over IMAP:")
    status, folders = box.list()
    if status == "OK":
        for entry in folders:
            print(f"  {entry.decode(errors='replace')}")
    print()

    # Quote the mailbox name — folder names contain spaces and punctuation,
    # and IMAP treats an unquoted space as an argument separator.
    target = MAILBOX.strip().strip('"')
    status, detail = box.select(f'"{target}"', readonly=True)  # readonly: cannot alter the mailbox
    if status != "OK":
        # Fail loudly and help find the right name instead of dying downstream.
        available = []
        for line in folders or []:
            name = line.decode(errors="replace")
            match = re.search(r'"([^"]*)"\s*$', name)
            if match:
                available.append(match.group(1))

        needle = target.lower()
        close = [f for f in available if needle in f.lower() or f.lower() in needle]

        print(f"\nCould not open mailbox {target!r}.")
        print(f"Server said: {detail}")
        if close:
            print("\nDid you mean one of these? Copy the name EXACTLY into .env:")
            for name in close:
                print(f"    IMAP_MAILBOX={name}")
        else:
            print("\nNo similar folder names found. Folders containing a digit:")
            for name in available:
                if any(ch.isdigit() for ch in name):
                    print(f"    IMAP_MAILBOX={name}")
        box.logout()
        raise SystemExit(1)

    _, data = box.search(None, "ALL")
    ids = data[0].split()
    total = len(ids)
    sample = ids[-SAMPLE_SIZE:]
    print(f"Mailbox holds {total} messages. Sampling the most recent {len(sample)}.\n")

    senders = Counter()
    domains = Counter()
    platforms = Counter()
    keywords = Counter()
    attachments = Counter()
    subjects = []
    with_attachments = 0

    for i, msg_id in enumerate(sample, 1):
        _, raw = box.fetch(msg_id, "(RFC822)")
        msg = email.message_from_bytes(raw[0][1])

        sender = decode(msg.get("From"))
        subject = decode(msg.get("Subject"))
        subjects.append(subject)
        senders[sender] += 1

        match = re.search(r"@([\w.-]+)", sender)
        if match:
            domains[match.group(1).lower()] += 1

        for name, pattern in PLATFORM_PATTERNS.items():
            if re.search(pattern, sender, re.I):
                platforms[name] += 1

        haystack = subject.lower()
        for kw in KEYWORDS:
            if kw in haystack:
                keywords[kw] += 1

        has_attachment = False
        for part in msg.walk():
            filename = part.get_filename()
            if filename:
                attachments[classify_attachment(decode(filename))] += 1
                has_attachment = True
        if has_attachment:
            with_attachments += 1

        if i % 50 == 0:
            print(f"  ...{i}/{len(sample)}")

    box.logout()

    def section(title):
        print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")

    section("TOP SENDER DOMAINS  (who is he actually talking to?)")
    for domain, count in domains.most_common(25):
        print(f"  {count:5}  {domain}")

    section("PLATFORM NOTIFICATIONS  (structured parsing available?)")
    if platforms:
        for name, count in platforms.most_common():
            print(f"  {count:5}  {name}")
    else:
        print("  NONE FOUND — confirms the email-native thesis.")
        print("  Parser priority: form PDFs and prose, not platform notifications.")

    section("CONSTRUCTION KEYWORDS IN SUBJECTS  (project signal density)")
    if keywords:
        for kw, count in keywords.most_common():
            print(f"  {count:5}  {kw}")
    else:
        print("  None found in subjects — check message bodies before concluding.")

    section("ATTACHMENTS")
    print(f"  {with_attachments}/{len(sample)} messages carry attachments "
          f"({with_attachments / max(len(sample), 1):.0%})")
    for kind, count in attachments.most_common():
        print(f"  {count:5}  {kind}")

    section("SUBJECT SAMPLE  (look for project-name patterns: Q004, PS 004, ...)")
    for subject in subjects[-40:]:
        print(f"  {subject[:100]}")

    section("WHAT TO CONCLUDE")
    print("""
  1. Sender domains → how many distinct GCs, suppliers, and inspectors.
  2. Platform notifications → if zero, structured parsing must come from
     form PDFs and prose. That is the harder and more defensible problem.
  3. Keyword density → whether project content is thick enough to be worth
     parsing, or whether the inbox is mostly noise.
  4. Attachment rate → how central document handling is. High PDF counts
     mean the document pipeline is the product, not a side feature.
  5. Subject patterns → the raw material for the project alias table.
""")


if __name__ == "__main__":
    main()
