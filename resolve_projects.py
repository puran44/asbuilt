"""
Asbuilt — Project Resolution (deterministic pass)
=================================================

Turns candidate identifiers into actual projects, and assigns messages to them.
Everything here is rule-based and free: no model calls. It handles the cases
that don't need judgment, so the LLM stage only sees what's genuinely hard.

Four steps:
  1. Promote frequent identifiers to projects, with every observed spelling
     recorded as an alias.
  2. Assign messages by direct identifier match (subject beats filename beats
     body, since a job named in the subject is stronger evidence than one
     buried in a quoted chain).
  3. Suppress per-sender letterhead: an address appearing in most mail from
     one sender is that sender's office, not a jobsite.
  4. Propagate through reply threads: a message with no identifier inherits
     its parent's project. "Re: Re: Re:" carries the job silently.

    python resolve_projects.py
    python resolve_projects.py --min-messages 3
"""

import argparse
import os
import re
import sqlite3
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "asbuilt.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")

SOURCE_CONFIDENCE = {"subject": 0.95, "filename": 0.85, "body": 0.6}

BOROUGH = {"q": "Queens", "k": "Brooklyn", "m": "Manhattan",
           "x": "Bronx", "r": "Staten Island"}


def display_name(normalized, kind):
    if kind == "school":
        match = re.match(r"^([qkmxr?])(\d+)$", normalized)
        if match:
            letter, number = match.groups()
            if letter == "?":
                return f"School {number} (borough unknown)"
            return f"{letter.upper()}{number} ({BOROUGH[letter]})"
    if kind == "sca_contract":
        return f"SCA Contract C{normalized}"
    if kind == "sca_ls":
        return f"SCA LS{normalized}"
    if kind == "design_no":
        return f"Design No. D{normalized}"
    if kind == "address":
        return normalized
    return normalized


def suppress_sender_letterhead(conn, ratio=0.6, min_msgs=8):
    """Drop address candidates that are a given sender's own letterhead.

    Global frequency only catches the mailbox owner's address. A counterparty's
    office address is rare overall but appears in nearly everything THEY send —
    so measure frequency per sender, exactly as with parsing conventions.
    """
    rows = conn.execute("""
        SELECT c.normalized, m.from_email,
               COUNT(DISTINCT c.message_id) AS hits
        FROM candidate c JOIN message m ON m.id = c.message_id
        WHERE c.kind = 'address' AND m.from_email IS NOT NULL
        GROUP BY c.normalized, m.from_email
        HAVING hits >= ?""", (min_msgs,)).fetchall()

    sender_totals = {r["from_email"]: r["n"] for r in conn.execute(
        """SELECT from_email, COUNT(*) n FROM message
           WHERE from_email IS NOT NULL GROUP BY from_email""")}

    removed = 0
    for row in rows:
        total = sender_totals.get(row["from_email"], 0)
        if total and row["hits"] / total >= ratio:
            cur = conn.execute(
                """DELETE FROM candidate
                   WHERE kind = 'address' AND normalized = ?
                     AND message_id IN (SELECT id FROM message WHERE from_email = ?)""",
                (row["normalized"], row["from_email"]))
            removed += cur.rowcount
    conn.commit()
    return removed


def resolve_unknown_boroughs(conn):
    """'?354' means a school number with no borough letter. If exactly one
    borough variant of that number exists in the mailbox, it's that school."""
    merged = 0
    unknowns = conn.execute(
        """SELECT DISTINCT normalized FROM candidate
           WHERE kind = 'school' AND normalized LIKE '?%'""").fetchall()
    for row in unknowns:
        number = row["normalized"][1:]
        matches = conn.execute(
            """SELECT DISTINCT normalized FROM candidate
               WHERE kind = 'school' AND normalized LIKE ?
                 AND normalized NOT LIKE '?%'""", (f"_{number}",)).fetchall()
        if len(matches) == 1:
            conn.execute(
                """UPDATE OR IGNORE candidate SET normalized = ?
                   WHERE kind = 'school' AND normalized = ?""",
                (matches[0]["normalized"], row["normalized"]))
            merged += 1
    conn.commit()
    return merged


