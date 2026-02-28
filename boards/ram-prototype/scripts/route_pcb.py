#!/usr/bin/env python3
"""
Autoroute the RAM prototype PCB using FreeRouting.

Pipeline:
  1. Ensure FreeRouting JAR is available (download if missing)
  2. Export Specctra DSN from ram.kicad_pcb via KiCad's pcbnew Python
  3. Run FreeRouting CLI (headless) to produce a .ses session file
  4. Import .ses back into KiCad and save as ram_routed.kicad_pcb
  5. Run verify_pcb.py --post-routing on the result

The unrouted ram.kicad_pcb is never modified — routing output goes to
ram_routed.kicad_pcb.

Usage:
    cd boards/ram-prototype
    python scripts/route_pcb.py              # Full autoroute pipeline
    python scripts/route_pcb.py --dry-run    # Export DSN only, don't route
    python scripts/route_pcb.py --passes 30  # Override max routing passes
"""

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
import urllib.request

# Add shared library to path
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "shared", "python")))

from kicad_gen.common import KICAD_PYTHON, FREEROUTING_JAR

# --------------------------------------------------------------
# Configuration
# --------------------------------------------------------------

BOARD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
PCB_INPUT = os.path.join(BOARD_DIR, "ram.kicad_pcb")
PCB_ROUTED = os.path.join(BOARD_DIR, "ram_routed.kicad_pcb")
DSN_PATH = os.path.join(BOARD_DIR, "ram.dsn")
SES_PATH = os.path.join(BOARD_DIR, "ram.ses")

FREEROUTING_URL = (
    "https://github.com/freerouting/freerouting/releases/download/"
    "v2.0.1/freerouting-2.0.1.jar"
)

VERIFY_SCRIPT = os.path.join(BOARD_DIR, "scripts", "verify_pcb.py")


# --------------------------------------------------------------
# Step 1: Ensure FreeRouting JAR
# --------------------------------------------------------------

def ensure_freerouting_jar(jar_path):
    """Download FreeRouting JAR if not present. Returns resolved path."""
    if os.path.isfile(jar_path):
        print(f"  FreeRouting JAR: {jar_path}")
        return jar_path

    jar_dir = os.path.dirname(jar_path)
    os.makedirs(jar_dir, exist_ok=True)

    print(f"  Downloading FreeRouting v2.0.1 ...")
    print(f"  URL: {FREEROUTING_URL}")
    try:
        urllib.request.urlretrieve(FREEROUTING_URL, jar_path)
    except Exception as e:
        print(f"  ERROR: Download failed: {e}")
        print(f"  Please download manually to: {jar_path}")
        sys.exit(1)

    size_mb = os.path.getsize(jar_path) / (1024 * 1024)
    print(f"  Downloaded: {size_mb:.1f} MB -> {jar_path}")
    return jar_path


# --------------------------------------------------------------
# Step 2: Export Specctra DSN via KiCad Python
# --------------------------------------------------------------

def export_dsn(pcb_path, dsn_path):
    """Export Specctra DSN from a KiCad PCB using KiCad's bundled Python."""
    print(f"  Input:  {pcb_path}")
    print(f"  Output: {dsn_path}")

    # Build a small inline script for KiCad's Python
    script = textwrap.dedent(f"""\
        import pcbnew
        board = pcbnew.LoadBoard(r"{pcb_path}")
        pcbnew.ExportSpecctraDSN(board, r"{dsn_path}")
        print("DSN export OK")
    """)

    result = subprocess.run(
        [KICAD_PYTHON, "-c", script],
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode != 0:
        print(f"  STDERR: {result.stderr.strip()}")
        print("  ERROR: DSN export failed")
        sys.exit(1)

    if not os.path.isfile(dsn_path):
        print("  ERROR: DSN file was not created")
        sys.exit(1)

    size_kb = os.path.getsize(dsn_path) / 1024
    print(f"  DSN exported: {size_kb:.0f} KB")


# --------------------------------------------------------------
# Step 3: Run FreeRouting CLI
# --------------------------------------------------------------

def run_freerouting(jar_path, dsn_path, ses_path, max_passes=20):
    """Run FreeRouting in headless mode.

    Args:
        jar_path: Path to freerouting JAR
        dsn_path: Input Specctra DSN file
        ses_path: Output Specctra SES file
        max_passes: Maximum routing passes (-mp flag)
    """
    # Check java is available
    java = shutil.which("java")
    if not java:
        print("  ERROR: 'java' not found on PATH")
        print("  Install a JRE/JDK (Java 17+) and ensure 'java' is on PATH")
        sys.exit(1)

    cmd = [
        java, "-jar", jar_path,
        "-de", dsn_path,       # design input (DSN)
        "-do", ses_path,       # design output (SES)
        "-mp", str(max_passes),  # max passes
        "-mt", "4",            # threads
        "-da",                 # detail autorouter after global
    ]

    print(f"  Command: {' '.join(cmd)}")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=3600,
        )

        # Print FreeRouting output
        if result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                print(f"    {line}")
        if result.stderr.strip():
            for line in result.stderr.strip().splitlines():
                print(f"    [err] {line}")

        if result.returncode != 0:
            print(f"  WARNING: FreeRouting exited with code {result.returncode}")
    except subprocess.TimeoutExpired:
        print("  WARNING: FreeRouting timed out after 3600s")

    # Check if SES was produced (FreeRouting writes it during routing,
    # so it may exist even after timeout or non-zero exit)
    if not os.path.isfile(ses_path):
        print("  ERROR: SES file was not created")
        sys.exit(1)

    size_kb = os.path.getsize(ses_path) / 1024
    print(f"  SES output: {size_kb:.0f} KB")


