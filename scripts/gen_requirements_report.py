#!/usr/bin/env python3
"""
gen_requirements_report.py

Builds a requirement traceability report (Markdown and/or Excel) from a
Doxygen-based tracking setup that uses:

    @requirement REQ_XXXXX_x   - on the generated Requirements.hpp (master list)
    @satisfies   REQ_XXXXX_x   - on code that implements a requirement
    @verifies    REQ_XXXXX_x   - on GoogleTest fixture classes (TC_NN_Name)

This does NOT re-implement Doxygen's comment parsing: it reads Doxygen's own
XML output (GENERATE_XML = YES), specifically the consolidated \\xrefitem
list pages that the @satisfies / @verifies aliases produce (satisfies.xml /
verifies.xml, named after the xref key you chose in ALIASES). The master
requirement list is read directly from the generated header, since a
Doxygen comment block with nothing else in it does not get attached to any
documented entity and Doxygen silently discards it -- so it is not visible
in the XML output at all, no matter how @requirement is aliased.

Expected Doxyfile setup (adjust the xref keys if yours differ):

    GENERATE_XML = YES
    ALIASES += satisfies="@xrefitem satisfies \\"Satisfies\\" \\"Satisfies List\\""
    ALIASES += verifies="@xrefitem verifies \\"Verifies\\" \\"Verifies List\\""

Usage:
    python3 gen_requirements_report.py \\
        --xml-dir build/doxygen_out/xml \\
        --requirements-header build/Requirements.hpp \\
        --output-md build/requirements_report.md \\
        --output-xlsx build/requirements_report.xlsx
"""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# Tune these two patterns if your project's naming conventions differ.
# --------------------------------------------------------------------------

# Requirement IDs, e.g. REQ_00001_a
REQUIREMENT_ID_RE = re.compile(r"REQ_\d+_[A-Za-z]+")

# Test fixture class naming convention, e.g. TC_01_ServiceInit
TEST_CASE_RE = re.compile(r"^TC_\d+_")


@dataclass
class RequirementRow:
    req_id: str
    description: str = ""
    implemented: bool = False
    tested: bool = False
    test_names: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 1. Master requirement list: parsed directly from the generated header.
# --------------------------------------------------------------------------

_BLOCK_RE = re.compile(r"/\*\*(.*?)\*/", re.DOTALL)
_REQ_TAG_RE = re.compile(r"@requirement\s+(" + REQUIREMENT_ID_RE.pattern + r")")
_LEADING_STAR_RE = re.compile(r"^\*\s?")


def parse_requirements_header(path: Path) -> dict[str, str]:
    """Return an ordered {req_id: description} dict from the generated header.

    Looks for Doxygen comment blocks of the form:
        /** @requirement REQ_00001_a
          * Free text description, possibly spanning
          * several lines.
          */
    """
    text = path.read_text(encoding="utf-8")
    requirements: dict[str, str] = {}

    for block in _BLOCK_RE.finditer(text):
        content = block.group(1)
        m = _REQ_TAG_RE.search(content)
        if not m:
            continue
        req_id = m.group(1)

        lines = []
        for raw_line in content[m.end():].splitlines():
            line = _LEADING_STAR_RE.sub("", raw_line.strip())
            if line:
                lines.append(line)
        description = " ".join(lines).strip()

        if req_id in requirements:
            print(f"warning: '{req_id}' is documented more than once in "
                  f"{path}; keeping the first occurrence", file=sys.stderr)
            continue
        requirements[req_id] = description

    return requirements


# --------------------------------------------------------------------------
# 2. @satisfies / @verifies: parsed from Doxygen's own XML output.
# --------------------------------------------------------------------------

def _iter_xref_list_page(xml_dir: Path, page_name: str):
    """Yield (entity_name, [req_ids, ...]) from a Doxygen \\xrefitem list page.

    Doxygen renders each \\xrefitem key into a consolidated page named after
    the key (page_name.xml here), containing a <variablelist> that
    alternates a <varlistentry> (which entity: class/member/...) with a
    <listitem> (the xrefitem text -- our requirement ID(s)). See a sample
    verifies.xml for the exact shape this is walking.
    """
    page_path = xml_dir / f"{page_name}.xml"
    if not page_path.exists():
        # Nothing was ever tagged with this alias -- not an error.
        return

    root = ET.parse(page_path).getroot()
    for varlist in root.iter("variablelist"):
        current_entity = None
        for node in varlist:
            if node.tag == "varlistentry":
                term = node.find("term")
                ref = term.find("ref") if term is not None else None
                if ref is not None and ref.text:
                    current_entity = ref.text.strip()
                elif term is not None:
                    current_entity = "".join(term.itertext()).strip()
                else:
                    current_entity = None
            elif node.tag == "listitem":
                text = "".join(node.itertext())
                req_ids = REQUIREMENT_ID_RE.findall(text)
                if req_ids:
                    yield current_entity, req_ids


