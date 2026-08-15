"""
Orchestrator — runs phases per category.
Usage:
  python main.py -c uncategorized --limit 300        # full pipeline (1→4)
  python main.py -c uncategorized --no-pdp           # listing-only (1→3)
  python main.py -c test-measurement --phase 2       # single phase
  python main.py --all-categories --confirm-full-scrape
"""
import argparse
import os
import subprocess
import sys

import config


SCRIPTS = {
    1: "phase1_generator.py",
    2: "phase2_fetcher.py",
    3: "phase3_parser.py",
    4: "phase4_pdp.py",
}


def run_phase(phase_num, category, extra_args):
    script = SCRIPTS[phase_num]
    cmd = [sys.executable, script, "-c", category] + extra_args
    print(f"\n{'='*60}")
    print(f"  PHASE {phase_num}: {script}  |  category={category}")
    print(f"  {' '.join(cmd)}")
    print(f"{'='*60}\n")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"[main] Phase {phase_num} ({category}) failed with exit code {result.returncode}")
        sys.exit(result.returncode)
    print(f"[main] Phase {phase_num} ({category}) completed successfully")


def main():
    parser = argparse.ArgumentParser(description="Datasheets.com Scraper Pipeline")
    parser.add_argument("--phase", default="all",
                        choices=["1", "2", "3", "4", "all"],
                        help="Which phase to run. Default 'all' runs the full pipeline (1→4).")
    parser.add_argument("-c", "--category", default=None, help="Single category slug")
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap unique products (e.g. 300). Truncates listing pages in phases 1–2, parse in 3, PDP in 4.")
    parser.add_argument("--no-pdp", action="store_true",
                        help="Skip Phase 4 (product detail pages); produce listing-level output only.")
    # Deprecated: Phase 4 now runs by default. Accepted so older commands don't break.
    parser.add_argument("--pdp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--all-categories", action="store_true",
                        help="Run for every slug in config.CATEGORIES_TO_SCRAPE")
    parser.add_argument("--confirm-full-scrape", action="store_true",
                        help="Required with --all-categories (prevents accidental multi-day runs)")
    args = parser.parse_args()

    if args.all_categories:
        if not args.confirm_full_scrape:
            parser.error(
                "--all-categories affects every slug in config and can take days. "
                "Pass --confirm-full-scrape if you mean it, or use -c ONE_SLUG for a test run."
            )
        categories = list(config.CATEGORIES_TO_SCRAPE)
    elif args.category:
        categories = [args.category.lower().strip()]
    else:
        parser.error("Provide --category SLUG or --all-categories")

    extra = ["--limit", str(args.limit)] if args.limit else []
    extra4 = list(extra)
    phase = args.phase
    run_pdp = not args.no_pdp  # Phase 4 runs by default; --no-pdp opts out.

    for category in categories:
        print(f"\n{'#'*60}\n# CATEGORY: {category}\n{'#'*60}")
        products_path = f"data/output/{category}_products.json"
        if phase == "all":
            # Resume shortcut: if listing output already exists and we're doing PDP,
            # skip re-crawling and go straight to Phase 4. Delete the *_products.json
            # to force a fresh listing crawl.
            skip_listing = run_pdp and os.path.isfile(products_path)
            if skip_listing:
                print(f"[main] {products_path} already exists — skipping phases 1–3, running Phase 4 only")
            else:
                for n in (1, 2, 3):
                    run_phase(n, category, extra)
            if run_pdp:
                run_phase(4, category, extra4)
        else:
            run_phase(int(phase), category, extra4 if phase == "4" else extra)

    print("\n[main] Requested work complete.")
    if args.limit and args.category:
        print(f"[main] Listing products: data/output/{categories[0]}_products.json")
        if run_pdp or phase == "4":
            print(f"[main] PDP records:    data/output/{categories[0]}_final.jsonl")


if __name__ == "__main__":
    # `-pdp` is parsed as `--phase dp` if `-p` exists; treat it as `--pdp`.
    sys.argv = ["--pdp" if a == "-pdp" else a for a in sys.argv]
    main()
