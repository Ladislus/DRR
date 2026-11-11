#!/usr/bin/env python3
"""
gen_requirements_report.py

Builds a requirement traceability report (Markdown and/or Excel) from
Doxygen's native requirement-tracking feature (Doxygen >= 1.16):

    @requirement <id> [title]   - defines a requirement (its own comment block)
    @satisfies   <id> [text]    - on code that implements a requirement
    @verifies    <id> [text]    - on GoogleTest fixture classes (TC_NN_Name)

These are real Doxygen commands as of 1.16.0 -- no ALIASES/xrefitem setup
needed. This script reads Doxygen's XML output (GENERATE_XML = YES):

  - xml/index.xml lists every compound, including one <compound kind=
    "requirement"> per requirement (refid "requirement_<id>", name "<id>").
  - Each requirement also gets its own xml/requirement_<id>.xml with its
    <title> and <detaileddescription>.
  - Every other compound (classes, files, ...) that carries @satisfies or
    @verifies embeds a <satisfies> or <verifies> element (at compound or
    member level) containing one <requirement refid="requirement_<id>">
    child per reference -- that's what ties implementation/tests back to
    a requirement ID.

On top of that native feature, this script recognizes a local convention
for partially-implemented requirements, written into the @satisfies short
text (Doxygen itself doesn't understand this -- it's just free text to it):

    @satisfies REQ_00001_a !partial(3) Implement the database connection part

  - "!partial(3)" means REQ_00001_a needs 3 tagged @satisfies occurrences
    total to count as fully implemented. Every occurrence -- whether or not
    it repeats the !partial(...) marker -- counts as one fragment.
  - A requirement with no !partial(...) anywhere defaults to needing 1
    fragment (today's plain behaviour: any @satisfies makes it "done").
  - If different @satisfies occurrences for the same requirement declare
    different totals (e.g. one says !partial(3), another !partial(4)),
    that's treated as an authoring error and the script exits non-zero.

Usage:
    python3 gen_requirements_report.py \\
        --xml-dir build/doxygen_out/xml \\
        --output-md build/requirements_report.md \\
        --output-xlsx build/requirements_report.xlsx
"""

from __future__ import annotations

import argparse

import xml.etree.ElementTree as ET

from sys         import exit as sexit
from re          import compile as regex_compile, split as regex_split
from dataclasses import dataclass, field
from pathlib     import Path

from rich.console import Console

CONSOLE = Console()

from custom_logging import setup_logging

LOGGER = setup_logging( 'RequirementGenerator' )

# Test fixture class naming convention, e.g. TC_01_ServiceInit -- only used
# to sanity-check where @verifies shows up, never to find requirement IDs.
TEST_CASE_RE = regex_compile(r"^TC_\d+_")

REQUIREMENT_KIND = "requirement"
REQUIREMENT_REFID_PREFIX = "requirement_"

# "!partial(3) rest of the description" -- must be the very first thing in
# the @satisfies short text (i.e. right after the requirement ID).
PARTIAL_MARKER_RE = regex_compile(r"^!partial\(\s*(\d+)\s*\)\s*")
PARTIAL_MARKER_PREFIX = "!partial"


@dataclass
class SatisfiesFragment:
    location: str            # enclosing compound name (class/file/...)
    declared_total: int | None  # the N in !partial(N), if this occurrence gave one
    text: str                # short text with the marker stripped off


@dataclass
class ImplementationStatus:
    found: int = 0
    required: int = 1
    fragments: list[SatisfiesFragment] = field(default_factory=list)

    @property
    def is_partial_tracked(self) -> bool:
        return self.required > 1

    @property
    def complete(self) -> bool:
        return self.found >= self.required

    @property
    def partial(self) -> bool:
        return 0 < self.found < self.required


@dataclass
class RequirementRow:
    req_id: str
    title: str = ""
    description: str = ""
    impl: ImplementationStatus = field(default_factory=ImplementationStatus)
    tested: bool = False
    test_names: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 1. Enumerate every compound Doxygen knows about, and every requirement.
# --------------------------------------------------------------------------

def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def load_index(xml_dir: Path):
    """Return (all_compound_refids, {req_id: refid} for kind="requirement")."""
    index_path = xml_dir / "index.xml"
    if not index_path.is_file():
        sexit(f"error: {index_path} not found -- did you set "
                  f"GENERATE_XML = YES (and build) before running this?")

    root = ET.parse(index_path).getroot()
    all_refids = []
    requirements: dict[str, str] = {}  # req_id -> refid

    for compound in root.findall("compound"):
        refid = compound.get("refid", "")
        kind = compound.get("kind", "")
        all_refids.append(refid)
        if kind == REQUIREMENT_KIND:
            name = _text(compound.find("name")) or refid[len(REQUIREMENT_REFID_PREFIX):]
            requirements[name] = refid

    return all_refids, requirements


