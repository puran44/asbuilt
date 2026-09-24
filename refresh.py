"""
Asbuilt — refresh everything
============================

One command instead of five. Runs the whole pipeline in dependency order:

    ingest              new mail from every connected mailbox
    learn_boilerplate   per-sender signatures/disclaimers (needs new mail first)
    extract_candidates  strings that could be job identifiers
    resolve_projects    identifiers -> projects, assignments, thread propagation
    extract_documents   text out of new attachments

Every stage is resumable and idempotent, so a daily run costs seconds — it only
touches what arrived since last time. The first run after a fresh ingest is the
slow one.

    python refresh.py                 # everything
    python refresh.py --skip-ingest   # rebuild from what's already stored (no IMAP)
    python refresh.py --only documents
"""

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Order is dependency order, not preference. Boilerplate must be learned before
# anything reads bodies for meaning; candidates must exist before projects can
# be resolved from them.
STAGES = [
    ("ingest", "ingest.py", [], "pulling new mail"),
    ("boilerplate", "learn_boilerplate.py", [], "learning sender boilerplate"),
    ("candidates", "extract_candidates.py", [], "extracting job identifiers"),
    ("projects", "resolve_projects.py", ["--min-messages", "3"], "resolving projects"),
    ("documents", "extract_documents.py", [], "reading document text"),
]


def run_stage(name, script, args, label):
    print(f"\n=== {label} " + "=" * max(0, 52 - len(label)))
    started = time.time()
    result = subprocess.run(
        [sys.executable, os.path.join(HERE, script), *args],
        cwd=HERE,
        # Child scripts print progress; let it stream rather than buffering.
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    elapsed = time.time() - started
    if result.returncode != 0:
        print(f"\n!! {name} failed (exit {result.returncode}) after {elapsed:.0f}s")
        print("   Later stages depend on this one — stopping here.")
        return False
    print(f"--- {name} done in {elapsed:.0f}s")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-ingest", action="store_true",
                    help="rebuild from stored mail without touching IMAP")
    ap.add_argument("--only", metavar="STAGE",
                    choices=[s[0] for s in STAGES],
                    help="run a single stage: " + ", ".join(s[0] for s in STAGES))
    args = ap.parse_args()

    stages = STAGES
    if args.only:
        stages = [s for s in STAGES if s[0] == args.only]
    elif args.skip_ingest:
        stages = [s for s in STAGES if s[0] != "ingest"]

    started = time.time()
    for name, script, extra, label in stages:
        if not run_stage(name, script, extra, label):
            sys.exit(1)

    print(f"\n{'=' * 60}")
    print(f"refresh complete in {time.time() - started:.0f}s")
    print("  app:  python app.py    ->  http://127.0.0.1:5000")


if __name__ == "__main__":
    main()
