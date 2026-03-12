#!/usr/bin/env python3
"""Parse PDF files (e.g., TI datasheets) and extract text, tables, or pin mappings.

Usage:
    # Dump all text from a PDF
    python parse_pdf.py datasheet.pdf

    # Dump text from specific pages (1-indexed)
    python parse_pdf.py datasheet.pdf --pages 1-3

    # Search for text pattern
    python parse_pdf.py datasheet.pdf --search "pin"

    # Extract pin-to-ball table (common in TI DSBGA datasheets)
    python parse_pdf.py datasheet.pdf --pins

    # Show page count and metadata
    python parse_pdf.py datasheet.pdf --info
"""

import argparse
import re
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF not installed. Run: pip install PyMuPDF", file=sys.stderr)
    sys.exit(1)


def parse_page_range(spec: str, max_page: int) -> list[int]:
    """Parse a page range spec like '1-3,5,7-9' into 0-indexed page numbers."""
    pages = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            start, end = part.split("-", 1)
            start = max(1, int(start))
            end = min(max_page, int(end))
            pages.extend(range(start - 1, end))
        else:
            p = int(part)
            if 1 <= p <= max_page:
                pages.append(p - 1)
    return sorted(set(pages))


def get_pages(doc: fitz.Document, page_spec: str | None) -> list[int]:
    """Return list of 0-indexed page numbers to process."""
    if page_spec:
        return parse_page_range(page_spec, len(doc))
    return list(range(len(doc)))


def dump_text(doc: fitz.Document, pages: list[int]):
    """Print extracted text from specified pages."""
    for i in pages:
        page = doc[i]
        text = page.get_text()
        print(f"{'='*60}")
        print(f"PAGE {i + 1}")
        print(f"{'='*60}")
        print(text)


def search_text(doc: fitz.Document, pages: list[int], pattern: str):
    """Search for a pattern across pages and show matching lines with context."""
    pat = re.compile(pattern, re.IGNORECASE)
    found = 0
    for i in pages:
        page = doc[i]
        text = page.get_text()
        lines = text.splitlines()
        for line_num, line in enumerate(lines):
            if pat.search(line):
                found += 1
                # Show 1 line of context before and after
                start = max(0, line_num - 1)
                end = min(len(lines), line_num + 2)
                print(f"--- Page {i + 1}, line {line_num + 1} ---")
                for j in range(start, end):
                    marker = ">>>" if j == line_num else "   "
                    print(f"  {marker} {lines[j]}")
    if found == 0:
        print(f"No matches for '{pattern}'")
    else:
        print(f"\n{found} match(es) found.")


def extract_pin_table(doc: fitz.Document, pages: list[int]):
    """Extract pin-to-ball mapping tables from TI DSBGA datasheets.

    Looks for patterns like:
        PIN NAME  NO.  DSBGA (YZP)
        A         1    A1
        B         2    B1
    or tabular text with ball names (A1, B2, C1, etc.) and pin numbers.
    """
    # Patterns for ball names and pin rows
    ball_pat = re.compile(r'\b([A-D][1-4])\b')
    pin_row_pat = re.compile(
        r'(\w[\w/\s]*?)\s+'      # pin name (e.g., "1A", "GND", "VCC")
        r'(\d+)\s+'              # pin number
        r'([A-D][1-4])',         # ball name
        re.IGNORECASE
    )
    # Alternate: ball name first, then pin number
    ball_first_pat = re.compile(
        r'([A-D][1-4])\s+'      # ball name
        r'(\d+)\s+'              # pin number
        r'(\w[\w/\s]*)',         # pin name
        re.IGNORECASE
    )

    mappings = []
    for i in pages:
        page = doc[i]
        text = page.get_text()
        lines = text.splitlines()

        for line in lines:
            # Try pin_name, number, ball
            m = pin_row_pat.search(line)
            if m:
                name, num, ball = m.group(1).strip(), m.group(2), m.group(3).upper()
                mappings.append((int(num), ball, name, i + 1))
                continue

            # Try ball, number, pin_name
            m = ball_first_pat.search(line)
            if m:
                ball, num, name = m.group(1).upper(), m.group(2), m.group(3).strip()
                mappings.append((int(num), ball, name, i + 1))

    if not mappings:
        # Fallback: just find any lines with ball names near numbers
        print("No structured pin table found. Lines containing ball names:")
        for i in pages:
            page = doc[i]
            text = page.get_text()
            for line in text.splitlines():
                if ball_pat.search(line) and re.search(r'\d', line):
                    print(f"  Page {i + 1}: {line.strip()}")
        return

    # Deduplicate and sort by pin number
    seen = set()
    unique = []
    for num, ball, name, pg in mappings:
        key = (num, ball)
        if key not in seen:
            seen.add(key)
            unique.append((num, ball, name, pg))
    unique.sort(key=lambda x: x[0])

    print("Pin-to-Ball Mapping:")
    print(f"  {'Pin#':<6} {'Ball':<6} {'Name':<20} {'Page'}")
    print(f"  {'----':<6} {'----':<6} {'----':<20} {'----'}")
    for num, ball, name, pg in unique:
        print(f"  {num:<6} {ball:<6} {name:<20} {pg}")

    # Print as Python dict
    print("\nAs Python dict (pin_to_ball):")
    items = ", ".join(f'"{num}": "{ball}"' for num, ball, _, _ in unique)
    print(f"  {{{items}}}")


def show_info(doc: fitz.Document):
    """Show PDF metadata and page count."""
    meta = doc.metadata
    print(f"Pages: {len(doc)}")
    if meta:
        for key in ("title", "author", "subject", "creator", "producer"):
            val = meta.get(key, "")
            if val:
                print(f"{key.capitalize()}: {val}")


def main():
    parser = argparse.ArgumentParser(
        description="Parse PDF files (TI datasheets, etc.)")
    parser.add_argument("pdf", help="Path to PDF file")
    parser.add_argument("--pages", "-p",
                        help="Page range (1-indexed), e.g. '1-3,5'")
    parser.add_argument("--search", "-s",
                        help="Search for regex pattern")
    parser.add_argument("--pins", action="store_true",
                        help="Extract pin-to-ball mapping table")
    parser.add_argument("--info", "-i", action="store_true",
                        help="Show PDF metadata and page count")
    args = parser.parse_args()

    doc = fitz.open(args.pdf)
    pages = get_pages(doc, args.pages)

    if args.info:
        show_info(doc)
    elif args.search:
        search_text(doc, pages, args.search)
    elif args.pins:
        extract_pin_table(doc, pages)
    else:
        dump_text(doc, pages)

    doc.close()


if __name__ == "__main__":
    main()