def collect_satisfies(xml_dir: Path, xref_key: str) -> set[str]:
    implemented: set[str] = set()
    for _entity, req_ids in _iter_xref_list_page(xml_dir, xref_key):
        implemented.update(req_ids)
    return implemented


def collect_verifies(xml_dir: Path, xref_key: str) -> dict[str, list[str]]:
    """Return {req_id: sorted [test_class_name, ...]}."""
    tested: dict[str, set[str]] = {}
    for entity, req_ids in _iter_xref_list_page(xml_dir, xref_key):
        entity_name = entity or "?"
        if entity and not TEST_CASE_RE.match(entity):
            print(f"warning: @verifies found on '{entity}', which doesn't "
                  f"look like a test class (expected TC_NN_Name)",
                  file=sys.stderr)
        for req_id in req_ids:
            tested.setdefault(req_id, set()).add(entity_name)
    return {req_id: sorted(names) for req_id, names in tested.items()}


# --------------------------------------------------------------------------
# 3. Cross-reference into report rows.
# --------------------------------------------------------------------------

def _natural_key(req_id: str):
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", req_id)]


def build_rows(
    requirements: dict[str, str],
    implemented: set[str],
    tested: dict[str, list[str]],
) -> list[RequirementRow]:
    known_ids = set(requirements)

    # Flag @satisfies/@verifies tags that reference a requirement ID which
    # doesn't exist in the master list -- almost always a typo.
    unknown = (implemented | set(tested)) - known_ids
    for req_id in sorted(unknown):
        print(f"warning: '{req_id}' is referenced by @satisfies/@verifies "
              f"but is not declared in the requirements header",
              file=sys.stderr)

    rows = []
    for req_id in sorted(requirements, key=_natural_key):
        rows.append(RequirementRow(
            req_id=req_id,
            description=requirements[req_id],
            implemented=req_id in implemented,
            tested=req_id in tested,
            test_names=tested.get(req_id, []),
        ))
    return rows


# --------------------------------------------------------------------------
# 4. Output writers.
# --------------------------------------------------------------------------

def _summary(rows: list[RequirementRow]) -> tuple[int, int, int]:
    total = len(rows)
    n_impl = sum(1 for r in rows if r.implemented)
    n_test = sum(1 for r in rows if r.tested)
    return total, n_impl, n_test


def write_markdown(rows: list[RequirementRow], out_path: Path, with_description: bool) -> None:
    total, n_impl, n_test = _summary(rows)

    lines = ["# Requirements traceability report", ""]
    if total:
        lines.append(
            f"**Implemented:** {n_impl}/{total} ({100 * n_impl / total:.0f}%)  \n"
            f"**Tested:** {n_test}/{total} ({100 * n_test / total:.0f}%)"
        )
        lines.append("")

    header = ["Requirement"] + (["Description"] if with_description else [])
    header += ["Implemented", "Tested", "Test Name"]
    align = [":--"] + (([":--"] if with_description else [])) + [":-:", ":-:", ":--"]

    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(align) + " |")

    for row in rows:
        cells = [row.req_id]
        if with_description:
            cells.append(row.description.replace("|", "\\|"))
        cells.append("✔" if row.implemented else "✘")
        cells.append("✔" if row.tested else "✘")
        cells.append(", ".join(row.test_names))
        lines.append("| " + " | ".join(cells) + " |")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_excel(rows: list[RequirementRow], out_path: Path, with_description: bool) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("error: --output-xlsx requires the 'openpyxl' package "
              "(pip install openpyxl)", file=sys.stderr)
        sys.exit(1)

    total, n_impl, n_test = _summary(rows)

    wb = Workbook()
    ws = wb.active
    ws.title = "Requirements"

    header = ["Requirement"] + (["Description"] if with_description else [])
    header += ["Implemented", "Tested", "Test Name"]
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")

    green = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
    red = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    impl_col = 2 + with_description
    test_col = 3 + with_description

    for row in rows:
        cells = [row.req_id]
        if with_description:
            cells.append(row.description)
        cells.append("Yes" if row.implemented else "No")
        cells.append("Yes" if row.tested else "No")
        cells.append(", ".join(row.test_names))
        ws.append(cells)

        r = ws.max_row
        ws.cell(row=r, column=impl_col).fill = green if row.implemented else red
        ws.cell(row=r, column=test_col).fill = green if row.tested else red
        for c in (impl_col, test_col):
            ws.cell(row=r, column=c).alignment = Alignment(horizontal="center")

    for i in range(1, len(header) + 1):
        col_letter = get_column_letter(i)
        cell_lengths = [len(str(header[i - 1]))]
        cell_lengths += [
            len(str(ws.cell(row=r, column=i).value or ""))
            for r in range(2, ws.max_row + 1)
        ]
        ws.column_dimensions[col_letter].width = min(max(cell_lengths) + 2, 60)
    ws.freeze_panes = "A2"

    if total:
        ws2 = wb.create_sheet("Summary")
        ws2.append(["Metric", "Count", "Total", "Percent"])
        for cell in ws2[1]:
            cell.font = Font(bold=True)
        ws2.append(["Implemented", n_impl, total, f"{100 * n_impl / total:.0f}%"])
        ws2.append(["Tested", n_test, total, f"{100 * n_test / total:.0f}%"])
        for i, width in enumerate([16, 10, 10, 10], start=1):
            ws2.column_dimensions[get_column_letter(i)].width = width

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


