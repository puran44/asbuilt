"""
Asbuilt — Evaluation labeling
=============================

Draws a frozen stratified sample and scores the resolver against hand labels.
The web UI for labeling lives in app.py at /label.

    python label.py draw            # draw the sample once (150 assigned + 250 unassigned)
    python label.py score           # precision, recall, composition
    python label.py export          # dump labels to CSV

The sample is drawn ONCE and never redrawn, so numbers stay comparable across
resolver versions. Redrawing would let you tune against your own test set.
"""

import argparse
import csv
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "data", "asbuilt.db")
SCHEMA_PATH = os.path.join(HERE, "schema.sql")


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    return conn


def draw(conn, n_assigned=50, n_unassigned=100):
    """Deliberately small.

    At n=100 the 95% error bar is about +/-8 points -- enough to tell 60% from
    85% from 98%, which is the only distinction that changes what you do next.
    Going to n=400 buys +/-4 points and costs three extra hours. Label the small
    sample, act on it, and draw more only if a decision actually hinges on the
    difference. The unassigned stratum gets the larger share because nobody has
    any idea what is in those messages, and that answer determines whether 30%
    coverage is a success or a failure.
    """
    existing = conn.execute("SELECT COUNT(*) FROM gold_sample").fetchone()[0]
    if existing:
        print(f"Sample already drawn: {existing} messages. Refusing to redraw —")
        print("redrawing would let you tune against your own test set.")
        print("To start over deliberately: DELETE FROM gold_sample;")
        return

    conn.execute("""
        INSERT INTO gold_sample (message_id, stratum)
        SELECT id, 'assigned' FROM message
        WHERE id IN (SELECT message_id FROM message_project)
        ORDER BY RANDOM() LIMIT ?""", (n_assigned,))
    conn.execute("""
        INSERT INTO gold_sample (message_id, stratum)
        SELECT id, 'unassigned' FROM message
        WHERE id NOT IN (SELECT message_id FROM message_project)
        ORDER BY RANDOM() LIMIT ?""", (n_unassigned,))
    conn.commit()

    for r in conn.execute(
            "SELECT stratum, COUNT(*) n FROM gold_sample GROUP BY stratum"):
        print(f"  {r['stratum']:12} {r['n']}")
    print("\nNow label them:  python app.py  ->  http://127.0.0.1:5000/label")


def draw_new(conn, n=40):
    """Sample messages assigned by the CURRENT resolver but never labeled.

    Loosening patterns to fix recall usually costs precision. The original
    sample only measures the version that drew it, so each round of changes
    needs a fresh slice of newly-assigned mail to confirm precision held.
    """
    cur = conn.execute("""
        INSERT OR IGNORE INTO gold_sample (message_id, stratum)
        SELECT DISTINCT mp.message_id, 'assigned_v2'
        FROM message_project mp
        WHERE mp.message_id NOT IN (SELECT message_id FROM gold_sample)
        ORDER BY RANDOM() LIMIT ?""", (n,))
    conn.commit()
    print(f"Drew {cur.rowcount} newly-assigned messages to verify.")
    print("Label them at /label, then: python label.py score")


