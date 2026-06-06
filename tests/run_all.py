"""Run every test file in its OWN process.

The ERP and MES packages both have modules named `config`, `database`,
`mqtt_client`, `main`; running them in one interpreter would clash in
sys.modules. One subprocess per test file keeps the caches isolated.

    python3 tests/run_all.py
"""
import os
import sys
import glob
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    failed = []
    for f in files:
        name = os.path.basename(f)
        print(f"\n=== {name} ===")
        rc = subprocess.call([sys.executable, f], cwd=HERE)
        if rc != 0:
            failed.append(name)
    print("\n" + "=" * 50)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        sys.exit(1)
    print(f"ALL {len(files)} TEST FILES PASSED")


if __name__ == "__main__":
    main()