def load_requirement_details(xml_dir: Path, refid: str) -> tuple[str, str]:
    """Return (title, description) for one requirement, reading its own XML file.

    Requirements imported from an external tag file (Polarion/DOORS/...) have
    no local XML file of their own -- return empty strings for those rather
    than failing.
    """
    path = xml_dir / f"{refid}.xml"
    if not path.is_file():
        return "", ""
    root = ET.parse(path).getroot()
    compounddef = root.find("compounddef")
    if compounddef is None:
        return "", ""
    title = _text(compounddef.find("title"))
    description = _text(compounddef.find("detaileddescription"))
    return title, description


# --------------------------------------------------------------------------
# 2. @satisfies / @verifies: walk every other compound's own XML.
# --------------------------------------------------------------------------

def _requirement_id_from_refid(refid: str, requirements_by_refid: dict[str, str]) -> str:
    if refid in requirements_by_refid:
        return requirements_by_refid[refid]
    # Fall back to stripping the conventional prefix (e.g. an external /
    # tag-file requirement not otherwise present in this project's index).
    if refid.startswith(REQUIREMENT_REFID_PREFIX):
        return refid[len(REQUIREMENT_REFID_PREFIX):]
    return refid


def _parse_satisfies_text(raw_text: str, req_id: str, location: str) -> tuple[int | None, str]:
    """Split a @satisfies short text into (declared_total, remaining_text).

    Exits with a clear error on a recognizable-but-malformed !partial(...)
    marker (e.g. a typo'd argument), rather than silently ignoring it.
    """
    stripped = raw_text.strip()
    m = PARTIAL_MARKER_RE.match(stripped)
    if m:
        return int(m.group(1)), stripped[m.end():].strip()
    if stripped.startswith(PARTIAL_MARKER_PREFIX):
        sexit(f"error: {location}: malformed !partial(...) marker on "
                  f"'{req_id}': {stripped!r}")
    return None, stripped


def collect_satisfies_and_verifies(xml_dir: Path, all_refids: list[str],
                                    requirements_by_refid: dict[str, str]):
    """Return ({req_id: [SatisfiesFragment, ...]}, {req_id: set[test_name]})."""
    fragments: dict[str, list[SatisfiesFragment]] = {}
    tested: dict[str, set[str]] = {}

    for refid in all_refids:
        if refid.startswith(REQUIREMENT_REFID_PREFIX):
            continue  # a requirement's own file never links back this way
        path = xml_dir / f"{refid}.xml"
        if not path.is_file():
            continue

        root = ET.parse(path).getroot()
        compounddef = root.find("compounddef")
        if compounddef is None:
            continue
        compound_name = _text(compounddef.find("compoundname"))

        for satisfies_el in compounddef.iter("satisfies"):
            for req_el in satisfies_el.findall("requirement"):
                req_id = _requirement_id_from_refid(req_el.get("refid", ""), requirements_by_refid)
                declared_total, frag_text = _parse_satisfies_text(_text(req_el), req_id, compound_name)
                fragments.setdefault(req_id, []).append(
                    SatisfiesFragment(location=compound_name, declared_total=declared_total, text=frag_text)
                )

        for verifies_el in compounddef.iter("verifies"):
            for req_el in verifies_el.findall("requirement"):
                req_id = _requirement_id_from_refid(req_el.get("refid", ""), requirements_by_refid)
                if not TEST_CASE_RE.match(compound_name):
                    LOGGER.warning( f'@verifies found on "{ compound_name }", which does not look like a test class ( expected TC_NN_Name )' )
                tested.setdefault(req_id, set()).add(compound_name)

    return fragments, {req_id: sorted(names) for req_id, names in tested.items()}


def resolve_implementation_status(req_id: str, frags: list[SatisfiesFragment]) -> ImplementationStatus:
    """Turn the raw @satisfies fragments for one requirement into a status.

    Exits with an error if fragments disagree on the total (!partial(N))
    they're counting towards.
    """
    declared = {f.declared_total for f in frags if f.declared_total is not None}
    if len(declared) > 1:
        detail = ", ".join(f"{f.location}=!partial({f.declared_total})"
                            for f in frags if f.declared_total is not None)
        sexit(f"error: '{req_id}' has mismatching !partial(...) totals: {detail}")

    required = declared.pop() if declared else 1
    found = len(frags)
    if found > required:
        LOGGER.warning( f'"{ req_id }" has { found } @satisfies occurrences but declares !partial( { required } ) -- treating as fully implemented' )

    return ImplementationStatus(found=found, required=required, fragments=frags)