def build_projects(conn, min_messages):
    conn.execute("DELETE FROM message_project")
    conn.execute("DELETE FROM project_alias")
    conn.execute("DELETE FROM project")

    # TRUST MODEL: create projects only from SUBJECTS and FILENAMES; match from
    # anywhere once the project exists.
    #
    # Subjects and filenames are author-chosen and job-specific. Bodies contain
    # signature blocks, quoted chains, and other people's letterhead. Verified
    # against this mailbox: the office address 117-02 Atlantic Avenue appears
    # 637 times in bodies and ZERO times in subject lines. That single rule
    # separates jobsite addresses from letterhead without any blocklist, and it
    # lets addresses back in as first-class project identifiers.
    groups = conn.execute("""
        SELECT normalized, kind, COUNT(DISTINCT message_id) m
        FROM candidate
        WHERE source IN ('subject', 'filename')
        GROUP BY normalized, kind
        HAVING m >= ?
        ORDER BY m DESC""", (min_messages,)).fetchall()

    created = 0
    for row in groups:
        cur = conn.execute(
            "INSERT INTO project (name) VALUES (?)",
            (display_name(row["normalized"], row["kind"]),))
        project_id = cur.lastrowid
        created += 1

        # Every spelling actually observed becomes a confirmed alias: these
        # were derived mechanically, not guessed, so they need no human review.
        for alias in conn.execute(
            """SELECT DISTINCT raw FROM candidate
               WHERE normalized = ? AND kind = ?""",
                (row["normalized"], row["kind"])):
            conn.execute(
                """INSERT OR IGNORE INTO project_alias
                   (project_id, alias, source, confirmed) VALUES (?, ?, ?, 1)""",
                (project_id, alias["raw"], "observed"))
        conn.execute(
            """INSERT OR IGNORE INTO project_alias
               (project_id, alias, source, confirmed) VALUES (?, ?, ?, 1)""",
            (project_id, row["normalized"], "normalized"))

        for hit in conn.execute(
            """SELECT message_id, source FROM candidate
               WHERE normalized = ? AND kind = ?""",
                (row["normalized"], row["kind"])):
            conn.execute(
                """INSERT INTO message_project
                   (message_id, project_id, confidence, method, confirmed)
                   VALUES (?, ?, ?, 'identifier', 0)
                   ON CONFLICT(message_id, project_id) DO UPDATE SET
                     confidence = MAX(confidence, excluded.confidence)""",
                (hit["message_id"], project_id,
                 SOURCE_CONFIDENCE.get(hit["source"], 0.5)))
    conn.commit()
    return created


CLEAN_SCHOOL = re.compile(r"^(P\.?\s?S\.?|I\.?\s?S\.?|H\.?\s?S\.?|M\.?\s?S\.?)\s*0*(\d{1,4})$",
                          re.I)


def prettify_names(conn):
    """Rename projects to what the contractor actually calls them.

    Two problems this fixes, both visible on the first screen:
      * address projects were showing their normalized key ("300jay")
      * schools with no borough letter read "School 30 (borough unknown)",
        which is internal bookkeeping, not a job name

    The human forms are already in the alias table — the raw strings people
    typed. Pick the best one and use that.
    """
    renamed = 0
    for proj in conn.execute("SELECT id, name FROM project").fetchall():
        aliases = [
            " ".join(r["alias"].split())            # aliases wrap across lines
            for r in conn.execute(
                "SELECT alias FROM project_alias WHERE project_id = ?", (proj["id"],))
        ]
        aliases = [a for a in aliases if a and not a.startswith("?")]
        if not aliases:
            continue
        new = None

        if proj["name"].startswith("School ") and "borough unknown" in proj["name"]:
            # "PS 30" is what he says; the borough letter is what we couldn't
            # determine. Show the former, don't advertise the latter.
            clean = [a for a in aliases if CLEAN_SCHOOL.match(a)]
            if clean:
                m = CLEAN_SCHOOL.match(min(clean, key=len))
                prefix = re.sub(r"[^A-Za-z]", "", m.group(1)).upper()
                new = f"{prefix} {int(m.group(2))}"

        elif proj["name"][:1].isdigit():            # address-keyed project
            best = max(aliases, key=len)
            new = best.title() if best.isupper() else best
            new = re.sub(r"\s+", " ", new).strip(" .,")

        if new and new != proj["name"]:
            conn.execute("UPDATE project SET name = ? WHERE id = ?", (new, proj["id"]))
            renamed += 1
    conn.commit()
    return renamed


def merge_addresses_into_projects(conn, ratio=0.6, min_msgs=3):
    """Fold an address project into the coded project it belongs to.

    A jobsite address is not a separate job -- "109-10 47th Avenue" IS Q28, and
    IS 002 has a street address that means the same thing. When an address's
    messages consistently also carry one school or contract code, the address
    is an alias of that project, not a project of its own.

    This is the "addresses as corroborating evidence" idea: they identify, but
    they don't get to create.
    """
    merged = 0
    address_projects = conn.execute("""
        SELECT p.id, p.name, COUNT(DISTINCT mp.message_id) m
        FROM project p JOIN message_project mp ON mp.project_id = p.id
        WHERE p.name GLOB '[0-9]*'
        GROUP BY p.id HAVING m >= ?""", (min_msgs,)).fetchall()

    for addr in address_projects:
        partners = conn.execute("""
            SELECT mp2.project_id, COUNT(*) shared
            FROM message_project mp1
            JOIN message_project mp2 ON mp2.message_id = mp1.message_id
            JOIN project p2 ON p2.id = mp2.project_id
            WHERE mp1.project_id = ? AND mp2.project_id != ?
              AND p2.name NOT GLOB '[0-9]*'
            GROUP BY mp2.project_id ORDER BY shared DESC LIMIT 1""",
            (addr["id"], addr["id"])).fetchone()

        if not partners or partners["shared"] / addr["m"] < ratio:
            continue

        target = partners["project_id"]
        # The address and all its spellings become aliases of the real project.
        conn.execute("""
            INSERT OR IGNORE INTO project_alias (project_id, alias, source, confirmed)
            SELECT ?, alias, 'address', 1 FROM project_alias WHERE project_id = ?""",
            (target, addr["id"]))
        conn.execute("""
            INSERT OR IGNORE INTO message_project
                (message_id, project_id, confidence, method, confirmed)
            SELECT message_id, ?, confidence, 'address_alias', 0
            FROM message_project WHERE project_id = ?""", (target, addr["id"]))
        conn.execute("DELETE FROM project WHERE id = ?", (addr["id"],))
        merged += 1

    conn.commit()
    return merged