# --------------------------------------------------------------------------
# 5. CLI.
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a requirements traceability report from Doxygen XML output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--xml-dir", required=True, type=Path,
                         help="Doxygen XML output directory (GENERATE_XML = YES)")
    parser.add_argument("--requirements-header", required=True, type=Path,
                         help="Path to the generated Requirements.hpp (or similar)")
    parser.add_argument("--output-md", type=Path, help="Markdown report output path")
    parser.add_argument("--output-xlsx", type=Path, help="Excel report output path")
    parser.add_argument("--with-description", action="store_true",
                         help="Include the requirement description as a column")
    parser.add_argument("--fail-under", type=float, default=None, metavar="PCT",
                         help="Exit with a non-zero status if implemented+tested "
                              "coverage (average of the two) drops below PCT")
    parser.add_argument("--satisfies-key", default="satisfies", metavar="KEY",
                         help="xrefitem key used for @satisfies in your ALIASES "
                              "(the first \\xrefitem argument -- default: satisfies)")
    parser.add_argument("--verifies-key", default="verifies", metavar="KEY",
                         help="xrefitem key used for @verifies in your ALIASES "
                              "(the first \\xrefitem argument -- default: verifies)")
    args = parser.parse_args()

    if not args.output_md and not args.output_xlsx:
        parser.error("pass at least one of --output-md / --output-xlsx")
    if not args.requirements_header.is_file():
        parser.error(f"requirements header not found: {args.requirements_header}")
    if not args.xml_dir.is_dir():
        parser.error(f"Doxygen XML directory not found: {args.xml_dir}")

    requirements = parse_requirements_header(args.requirements_header)
    if not requirements:
        print(f"warning: no '@requirement REQ_...' blocks found in "
              f"{args.requirements_header}", file=sys.stderr)

    implemented = collect_satisfies(args.xml_dir, args.satisfies_key)
    tested = collect_verifies(args.xml_dir, args.verifies_key)
    rows = build_rows(requirements, implemented, tested)

    if args.output_md:
        write_markdown(rows, args.output_md, args.with_description)
        print(f"Wrote {args.output_md}")
    if args.output_xlsx:
        write_excel(rows, args.output_xlsx, args.with_description)
        print(f"Wrote {args.output_xlsx}")

    total, n_impl, n_test = _summary(rows)
    if total:
        pct_impl = 100 * n_impl / total
        pct_test = 100 * n_test / total
        print(f"Implemented: {n_impl}/{total} ({pct_impl:.0f}%)")
        print(f"Tested:      {n_test}/{total} ({pct_test:.0f}%)")

        if args.fail_under is not None:
            avg = (pct_impl + pct_test) / 2
            if avg < args.fail_under:
                print(f"error: coverage {avg:.0f}% is below --fail-under "
                      f"{args.fail_under:.0f}%", file=sys.stderr)
                sys.exit(1)


if __name__ == "__main__":
    main()
