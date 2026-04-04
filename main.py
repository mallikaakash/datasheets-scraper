"""
Orchestrator — runs all three phases sequentially.
Usage: python main.py [--phase 1|2|3|all]
"""
import argparse
import subprocess
import sys


def run_phase(phase_num):
    scripts = {
        1: "phase1_generator.py",
        2: "phase2_fetcher.py",
        3: "phase3_parser.py",
    }
    script = scripts[phase_num]
    print(f"\n{'='*60}")
    print(f"  PHASE {phase_num}: {script}")
    print(f"{'='*60}\n")
    result = subprocess.run([sys.executable, script])
    if result.returncode != 0:
        print(f"[main] Phase {phase_num} failed with exit code {result.returncode}")
        sys.exit(result.returncode)
    print(f"[main] Phase {phase_num} completed successfully")


def main():
    parser = argparse.ArgumentParser(description="Datasheets.com Scraper Pipeline")
    parser.add_argument("--phase", "-p", default="all",
                        choices=["1", "2", "3", "all"],
                        help="Which phase to run (default: all)")
    args = parser.parse_args()

    phase = args.phase

    if phase == "all":
        run_phase(1)
        run_phase(2)
        run_phase(3)
        print("\n[mana] All phases complete!")
    else:
        run_phase(int(phase))


if __name__ == "__main__":
    main()
