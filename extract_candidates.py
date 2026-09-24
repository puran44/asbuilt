"""
Asbuilt — Candidate Identifier Extraction (stage 1 of 2)
========================================================

Finds every string in the mailbox that COULD be a job identifier. Deliberately
high-recall and low-precision: "690" also shows up in dollar amounts and phone
numbers, and that is fine, because stage 2 (the LLM) decides what is real.

Patterns come from identifiers actually observed in this mailbox, but nothing
here is assumed universal -- a different contractor will have different ones,
and unmatched text still falls through to content reading.

    python extract_candidates.py            # extract and report
    python extract_candidates.py --report   # report only, no re-extraction
"""

import argparse
import os
import re
import sqlite3
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "asbuilt.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")

# NYC school identifiers. Borough letters: Q=Queens K=Brooklyn M=Manhattan
# X=Bronx R=Staten Island. The same school appears as Q690, "Q 690", "690 (Q)",
# "HS 690Q", "P.S. 167(K)", "K167" -- all normalize to the same key.
PATTERNS = [
    # P.S. 167(K) / PS 167 K / I.S. 144 (Bronx) / HS 965K
    ("school", re.compile(
        r"\b(?:P\.?\s?S\.?|I\.?\s?S\.?|H\.?\s?S\.?|M\.?\s?S\.?)\s*#?\s*"
        r"(\d{1,4})\s*\(?\s*([QKMXR])?\s*\)?", re.I)),
    # Q690 / Q 690 / K167 / X162 -- borough letter first
    ("school", re.compile(r"\b([QKMXR])\s?-?\s?(\d{2,4})\b")),
    # 690(Q) / 690 (Q) -- number first
    ("school", re.compile(r"\b(\d{2,4})\s*\(\s*([QKMXR])\s*\)")),
    # 690Q -- number then borough, as in "HS 690Q"
    ("school", re.compile(r"\b(\d{2,4})([QKMXR])\b")),

    # SCA contract numbers: C000015851, C#80934
    ("sca_contract", re.compile(r"\bC\s?#?\s?0*(\d{5,12})\b")),
    # SCA / LS work order: LS0086
    ("sca_ls", re.compile(r"\bLS\s?-?\s?(\d{3,6})\b", re.I)),
    # Design numbers: D022431
    ("design_no", re.compile(r"\bD\s?-?\s?(\d{6})\b")),
    # Addendum / solicitation: 26-22858D-1, 25-22431D-1
    ("solicitation", re.compile(r"\b(\d{2}-\d{4,6}[A-Z]-\d)\b")),
    # OGS contracts seen in this mailbox: M2947, Q1330P
    ("ogs", re.compile(r"\b([MQ]\d{3,4}[A-Z]?)\s*\(\s*OGS\s*\)", re.I)),
    # NYC DOB filing / request numbers. A whole identifier family found by
    # inspecting real misses: CERT-ADV-LA-20-003342, REQ-PL-SO-22-0010867,
    # REQ-SP-SO-23-0001740. Stable and unique per filing.
    ("dob_request", re.compile(r"\b([A-Z]{3,4}(?:-[A-Z]{2,3}){1,3}-\d{2}-\d{5,8})\b")),
    # DOB job numbers: 421409407BL, 421629795. First digit is the borough
    # (1 Manhattan, 2 Bronx, 3 Brooklyn, 4 Queens, 5 Staten Island).
    # REQUIRES nearby context -- a bare 9-digit pattern also matches bank
    # routing numbers (021000021 is JPMorgan Chase), account numbers, and
    # tracking numbers. See CONTEXT_REQUIRED below.
    ("dob_job", re.compile(r"\b([1-5]\d{8})(?:[A-Z]{2})?\b")),

    # NYC street addresses. Case-insensitive, because real subjects contain
    # "2240 TIEBOUT AVENUE", "300 jay street", and "106-18 Rockaway BLvd".
    # Street names may be numbers ("41-14 29 Street") or ordinals ("520 W 151st").
    ("address", re.compile(
        r"\b(\d{1,4}(?:-\d{1,4})?\s+"                    # 116-25, 2388
        r"(?:[NSEW]\.?\s+)?"                             # optional W / E.
        r"(?:[A-Za-z][A-Za-z.]*|\d{1,3}(?:st|nd|rd|th)?)"  # Guy | 29 | 151st
        r"(?:\s+[A-Za-z][A-Za-z.]*){0,2}"                # R Brewer
        r"\s*(?:Ave|Avenue|St|Street|Blvd|Boulevard|Pkwy|Parkway|"
        r"Rd|Road|Dr|Drive|Ln|Lane|Pl|Place)\.?)(?=\W|$)", re.I)),
    # Ordinal streets with the suffix omitted entirely: "520 W 151st".
    ("address", re.compile(
        r"\b(\d{1,4}(?:-\d{1,4})?\s+(?:[NSEW]\.?\s+)?\d{1,3}(?:st|nd|rd|th))"
        r"(?=\W|$)", re.I)),
]