def propagate_threads(conn, max_passes=6):
    """A reply inherits its parent's project. Handles the very common case of a
    named opening message followed by a long 'Re:' chain that names nothing."""
    id_by_message_id = {
        r["message_id"]: r["id"] for r in conn.execute(
            "SELECT id, message_id FROM message WHERE message_id IS NOT NULL")}

    parents = defaultdict(set)
    for row in conn.execute(
            "SELECT id, in_reply_to, thread_refs FROM message"):
        refs = []
        if row["in_reply_to"]:
            refs += re.findall(r"<[^>]+>", row["in_reply_to"])
        if row["thread_refs"]:
            refs += re.findall(r"<[^>]+>", row["thread_refs"])[-3:]
        for ref in refs:
            parent = id_by_message_id.get(ref)
            if parent and parent != row["id"]:
                parents[row["id"]].add(parent)

    total = 0
    for _ in range(max_passes):
        assigned = defaultdict(set)
        for row in conn.execute("SELECT message_id, project_id FROM message_project"):
            assigned[row["message_id"]].add(row["project_id"])

        added = 0
        for child, parent_ids in parents.items():
            if child in assigned:
                continue
            inherited = set()
            for parent in parent_ids:
                inherited |= assigned.get(parent, set())
            # Only inherit when the thread points at exactly one project;
            # an ambiguous parent should stay ambiguous.
            if len(inherited) == 1:
                conn.execute(
                    """INSERT OR IGNORE INTO message_project
                       (message_id, project_id, confidence, method, confirmed)
                       VALUES (?, ?, 0.7, 'thread', 0)""",
                    (child, next(iter(inherited))))
                added += 1
        conn.commit()
        total += added
        if not added:
            break
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-messages", type=int, default=4,
                    help="mentions needed before an identifier becomes a project")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        conn.executescript(fh.read())

    total = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]

    print("1. suppressing per-sender letterhead ...")
    print(f"   removed {suppress_sender_letterhead(conn)} address candidates")

    print("2. resolving unknown boroughs ...")
    print(f"   merged {resolve_unknown_boroughs(conn)} identifiers")

    print("3. building projects ...")
    print(f"   created {build_projects(conn, args.min_messages)} projects")

    direct = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM message_project").fetchone()[0]
    print(f"   {direct} messages assigned by identifier "
          f"({direct/max(total,1):.0%})")

    print("4. folding addresses into the projects they identify ...")
    print(f"   merged {merge_addresses_into_projects(conn)} address projects")

    print("5. naming projects the way the contractor does ...")
    print(f"   renamed {prettify_names(conn)} projects")

    print("6. propagating through reply threads ...")
    gained = propagate_threads(conn)
    after = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM message_project").fetchone()[0]
    print(f"   +{gained} messages inherited a project from their thread")

    print(f"\n{'=' * 70}\nRESULT\n{'=' * 70}")
    print(f"  {after} of {total} messages assigned ({after/max(total,1):.0%})")
    print(f"  {total - after} unassigned\n")

    print("TOP PROJECTS")
    for r in conn.execute("""
        SELECT p.name, COUNT(DISTINCT mp.message_id) m,
               (SELECT COUNT(*) FROM project_alias a WHERE a.project_id = p.id) aliases,
               MIN(msg.sent_at) first_seen, MAX(msg.sent_at) last_seen
        FROM project p
        JOIN message_project mp ON mp.project_id = p.id
        JOIN message msg ON msg.id = mp.message_id
        GROUP BY p.id ORDER BY m DESC LIMIT 30"""):
        span = ""
        if r["first_seen"] and r["last_seen"]:
            span = f"  {r['first_seen'][:7]} -> {r['last_seen'][:7]}"
        print(f"  {r['m']:5} msgs  {r['aliases']:3} aliases  {r['name']:34}{span}")

    conn.close()


if __name__ == "__main__":
    main()