# --------------------------------------------------------------
# Step 4: Import Specctra SES back into KiCad
# --------------------------------------------------------------

def import_ses(pcb_input, ses_path, pcb_output):
    """Import Specctra SES into a copy of the PCB and save as a new file."""
    print(f"  Input PCB:  {pcb_input}")
    print(f"  SES file:   {ses_path}")
    print(f"  Output PCB: {pcb_output}")

    # Copy unrouted board to output path first (import modifies in place)
    shutil.copy2(pcb_input, pcb_output)

    script = textwrap.dedent(f"""\
        import pcbnew
        board = pcbnew.LoadBoard(r"{pcb_output}")
        pcbnew.ImportSpecctraSES(board, r"{ses_path}")
        board.Save(r"{pcb_output}")
        print("SES import OK")
    """)

    result = subprocess.run(
        [KICAD_PYTHON, "-c", script],
        capture_output=True, text=True, timeout=120,
    )

    if result.returncode != 0:
        print(f"  STDERR: {result.stderr.strip()}")
        print("  ERROR: SES import failed")
        sys.exit(1)

    size_kb = os.path.getsize(pcb_output) / 1024
    print(f"  Routed PCB saved: {size_kb:.0f} KB")


# --------------------------------------------------------------
# Step 5: Post-route verification
# --------------------------------------------------------------

def run_post_route_verify():
    """Run verify_pcb.py --post-routing on the routed board."""
    cmd = [sys.executable, VERIFY_SCRIPT, "--post-routing"]
    print(f"  Command: {' '.join(cmd)}")

    result = subprocess.run(cmd, timeout=120)
    return result.returncode


# --------------------------------------------------------------
# Main
# --------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Autoroute RAM prototype PCB using FreeRouting")
    parser.add_argument("--dry-run", action="store_true",
                        help="Export DSN only, don't route")
    parser.add_argument("--passes", type=int, default=20,
                        help="Maximum routing passes (default: 20)")
    parser.add_argument("--jar", default=FREEROUTING_JAR,
                        help="Path to FreeRouting JAR")
    parser.add_argument("--skip-verify", action="store_true",
                        help="Skip post-routing verification")
    args = parser.parse_args()

    print("=" * 60)
    print("RAM Prototype PCB Autorouter")
    print("=" * 60)

    # Check input PCB exists
    if not os.path.isfile(PCB_INPUT):
        print(f"\n  ERROR: {PCB_INPUT} not found")
        print("  Run generate_pcb.py first")
        return 1

    # Step 1: FreeRouting JAR
    print(f"\n--- Step 1: FreeRouting JAR ---")
    jar_path = ensure_freerouting_jar(args.jar)

    # Step 2: Export DSN
    print(f"\n--- Step 2: Export Specctra DSN ---")
    export_dsn(PCB_INPUT, DSN_PATH)

    if args.dry_run:
        print(f"\n--- Dry run complete ---")
        print(f"  DSN file: {DSN_PATH}")
        return 0

    # Step 3: FreeRouting
    print(f"\n--- Step 3: Run FreeRouting (max {args.passes} passes) ---")
    run_freerouting(jar_path, DSN_PATH, SES_PATH, max_passes=args.passes)

    # Step 4: Import SES
    print(f"\n--- Step 4: Import SES -> ram_routed.kicad_pcb ---")
    import_ses(PCB_INPUT, SES_PATH, PCB_ROUTED)

    # Step 5: Verify
    if not args.skip_verify:
        print(f"\n--- Step 5: Post-Routing Verification ---")
        verify_rc = run_post_route_verify()
        if verify_rc != 0:
            print("\n  WARNING: Post-routing verification reported issues")
            print("  Open ram_routed.kicad_pcb in KiCad to inspect")
    else:
        print(f"\n--- Step 5: Verification skipped ---")

    print(f"\n{'=' * 60}")
    print(f"  Routed PCB: {PCB_ROUTED}")
    print(f"{'=' * 60}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
