"""
Asbuilt — Ingestion
===================

Pulls mail from IMAP into a local database. No AI, no classification, no
interpretation. Just capture, and capture never fails: every message,
attachment, and shared link is recorded whether or not anything downstream
understands it.

    python ingest.py                    # sync configured folders
    python ingest.py --folders INBOX    # just one
    python ingest.py --reset            # start over (drops sync state only)

Resumable: tracks the last UID seen per folder, so re-running is cheap.
Read-only against the mailbox — it can never alter or delete mail.
"""

import argparse
import email
import email.utils
import hashlib
import imaplib
import os
import re
import sqlite3
import sys
from datetime import timezone
from email.header import decode_header, make_header

from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "asbuilt.db")
BLOB_DIR = os.path.join(HERE, "data", "attachments")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")

HOST = os.getenv("IMAP_HOST", "imap.mail.yahoo.com")
USER = os.getenv("IMAP_USER")
PASS = os.getenv("IMAP_PASS")

DEFAULT_FOLDERS = ["INBOX", "Sent"]

# Attachments below this are almost always signature logos / tracking pixels
# (image001.png, Outlook-*.png). Stored, but flagged so they never pollute
# document counts or search results.
INLINE_SIZE_LIMIT = 20 * 1024

INLINE_NAME_HINTS = re.compile(
    r"^(image\d+\.(png|jpg|jpeg|gif)|outlook-[a-z0-9]+\.(png|jpg)|"
    r"logo|signature|spacer)", re.I
)

# Documents often arrive as links rather than attachments. Links expire, so
# the fact of delivery is recorded even when the file is unreachable.
LINK_PROVIDERS = [
    ("dropbox",         re.compile(r"https?://[^\s<>\"]*dropbox\.com/[^\s<>\"]+", re.I)),
    ("wetransfer",      re.compile(r"https?://[^\s<>\"]*wetransfer\.com/[^\s<>\"]+", re.I)),
    # Non-capturing groups throughout: re.findall returns the GROUP, not the
    # whole match, whenever a pattern contains a capturing group.
    ("googledrive",     re.compile(r"https?://(?:drive|docs)\.google\.com/[^\s<>\"]+", re.I)),
    ("onedrive",        re.compile(r"https?://[^\s<>\"]*(?:1drv\.ms|onedrive\.live\.com)/[^\s<>\"]+", re.I)),
    ("sharepoint",      re.compile(r"https?://[^\s<>\"]*sharepoint\.com/[^\s<>\"]+", re.I)),
    ("box",             re.compile(r"https?://[^\s<>\"]*box\.com/[^\s<>\"]+", re.I)),
    ("egnyte",          re.compile(r"https?://[^\s<>\"]*egnyte\.com/[^\s<>\"]+", re.I)),
    ("sharefile",       re.compile(r"https?://[^\s<>\"]*sharefile\.com/[^\s<>\"]+", re.I)),
    ("smartsheet",      re.compile(r"https?://[^\s<>\"]*smartsheet\.com/[^\s<>\"]+", re.I)),
    ("submittalexchange", re.compile(r"https?://[^\s<>\"]*submittalexchange\.com/[^\s<>\"]+", re.I)),
    ("procore",         re.compile(r"https?://[^\s<>\"]*procore\.com/[^\s<>\"]+", re.I)),
    ("autodesk",        re.compile(r"https?://[^\s<>\"]*(?:autodesk|bim360)\.[^\s<>\"]+", re.I)),
]


def decode_str(raw):
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def parse_date(raw):
    """RFC date header -> ISO8601 UTC string, or None."""
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def addr_list(raw):
    if not raw:
        return ""
    return ", ".join(a for _, a in email.utils.getaddresses([decode_str(raw)]) if a)


HTML_DOC = re.compile(r"<!DOCTYPE\s+html|<html[\s>]|<body[\s>]|<table[\s>]", re.I)


def looks_like_html(text):
    """Is this markup rather than prose?"""
    if not text:
        return False
    head = text[:4000]
    return bool(HTML_DOC.search(head)) or head.count("<") > 30