# --------------------------------------------------------------------------
# 3. Cross-reference into report rows.
# --------------------------------------------------------------------------

def _natural_key(req_id: str):
    return [int(tok) if tok.isdigit() else tok for tok in regex_split(r"(\d+)", req_id)]


def build_rows(xml_dir: Path, requirements_by_id: dict[str, str],
               fragments: dict[str, list[SatisfiesFragment]],
               tested: dict[str, list[str]]) -> list[RequirementRow]:
    known_ids = set(requirements_by_id)

    unknown = (set(fragments) | set(tested)) - known_ids
    for req_id in sorted(unknown):
        LOGGER.warning( f'"{ req_id }" is referenced by @satisfies/@verifies but has no matching @requirement' )

    rows = []
    for req_id in sorted(requirements_by_id, key=_natural_key):
        title, description = load_requirement_details(xml_dir, requirements_by_id[req_id])
        impl = resolve_implementation_status(req_id, fragments.get(req_id, []))
        rows.append(RequirementRow(
            req_id=req_id,
            title=title,
            description=description,
            impl=impl,
            tested=req_id in tested,
            test_names=tested.get(req_id, []),
        ))
    return rows


# --------------------------------------------------------------------------
# 4. Output writers.
# --------------------------------------------------------------------------

def _summary(rows: list[RequirementRow]) -> tuple[int, int, int, int]:
    total = len(rows)
    n_impl = sum(1 for r in rows if r.impl.complete)
    n_partial = sum(1 for r in rows if r.impl.partial)
    n_test = sum(1 for r in rows if r.tested)
    return total, n_impl, n_partial, n_test


def _description_column(row: RequirementRow) -> str:
    if row.title and row.description:
        return f"{row.title} -- {row.description}"
    return row.title or row.description


def _implemented_cell(row: RequirementRow, symbols: tuple[str, str, str]) -> str:
    """symbols = (complete, partial, none), rendered plain unless the
    requirement actually uses !partial(...) tracking, in which case the
    found/required fraction is appended."""
    complete_sym, partial_sym, none_sym = symbols
    if not row.impl.is_partial_tracked:
        return complete_sym if row.impl.complete else none_sym
    if row.impl.complete:
        sym = complete_sym
    elif row.impl.partial:
        sym = partial_sym
    else:
        sym = none_sym
    return f"{sym} ({row.impl.found}/{row.impl.required})"


def _fragment_notes(rows: list[RequirementRow]) -> list[RequirementRow]:
    return [r for r in rows if r.impl.is_partial_tracked and r.impl.fragments]