def score(conn):
    total_msgs = conn.execute("SELECT COUNT(*) FROM message").fetchone()[0]
    total_assigned = conn.execute(
        "SELECT COUNT(DISTINCT message_id) FROM message_project").fetchone()[0]
    total_unassigned = total_msgs - total_assigned

    def rows(stratum):
        return conn.execute("""
            SELECT l.* FROM gold_label l
            JOIN gold_sample s ON s.message_id = l.message_id
            WHERE s.stratum = ?""", (stratum,)).fetchall()

    unassigned = rows("unassigned")

    print("=" * 66)
    print("RESOLVER EVALUATION")
    print("=" * 66)

    for stratum, caption in (("assigned", "PRECISION — original sample"),
                             ("assigned_v2", "PRECISION — current resolver")):
        assigned = rows(stratum)
        if not assigned:
            continue
        correct = sum(1 for r in assigned if r["verdict"] == "correct")
        wrong = sum(1 for r in assigned if r["verdict"] == "wrong")
        amb = sum(1 for r in assigned if r["verdict"] == "ambiguous")
        n = len(assigned)
        print(f"\n{caption}  (n={n} labeled of {total_assigned} assigned)")
        print(f"  correct    {correct:4}  {correct/n:6.1%}")
        print(f"  wrong      {wrong:4}  {wrong/n:6.1%}")
        print(f"  ambiguous  {amb:4}  {amb/n:6.1%}")
        # Wilson-ish sanity band: at n=100 the 95% interval is roughly +/-8pts.
        print(f"  -> assignment precision ~{correct/n:.0%} "
              f"(+/- ~{(0.98/ (n ** 0.5)) * 100:.0f} pts at 95%)")

    if not unassigned:
        print("\nNo unassigned messages labeled yet.")
        return

    n = len(unassigned)
    print(f"\nWHAT IS IN THE UNASSIGNED PILE  (n={n} labeled of "
          f"{total_unassigned})")
    comp = {}
    for r in unassigned:
        comp[r["entity_type"] or "unlabeled"] = comp.get(
            r["entity_type"] or "unlabeled", 0) + 1
    for kind, count in sorted(comp.items(), key=lambda x: -x[1]):
        est = int(count / n * total_unassigned)
        print(f"  {kind:14} {count:4}  {count/n:6.1%}   (~{est} messages)")

    # These messages were unassigned when the sample was drawn. Later resolver
    # versions may have picked them up, so recall must be measured against the
    # CURRENT state, not against what was true at labeling time.
    construction = [r for r in unassigned if r["entity_type"] == "construction"]
    recovered = sum(
        1 for r in construction
        if conn.execute("SELECT 1 FROM message_project WHERE message_id=?",
                        (r["message_id"],)).fetchone())
    missed = len(construction) - recovered
    missing_proj = sum(1 for r in unassigned if r["belongs_to"] == "missing")

    print(f"\nRECALL")
    if recovered:
        print(f"  {recovered} of {len(construction)} labeled misses have since "
              f"been picked up by resolver changes")
    print(f"  construction mail still unassigned: {missed}/{n} = {missed/n:.1%}")
    est_missed = int(missed / n * total_unassigned)
    print(f"  -> roughly {est_missed} project messages missed across the mailbox")
    if missing_proj:
        print(f"  {missing_proj} belong to projects the system NEVER DISCOVERED")

    latest = rows("assigned_v2") or rows("assigned")
    labeled_construction = sum(
        1 for r in latest if r["verdict"] in ("correct", "ambiguous"))
    if latest:
        good = total_assigned * (labeled_construction / len(latest))
        est_true_total = good + est_missed
        if est_true_total:
            recall = good / est_true_total
            print(f"\n  ESTIMATED RECALL over construction mail: {recall:.0%}")
            print("  (This is the number that matters — not '% of inbox assigned',")
            print("   which mostly measures how much spam the mailbox receives.)")


def export(conn):
    path = os.path.join(HERE, "data", "gold_labels.csv")
    rows = conn.execute("""
        SELECT s.stratum, l.*, m.subject, m.from_email, m.sent_at
        FROM gold_label l
        JOIN gold_sample s ON s.message_id = l.message_id
        JOIN message m ON m.id = l.message_id""").fetchall()
    if not rows:
        print("Nothing labeled yet.")
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r))
    print(f"Wrote {len(rows)} labels -> {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command",
                    choices=["draw", "draw-new", "score", "export"])
    args = ap.parse_args()
    conn = connect()
    {"draw": draw, "draw-new": draw_new,
     "score": score, "export": export}[args.command](conn)
    conn.close()


if __name__ == "__main__":
    main()