def html_to_text(html):
    """Real HTML → text.

    A regex that deletes <tags> leaves everything those tags CONTAINED: the
    contents of <style> and <script> blocks (raw CSS), and every &nbsp;/&zwnj;
    entity. Marketing mail is mostly that, and it was showing up as the largest
    "boilerplate" in the corpus. BeautifulSoup drops those subtrees outright and
    decodes entities, which matters the moment a customer's GC sends
    HTML-formatted business mail rather than plain text.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # Degrade to the old behavior rather than losing the message body.
        return re.sub(r"<[^>]+>", " ", html)

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script", "head", "meta", "link", "title"]):
        tag.decompose()
    # Block elements become line breaks so paragraph structure survives — the
    # boilerplate learner works line by line and needs real lines.
    for tag in soup.find_all(["br", "p", "div", "tr", "li", "h1", "h2", "h3"]):
        tag.append("\n")
    text = soup.get_text(" ")
    text = re.sub(r"[ \t ​‌‍]+", " ", text)   # nbsp/zwnj/zwsp
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def body_text(msg):
    """Best-effort plain text. Falls back to parsed HTML."""
    html_fallback = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_filename():
                continue
            ctype = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                text = payload.decode(
                    part.get_content_charset() or "utf-8", errors="replace"
                )
            except Exception:
                continue
            if ctype == "text/plain":
                return text
            if ctype == "text/html" and not html_fallback:
                html_fallback = text
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode(
                    msg.get_content_charset() or "utf-8", errors="replace"
                )
                # A single-part message can still be text/html — an earlier
                # version returned the raw markup here, storing whole HTML
                # documents as "body text".
                if msg.get_content_type() == "text/html" or looks_like_html(text):
                    return html_to_text(text)
                return text
        except Exception:
            pass

    if html_fallback:
        return html_to_text(html_fallback)
    return ""


def connect_imap():
    """Fresh authenticated connection. Yahoo caps session length and will drop
    long syncs, so this gets called again mid-run to resume."""
    box = imaplib.IMAP4_SSL(HOST, 993)
    box.login(USER, PASS)
    return box


def save_progress(conn, mailbox_id, folder, uid):
    """Persist the resume point continuously, not just at the end — a dropped
    connection must never cost re-downloading thousands of messages."""
    conn.execute(
        """INSERT INTO sync_state (mailbox_id, folder, last_uid)
           VALUES (?, ?, ?)
           ON CONFLICT(mailbox_id, folder)
           DO UPDATE SET last_uid = excluded.last_uid,
                         updated_at = datetime('now')""",
        (mailbox_id, folder, uid),
    )
    conn.commit()


def connect_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(BLOB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def get_mailbox_id(conn, address, host):
    cur = conn.execute("SELECT id FROM mailbox WHERE address = ?", (address,))
    row = cur.fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO mailbox (address, host) VALUES (?, ?)", (address, host)
    )
    return cur.lastrowid


def store_attachment(conn, part):
    """Content-addressed storage: identical bytes stored once, however many
    times the same drawing set gets forwarded."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return None, None

    filename = decode_str(part.get_filename()) or "unnamed"
    digest = hashlib.sha256(payload).hexdigest()
    size = len(payload)
    inline = 1 if (size < INLINE_SIZE_LIMIT and INLINE_NAME_HINTS.match(filename)) else 0

    row = conn.execute(
        "SELECT id FROM attachment WHERE sha256 = ?", (digest,)
    ).fetchone()
    if row:
        return row["id"], filename

    subdir = os.path.join(BLOB_DIR, digest[:2])
    os.makedirs(subdir, exist_ok=True)
    path = os.path.join(subdir, digest)
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(payload)

    cur = conn.execute(
        """INSERT INTO attachment (sha256, size_bytes, content_type,
                                   stored_path, is_inline)
           VALUES (?, ?, ?, ?, ?)""",
        (digest, size, part.get_content_type(),
         os.path.relpath(path, HERE), inline),
    )
    return cur.lastrowid, filename