# Words that produce noise when they sit next to a number.
NOISE_CONTEXT = re.compile(
    r"(invoice|check\s?#|phone|tel|fax|zip|suite|ste\.|room\s|rm\s|\$)", re.I)

# Addresses that follow these phrases are mailing, billing, or delivery
# addresses -- never jobsites. Found by labeling: the owner's home address
# ("send check to my home address: 182 Landau Avenue") and Hilti delivery
# notifications were both being promoted to projects.
DELIVERY_CONTEXT = re.compile(
    r"(mail(ing)?\s+address|send\s+(the\s+)?check|ship(ping)?\s+(to|address)|"
    r"deliver(y|ed)?\s+(to|address)|home\s+address|bill(ing)?\s+(to|address)|"
    r"remit\s+to|attention:|attn:?)", re.I)
DELIVERY_WINDOW = 120

# A small contractor's inbox is also his personal inbox, so his own home
# address turns up in subject lines about appraisals and insurance policies --
# passing the subject-trust rule while being nobody's jobsite. These words
# anywhere in the same subject disqualify an address from creating a project.
PERSONAL_ADDRESS_CONTEXT = re.compile(
    r"(appraisal|\bpolicy\b|homeowner|mortgage|refinanc|deed|escrow|"
    r"closing\s+statement|property\s+tax|title\s+insurance)", re.I)

# Some identifier kinds are pure digits and therefore indistinguishable from
# account, routing, and tracking numbers on shape alone. These require a
# supporting word nearby before the match is accepted.
CONTEXT_REQUIRED = {
    "dob_job": re.compile(
        r"(dob|job\s?#|job\s?no|filing|permit|bis|application|"
        r"sign\s?off|inspection|plumbing|sprinkler|boiler|work\s?type)", re.I),
}
CONTEXT_WINDOW = 90


# Street suffixes vary freely: "361 Court St" and "361 Court Street" are one
# job written two ways. Collapse to a canonical form.
STREET_SUFFIX = {
    "ave": "avenue", "av": "avenue", "avenue": "avenue",
    "st": "street", "street": "street",
    "blvd": "boulevard", "boulevard": "boulevard",
    "rd": "road", "road": "road",
    "dr": "drive", "drive": "drive",
    "pkwy": "parkway", "parkway": "parkway",
    "ln": "lane", "lane": "lane",
    "pl": "place", "place": "place",
}


def normalize(kind, groups):
    """Collapse spelling variants to one key: 'Q 690', '690(Q)', 'HS 690Q' -> q690."""
    parts = [g for g in groups if g]
    if kind == "school" and len(parts) == 2:
        a, b = parts
        letter, number = (a, b) if a.isalpha() else (b, a)
        return f"{letter.lower()}{int(number)}"
    if kind == "school" and len(parts) == 1:
        return f"?{int(parts[0])}"          # school number, borough unknown

    value = parts[0] if parts else ""
    if kind == "address":
        # Drop the street suffix from the key entirely. People write the same
        # address as "109-10 47th Avenue", "109-10 47th", "361 Court St", and
        # "361 Court Street"; canonicalizing the suffix isn't enough, because
        # sometimes there is no suffix to canonicalize. House number plus
        # street name is specific enough on its own.
        words = re.sub(r"[.,#]", " ", value).lower().split()
        while words and words[-1] in STREET_SUFFIX:
            words.pop()
        # "47th Avenue" and "47 Avenue" are the same street.
        words = [re.sub(r"^(\d+)(st|nd|rd|th)$", r"\1", w) for w in words]
        return "".join(words)
    return re.sub(r"[\s\-#.]", "", value).lower()