def write_markdown(rows: list[RequirementRow], out_path: Path, with_description: bool) -> None:
    total, n_impl, n_partial, n_test = _summary(rows)

    lines = ["# Requirements traceability report", ""]
    if total:
        summary = (
            f"**Implemented:** {n_impl}/{total} ({100 * n_impl / total:.0f}%)  \n"
            f"**Tested:** {n_test}/{total} ({100 * n_test / total:.0f}%)"
        )
        if n_partial:
            summary += f"  \n**Partially implemented:** {n_partial}"
        lines.append(summary)
        lines.append("")

    header = ["Requirement"] + (["Description"] if with_description else [])
    header += ["Implemented", "Tested", "Test Name"]
    align = [":--"] + (([":--"] if with_description else [])) + [":-:", ":-:", ":--"]

    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(align) + " |")

    for row in rows:
        cells = [row.req_id]
        if with_description:
            cells.append(_description_column(row).replace("|", "\\|"))
        cells.append(_implemented_cell(row, ("✔", "◐", "✘")))
        cells.append("✔" if row.tested else "✘")
        cells.append(", ".join(row.test_names))
        lines.append("| " + " | ".join(cells) + " |")

    notes_rows = _fragment_notes(rows)
    if notes_rows:
        lines += ["", "## Partial implementation details", ""]
        for row in notes_rows:
            lines.append(f"**{row.req_id}** ({row.impl.found}/{row.impl.required} fragments):")
            for frag in row.impl.fragments:
                text = f": {frag.text}" if frag.text else ""
                lines.append(f"- `{frag.location}`{text}")
            lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_excel(rows: list[RequirementRow], out_path: Path, with_description: bool) -> None:
    try:
        from openpyxl import Workbook
        from openpyxl.comments import Comment
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        LOGGER.critical( '--output-xlsx requires the "openpyxl" package pip install openpyxl)' )
        sexit(1)

    total, n_impl, n_partial, n_test = _summary(rows)

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
    yellow = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    red = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
    impl_col = 3 if with_description else 2
    test_col = 4 if with_description else 3

    for row in rows:
        cells = [row.req_id]
        if with_description:
            cells.append(_description_column(row))
        cells.append(_implemented_cell(row, ("Yes", "Partial", "No")))
        cells.append("Yes" if row.tested else "No")
        cells.append(", ".join(row.test_names))
        ws.append(cells)

        r = ws.max_row
        impl_fill = green if row.impl.complete else (yellow if row.impl.partial else red)
        ws.cell(row=r, column=impl_col).fill = impl_fill
        ws.cell(row=r, column=test_col).fill = green if row.tested else red
        for c in (impl_col, test_col):
            ws.cell(row=r, column=c).alignment = Alignment(horizontal="center")

        if row.impl.is_partial_tracked and row.impl.fragments:
            note = "\n".join(
                f"{f.location}: {f.text}" if f.text else f.location
                for f in row.impl.fragments
            )
            ws.cell(row=r, column=impl_col).comment = Comment(note, "gen_requirements_report")

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
        ws2.append(["Partially implemented", n_partial, total, f"{100 * n_partial / total:.0f}%"])
        ws2.append(["Tested", n_test, total, f"{100 * n_test / total:.0f}%"])
        for i, width in enumerate([20, 10, 10, 10], start=1):
            ws2.column_dimensions[get_column_letter(i)].width = width

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


# --------------------------------------------------------------------------
# 5. CLI.
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a requirements traceability report from Doxygen's "
                     "native @requirement/@satisfies/@verifies XML output (Doxygen >= 1.16).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--xml-dir", required=True, type=Path,
                         help="Doxygen XML output directory (GENERATE_XML = YES)")
    parser.add_argument("--output-md", type=Path, help="Markdown report output path")
    parser.add_argument("--output-xlsx", type=Path, help="Excel report output path")
    parser.add_argument("--with-description", action="store_true",
                         help="Include the requirement title/description as a column")
    parser.add_argument("--fail-under", type=float, default=None, metavar="PCT",
                         help="Exit with a non-zero status if implemented+tested "
                              "coverage (average of the two, fully-implemented only) "
                              "drops below PCT")
    args = parser.parse_args()

    if not args.output_md and not args.output_xlsx:
        parser.error("pass at least one of --output-md / --output-xlsx")
    if not args.xml_dir.is_dir():
        parser.error(f"Doxygen XML directory not found: {args.xml_dir}")

    all_refids, requirements_by_id = load_index(args.xml_dir)
    if not requirements_by_id:
        LOGGER.warning( f'no @requirement compounds found in { args.xml_dir }/index.xml -- is GENERATE_REQUIREMENTS enabled (it is YES by default) and did you rebuild after adding @requirement blocks?' )

    fragments, tested = collect_satisfies_and_verifies(
        args.xml_dir, all_refids, requirements_by_id)
    rows = build_rows(args.xml_dir, requirements_by_id, fragments, tested)

    if args.output_md:
        with CONSOLE.status( '[bold magenta]Generating markdown...' ):
            write_markdown(rows, args.output_md, args.with_description)
        LOGGER.info( f'Wrote "{ args.output_md }"' )
    if args.output_xlsx:
        with CONSOLE.status( '[bold magenta]Generating xlsx...' ):
            write_excel(rows, args.output_xlsx, args.with_description)
        LOGGER.info( f'Wrote "{ args.output_xlsx }"' )

    total, n_impl, n_partial, n_test = _summary(rows)
    if total:
        pct_impl = 100 * n_impl / total
        pct_test = 100 * n_test / total
        LOGGER.info( f'Implemented: { n_impl } / { total } ( {pct_impl:.0f}% )' + ( f', partially: { n_partial }' if n_partial else '' ) )
        LOGGER.info( f'Tested:      { n_test } / { total } ( {pct_test:.0f}% )' )

        if args.fail_under is not None:
            avg = (pct_impl + pct_test) / 2
            if avg < args.fail_under:
                LOGGER.critical( f'coverage {avg:.0f}% is below --fail-under {args.fail_under:.0f}%' )
                sexit(1)


if __name__ == "__main__":
    main()