# --- repair of bodies stored by the old regex-based HTML stripper ------------
# Those bodies kept whatever <style>/<script> CONTAINED (raw CSS, JSON-LD) and
# left entities undecoded. We don't retain raw HTML, so re-downloading 20k
# messages is the only perfect fix — these patterns get ~all of it for free.

# Bare CSS left behind after tags were stripped: no markup to parse, just
# "@media screen and (max-width:480px) { .layout-table { ... } }" as prose.
# Gated on a CSS signal so brace-bearing business text is never touched.
CSS_SIGNAL = re.compile(r"@media\b|\{[^{}]{0,200}:[^{}]{0,200};|\.[A-Za-z][\w-]*\s*\{")
BRACE_BLOCK = re.compile(r"\{[^{}]*\}")
# Orphaned selectors, once their blocks are gone. Must START with . # or @ so a
# sentence never matches, or be a comma-separated selector list.
ORPHAN_SELECTOR = re.compile(
    r"(?m)^[ \t]*(?:[.#@][\w\-][\w\-.,#:>\[\]=\"'() \t]{0,240}"
    r"|[A-Za-z][\w\-]*(?:[.#][\w\-]+)?(?:\s*,\s*[\w\-.#:>\[\]]+){2,}\s*,?)[ \t]*$")
CSS_RULE = re.compile(r"[^\s{}]{1,80}\s*\{[^{}]{0,400}:[^{}]{0,400}\}")


def strip_bare_css(text):
    if not text or not CSS_SIGNAL.search(text):
        return text
    for _ in range(8):                      # innermost-out; @media nests
        stripped = BRACE_BLOCK.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    text = ORPHAN_SELECTOR.sub(" ", text)
    return re.sub(r"(?m)^[ \t]*[{}][ \t]*$", " ", text)   # stray braces
JSON_LD = re.compile(r'\[?\s*\{\s*"@context".{0,3000}?\}\s*\]?', re.S)
MSO_COND = re.compile(r"<!--\[if[^\]]*\]>.*?<!\[endif\]-->", re.S)
ZERO_WIDTH = re.compile(r"[ ​‌‍⁠﻿]+")


def repair_html_remnants(text):
    """Re-clean one stored body. Two damage classes, routed differently.

    Class 1 — raw markup that was never stripped (single-part text/html
    messages, which the old body_text() returned verbatim). Send it through the
    real parser: BeautifulSoup drops <style>/<script> subtrees whole, which the
    remnant patterns below can't do once tags are gone.

    Class 2 — tags already stripped, but whatever <style>/<script> CONTAINED
    was left behind as loose CSS and undecoded entities. Pattern cleanup.
    """
    if not text:
        return text
    import html as html_mod

    if looks_like_html(text):
        text = html_to_text(text)

    text = MSO_COND.sub(" ", text)
    text = JSON_LD.sub(" ", text)
    text = html_mod.unescape(text)
    text = ZERO_WIDTH.sub(" ", text)
    text = strip_bare_css(text)
    # Repeat: CSS blocks nest, and one pass leaves the outer rule behind.
    for _ in range(3):
        cleaned = CSS_RULE.sub(" ", text)
        if cleaned == text:
            break
        text = cleaned
    # Any tags the old pass missed. Must look like a real tag: an angle bracket
    # followed by a tag name and then whitespace or '>'. A looser pattern eats
    # <adisa.azeez@stvinc.com> and <https://...>, which are the thread's
    # participants and links — content, not markup.
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]{0,300})?/?>", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def repair_bodies(conn):
    """Re-clean stored bodies in place. Idempotent — safe to run repeatedly."""
    rows = conn.execute(
        "SELECT id, body_text FROM message WHERE body_text IS NOT NULL"
    ).fetchall()
    print(f"checking {len(rows)} stored bodies ...")

    changed = saved = 0
    for i, row in enumerate(rows, 1):
        fixed = repair_html_remnants(row["body_text"])
        if fixed != row["body_text"]:
            saved += len(row["body_text"]) - len(fixed)
            conn.execute("UPDATE message SET body_text = ? WHERE id = ?",
                         (fixed, row["id"]))
            changed += 1
        if i % 2000 == 0:
            conn.commit()
            print(f"  ...{i}/{len(rows)}")
    conn.commit()

    # The FTS index mirrors message rows via triggers on INSERT/DELETE only, so
    # an UPDATE leaves it holding the old text. Rebuild it.
    conn.execute("INSERT INTO message_fts(message_fts) VALUES('rebuild')")
    conn.commit()
    print(f"repaired {changed} bodies, removed {saved:,} characters; "
          f"FTS index rebuilt")


