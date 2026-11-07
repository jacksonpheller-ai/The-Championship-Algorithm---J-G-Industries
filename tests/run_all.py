"""Run every tests/test_*.py in its own interpreter and fail if any fails.
Usage: python tests/run_all.py"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    failed = []
    for f in files:
        name = os.path.basename(f)
        r = subprocess.run([sys.executable, f], capture_output=True, text=True, cwd=os.path.dirname(HERE))
        tail = (r.stdout.strip().splitlines() or [""])[-1]
        print(f"{'PASS' if r.returncode == 0 else 'FAIL'}  {name}: {tail}")
        if r.returncode != 0:
            failed.append(name)
            print(r.stdout[-2000:])
            print(r.stderr[-4000:])
    print(f"{len(files) - len(failed)}/{len(files)} test files passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