def extract_from(text, source, seen):
    """Yield (raw, normalized, kind, source) tuples.

    Patterns overlap by design -- several address forms match the same text.
    Longest match wins at any given position, so "109-10 47th Avenue" yields
    one identifier instead of both "109-1047th" and "109-1047thavenue".
    """
    if not text:
        return
    claimed = []          # (start, end) spans already taken by a longer match

    def overlaps(start, end):
        return any(start < e and end > s for s, e in claimed)

    # Longest matches first, so a short pattern cannot claim a span that a
    # longer one would have covered.
    matches = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            matches.append((match.end() - match.start(), kind, match))
    matches.sort(key=lambda t: -t[0])

    for _, kind, match in matches:
        # Address patterns overlap each other; other kinds don't, and can
        # legitimately sit inside an address (a school code in a street name).
        if kind == "address":
            if overlaps(match.start(), match.end()):
                continue
            claimed.append((match.start(), match.end()))

        raw = match.group(0).strip()
        norm = normalize(kind, match.groups())
        if not norm or norm == "?0":
            continue

        # Cheap noise suppression: skip when the 60 characters before the
        # match look like an invoice, phone number, or dollar figure.
        window = text[max(0, match.start() - 60):match.start()]
        if NOISE_CONTEXT.search(window):
            continue

        # A mailing or delivery address is never a jobsite, and neither is the
        # owner's own house showing up in insurance and appraisal mail.
        if kind == "address":
            lead = text[max(0, match.start() - DELIVERY_WINDOW):match.start()]
            if DELIVERY_CONTEXT.search(lead):
                continue
            if PERSONAL_ADDRESS_CONTEXT.search(text):
                continue

        # Digit-only identifiers must be corroborated by a nearby word,
        # looking both directions -- shape alone cannot separate a DOB job
        # number from a bank routing number.
        required = CONTEXT_REQUIRED.get(kind)
        if required:
            around = text[max(0, match.start() - CONTEXT_WINDOW):
                          match.end() + CONTEXT_WINDOW]
            if not required.search(around):
                continue

        key = (norm, kind, source)
        if key in seen:
            continue
        seen.add(key)
        yield raw, norm, kind, source


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="skip extraction")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        conn.executescript(fh.read())

    if not args.report:
        conn.execute("DELETE FROM candidate")
        rows = conn.execute(
            "SELECT id, subject, body_text FROM message").fetchall()
        print(f"Scanning {len(rows)} messages ...")

        total = 0
        for i, row in enumerate(rows, 1):
            seen = set()
            found = []
            found += list(extract_from(row["subject"], "subject", seen))
            # Bodies include quoted history; cap the scan so a 200-message
            # forward chain does not dominate.
            found += list(extract_from((row["body_text"] or "")[:20000],
                                       "body", seen))

            for filename in conn.execute(
                "SELECT filename FROM message_attachment WHERE message_id = ?",
                (row["id"],)
            ):
                found += list(extract_from(filename["filename"], "filename", seen))

            for raw, norm, kind, source in found:
                conn.execute(
                    """INSERT OR IGNORE INTO candidate
                       (message_id, raw, normalized, kind, source)
                       VALUES (?, ?, ?, ?, ?)""",
                    (row["id"], raw[:120], norm, kind, source))
                total += 1

            if i % 2000 == 0:
                conn.commit()
                print(f"  ...{i}/{len(rows)}")
        conn.commit()
        print(f"Extracted {total} candidate identifiers.\n")

    # ---- report ----
    def head(title):
        print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")

    total_msgs = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]

    # Boilerplate detection by document frequency. An identifier appearing in a
    # large share of ALL mail is letterhead, not a project -- the sender's own
    # office address sits in every signature. No blocklist needed, and this
    # generalizes to any customer without configuration.
    DF_LIMIT = 0.05          # >5% of all messages = boilerplate
    cutoff = max(int(total_msgs * DF_LIMIT), 25)

    boiler = {r["normalized"] for r in conn.execute(
        """SELECT normalized FROM candidate
           GROUP BY normalized HAVING COUNT(DISTINCT message_id) > ?""",
        (cutoff,))}

    head(f"BOILERPLATE EXCLUDED  (appears in >{cutoff} messages = letterhead)")
    for r in conn.execute("""SELECT normalized, kind, COUNT(DISTINCT message_id) m
                             FROM candidate GROUP BY normalized, kind
                             HAVING m > ? ORDER BY m DESC""", (cutoff,)):
        print(f"  {r['m']:6} msgs  {r['normalized']:26} {r['kind']}")
    if not boiler:
        print("  (none)")

    head("CANDIDATES BY KIND")
    for r in conn.execute("""SELECT kind, COUNT(*) n, COUNT(DISTINCT normalized) d
                             FROM candidate GROUP BY kind ORDER BY n DESC"""):
        print(f"  {r['kind']:14} {r['n']:6} mentions   {r['d']:5} distinct")

    head("TOP IDENTIFIERS  (boilerplate removed — this is the project list)")
    for r in conn.execute("""SELECT normalized, kind, COUNT(DISTINCT message_id) m
                             FROM candidate GROUP BY normalized, kind
                             HAVING m <= ? ORDER BY m DESC LIMIT 40""", (cutoff,)):
        print(f"  {r['m']:5} msgs  {r['normalized']:22} {r['kind']}")

    head("SPELLING VARIANTS  (the alias problem, measured)")
    print("  Same normalized key, different raw text:\n")
    for r in conn.execute("""SELECT normalized, COUNT(DISTINCT raw) v,
                                    COUNT(DISTINCT message_id) m
                             FROM candidate WHERE kind = 'school'
                             GROUP BY normalized HAVING v > 2
                             ORDER BY m DESC LIMIT 12"""):
        forms = conn.execute(
            """SELECT DISTINCT raw FROM candidate
               WHERE normalized = ? LIMIT 10""", (r["normalized"],)).fetchall()
        print(f"  {r['normalized']:10} ({r['m']} msgs, {r['v']} forms): "
              + ", ".join(f'"{f["raw"]}"' for f in forms))

    head("COVERAGE")
    raw_cov = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM candidate").fetchone()[0]
    real_cov = conn.execute(
        """SELECT COUNT(DISTINCT message_id) FROM candidate
           WHERE normalized NOT IN (
               SELECT normalized FROM candidate GROUP BY normalized
               HAVING COUNT(DISTINCT message_id) > ?)""", (cutoff,)).fetchone()[0]
    school_cov = conn.execute(
        """SELECT COUNT(DISTINCT message_id) FROM candidate
           WHERE kind = 'school' AND normalized NOT IN (
               SELECT normalized FROM candidate GROUP BY normalized
               HAVING COUNT(DISTINCT message_id) > ?)""", (cutoff,)).fetchone()[0]

    print(f"  including boilerplate : {raw_cov:6} / {total_msgs} "
          f"({raw_cov/max(total_msgs,1):.0%})  <- misleading")
    print(f"  real identifiers      : {real_cov:6} / {total_msgs} "
          f"({real_cov/max(total_msgs,1):.0%})")
    print(f"  school codes only     : {school_cov:6} / {total_msgs} "
          f"({school_cov/max(total_msgs,1):.0%})")
    print(f"\n  {total_msgs - real_cov} messages carry no usable identifier.")
    print("  Those need content reading, thread context (a reply inherits its")
    print("  parent's project), or are simply not project mail — bid blasts,")
    print("  newsletters, insurance certificates, spam.")

    conn.close()


if __name__ == "__main__":
    main()