def relink_all(conn):
    """Re-scan every stored message body for shared links.

    Message bodies live in the database, so fixing a link pattern never means
    re-downloading mail. Clears existing links first so a corrected pattern
    replaces bad rows rather than adding to them.
    """
    conn.execute("DELETE FROM shared_link")
    rows = conn.execute(
        "SELECT id, body_text FROM message WHERE body_text IS NOT NULL"
    ).fetchall()
    total = 0
    for i, row in enumerate(rows, 1):
        total += extract_links(conn, row["id"], row["body_text"])
        if i % 2000 == 0:
            conn.commit()
            print(f"  ...{i}/{len(rows)}")
    conn.commit()
    print(f"Re-extracted {total} links from {len(rows)} messages.")


def extract_links(conn, message_row_id, text):
    found = 0
    for provider, pattern in LINK_PROVIDERS:
        for url in set(pattern.findall(text)):
            if isinstance(url, tuple):        # defensive: capturing group slipped in
                continue
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO shared_link (message_id, url, provider)
                       VALUES (?, ?, ?)""",
                    (message_row_id, url[:2000], provider),
                )
                found += 1
            except sqlite3.Error:
                pass
    return found


def ingest_folder(conn, mailbox_id, folder, reset=False):
    """Owns its own IMAP connection so it can rebuild it after a server drop."""
    box = connect_imap()
    status, _ = box.select(f'"{folder}"', readonly=True)
    if status != "OK":
        print(f"  {folder}: cannot open, skipping")
        box.logout()
        return 0, 0, 0

    state = conn.execute(
        "SELECT last_uid FROM sync_state WHERE mailbox_id = ? AND folder = ?",
        (mailbox_id, folder),
    ).fetchone()
    last_uid = 0 if reset else (state["last_uid"] if state else 0)

    if not reset and last_uid == 0:
        # A previous run may have died before writing progress. Messages are
        # committed as they land, so recover the resume point from them.
        row = conn.execute(
            """SELECT MAX(uid) AS max_uid FROM message_location
               WHERE mailbox_id = ? AND folder = ?""",
            (mailbox_id, folder),
        ).fetchone()
        if row and row["max_uid"]:
            last_uid = row["max_uid"]
            print(f"  {folder}: recovered resume point at uid {last_uid}")

    status, data = box.uid("SEARCH", None, f"UID {last_uid + 1}:*")
    if status != "OK":
        print(f"  {folder}: search failed")
        return 0, 0, 0

    uids = [int(u) for u in data[0].split()] if data[0] else []
    uids = [u for u in uids if u > last_uid]
    if not uids:
        print(f"  {folder}: up to date")
        return 0, 0, 0

    print(f"  {folder}: {len(uids)} new")

    new_msgs = new_atts = new_links = 0
    highest = last_uid

    for i, uid in enumerate(uids, 1):
        raw = None
        for attempt in range(4):
            try:
                status, raw = box.uid("FETCH", str(uid), "(RFC822)")
                if status == "OK":
                    break
                raw = None
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError) as exc:
                # Yahoo drops long sessions. Rebuild and carry on from here.
                print(f"    connection lost at uid {uid} ({exc}); reconnecting")
                try:
                    box.logout()
                except Exception:
                    pass
                try:
                    box = connect_imap()
                    box.select(f'"{folder}"', readonly=True)
                except Exception as reconnect_exc:
                    print(f"    reconnect failed: {reconnect_exc}")
                    save_progress(conn, mailbox_id, folder, highest)
                    return new_msgs, new_atts, new_links

        if raw is None or not isinstance(raw[0], tuple):
            continue          # unreadable message; skip rather than abort

        msg = email.message_from_bytes(raw[0][1])
        highest = max(highest, uid)

        msg_id_hdr = (msg.get("Message-ID") or "").strip() or None
        text = body_text(msg)

        existing = None
        if msg_id_hdr:
            existing = conn.execute(
                "SELECT id FROM message WHERE message_id = ?", (msg_id_hdr,)
            ).fetchone()

        if existing:
            row_id = existing["id"]          # same mail, another mailbox/folder
        else:
            from_name, from_email = email.utils.parseaddr(
                decode_str(msg.get("From"))
            )
            cur = conn.execute(
                """INSERT INTO message (message_id, sent_at, from_name, from_email,
                                        to_emails, cc_emails, subject, body_text,
                                        in_reply_to, thread_refs)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg_id_hdr,
                    parse_date(msg.get("Date")),
                    from_name,
                    from_email.lower() if from_email else None,
                    addr_list(msg.get("To")),
                    addr_list(msg.get("Cc")),
                    decode_str(msg.get("Subject")),
                    text,
                    (msg.get("In-Reply-To") or "").strip() or None,
                    (msg.get("References") or "").strip() or None,
                ),
            )
            row_id = cur.lastrowid
            new_msgs += 1

            for part in msg.walk():
                if part.get_filename():
                    att_id, filename = store_attachment(conn, part)
                    if att_id:
                        conn.execute(
                            """INSERT OR IGNORE INTO message_attachment
                               (message_id, attachment_id, filename)
                               VALUES (?, ?, ?)""",
                            (row_id, att_id, filename),
                        )
                        new_atts += 1

            new_links += extract_links(conn, row_id, text)

        conn.execute(
            """INSERT OR IGNORE INTO message_location
               (message_id, mailbox_id, folder, uid) VALUES (?, ?, ?, ?)""",
            (row_id, mailbox_id, folder, uid),
        )

        if i % 100 == 0:
            save_progress(conn, mailbox_id, folder, highest)
            print(f"    ...{i}/{len(uids)}")

    save_progress(conn, mailbox_id, folder, highest)
    try:
        box.logout()
    except Exception:
        pass
    return new_msgs, new_atts, new_links


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folders", nargs="*", default=None)
    ap.add_argument("--reset", action="store_true",
                    help="re-scan folders from UID 1 (existing rows are kept)")
    ap.add_argument("--relink", action="store_true",
                    help="re-extract shared links from stored bodies; no IMAP")
    ap.add_argument("--repair-html", action="store_true",
                    help="re-clean stored bodies of CSS/entity remnants; no IMAP")
    args = ap.parse_args()

    if args.relink or args.repair_html:
        conn = connect_db()
        if args.repair_html:
            repair_bodies(conn)
        if args.relink:
            relink_all(conn)
        conn.close()
        return

    if not (USER and PASS):
        sys.exit("Set IMAP_USER and IMAP_PASS in .env first.")

    folders = args.folders or DEFAULT_FOLDERS
    conn = connect_db()
    mailbox_id = get_mailbox_id(conn, USER, HOST)

    print(f"Connecting to {HOST} as {USER}")

    totals = [0, 0, 0]
    for folder in folders:
        m, a, l = ingest_folder(conn, mailbox_id, folder, reset=args.reset)
        totals = [totals[0] + m, totals[1] + a, totals[2] + l]

    counts = conn.execute(
        """SELECT (SELECT COUNT(*) FROM message)    AS messages,
                  (SELECT COUNT(*) FROM attachment) AS attachments,
                  (SELECT COUNT(*) FROM attachment WHERE is_inline = 0) AS real_docs,
                  (SELECT COUNT(*) FROM shared_link) AS links"""
    ).fetchone()

    print(f"\nThis run: +{totals[0]} messages, +{totals[1]} attachments, "
          f"+{totals[2]} links")
    print(f"Database: {counts['messages']} messages, "
          f"{counts['real_docs']} documents "
          f"({counts['attachments']} incl. signature images), "
          f"{counts['links']} shared links")
    print(f"\n  {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
