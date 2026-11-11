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
for requirements that are implemented / verified in several fragments,
written into the @satisfies / @verifies short text (Doxygen itself doesn't
understand this -- it's just free text to it):

    @satisfies REQ_00001_a !partial( 3 ) Implement the database connection part
    @verifies  REQ_00001_a !partial( 2 ) Covers the happy path

  - "!partial( 3 )" means REQ_00001_a needs 3 tagged occurrences of that
    command total to count as complete. Every occurrence -- whether or not
    it repeats the !partial( x ) marker -- counts as one fragment.
  - With no !partial( x ) anywhere, the total implicitly equals the number
    of occurrences (two plain @satisfies == !partial( 2 ), both found), so
    the requirement is simply complete and renders as a plain check mark:
    only explicit !partial( x ) shows an X/Y fraction. No occurrence at all
    means "not done".
  - If different occurrences for the same requirement declare different
    totals (e.g. !partial( 3 ) and !partial( 4 )), that's treated as an
    authoring error and the script exits non-zero. Same if an explicit
    !partial( N ) is declared but more than N occurrences exist (fewer is
    fine: that's just a partially done requirement).
  - Applies independently to @satisfies (Implemented) and @verifies (Tested).

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
from functools   import lru_cache
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
# the @satisfies / @verifies short text (i.e. right after the requirement ID).
PARTIAL_MARKER_RE = regex_compile(r"^!partial\(\s*(\d+)\s*\)\s*")
PARTIAL_MARKER_PREFIX = "!partial"


@dataclass
class Fragment:
    location: str            # enclosing compound name (class/file/test class/...)
    declared_total: int | None  # the N in !partial(N), if this occurrence gave one
    text: str                # short text with the marker stripped off


@dataclass
class CoverageStatus:
    """Fragment accounting for one requirement and one command (@satisfies
    or @verifies)."""
    found: int = 0
    required: int = 1
    fragments: list[Fragment] = field(default_factory=list)
    explicit: bool = False   # True only if some occurrence declared !partial( N )

    @property
    def is_partial_tracked(self) -> bool:
        # Implicit totals (plain repeated tags) render like any other
        # complete requirement: no X/Y fraction, no details entry.
        return self.explicit and self.required > 1

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
    impl: CoverageStatus = field(default_factory=CoverageStatus)
    verif: CoverageStatus = field(default_factory=CoverageStatus)

    @property
    def test_names(self) -> list[str]:
        return sorted({f.location for f in self.verif.fragments})


# --------------------------------------------------------------------------
# 1. Enumerate every compound Doxygen knows about, and every requirement.
# --------------------------------------------------------------------------

def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def _refid_basename(refid: str) -> str:
    """Drop the directory part Doxygen prepends to refids when CREATE_SUBDIRS
    is enabled ("d3/d02/requirement_REQ_1" -> "requirement_REQ_1")."""
    return refid.rsplit("/", 1)[-1]


@lru_cache(maxsize=None)
def _xml_files_by_stem(xml_dir: Path) -> dict[str, Path]:
    return {p.stem: p for p in xml_dir.rglob("*.xml")}


def _resolve_xml(xml_dir: Path, refid: str) -> Path | None:
    """Locate the XML file for a refid, with or without CREATE_SUBDIRS."""
    direct = xml_dir / f"{refid}.xml"
    if direct.is_file():
        return direct
    return _xml_files_by_stem(xml_dir).get(_refid_basename(refid))


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
            name = _text(compound.find("name")) or _refid_basename(refid)[len(REQUIREMENT_REFID_PREFIX):]
            requirements[name] = refid

    return all_refids, requirements


def load_requirement_details(xml_dir: Path, refid: str) -> tuple[str, str]:
    """Return (title, description) for one requirement, reading its own XML file.

    Requirements imported from an external tag file (Polarion/DOORS/...) have
    no local XML file of their own -- return empty strings for those rather
    than failing.
    """
    path = _resolve_xml(xml_dir, refid)
    if path is None:
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

def _requirement_id_from_refid(refid: str, id_by_refid: dict[str, str]) -> str:
    refid = _refid_basename(refid)
    if refid in id_by_refid:
        return id_by_refid[refid]
    # Fall back to stripping the conventional prefix (e.g. an external /
    # tag-file requirement not otherwise present in this project's index).
    if refid.startswith(REQUIREMENT_REFID_PREFIX):
        return refid[len(REQUIREMENT_REFID_PREFIX):]
    return refid


def _parse_fragment_text(raw_text: str, tag: str, req_id: str, location: str) -> tuple[int | None, str]:
    """Split a @satisfies / @verifies short text into (declared_total, remaining_text).

    Exits with a clear error on a recognizable-but-malformed !partial(...)
    marker (e.g. a typo'd argument), rather than silently ignoring it.
    """
    stripped = raw_text.strip()
    m = PARTIAL_MARKER_RE.match(stripped)
    if m:
        return int(m.group(1)), stripped[m.end():].strip()
    if stripped.startswith(PARTIAL_MARKER_PREFIX):
        sexit(f"error: {location}: malformed !partial(...) marker on "
                  f"@{tag} '{req_id}': {stripped!r}")
    return None, stripped


def collect_satisfies_and_verifies(xml_dir: Path, all_refids: list[str],
                                    requirements_by_id: dict[str, str]):
    """Return ({req_id: [Fragment, ...]} for @satisfies, same for @verifies)."""
    satisfies: dict[str, list[Fragment]] = {}
    verifies: dict[str, list[Fragment]] = {}
    id_by_refid = {_refid_basename(rf): rid for rid, rf in requirements_by_id.items()}

    for refid in all_refids:
        if _refid_basename(refid).startswith(REQUIREMENT_REFID_PREFIX):
            continue  # a requirement's own file never links back this way
        path = _resolve_xml(xml_dir, refid)
        if path is None:
            continue

        root = ET.parse(path).getroot()
        compounddef = root.find("compounddef")
        if compounddef is None:
            continue
        compound_name = _text(compounddef.find("compoundname"))

        for satisfies_el in compounddef.iter("satisfies"):
            for req_el in satisfies_el.findall("requirement"):
                req_id = _requirement_id_from_refid(req_el.get("refid", ""), id_by_refid)
                declared_total, frag_text = _parse_fragment_text(_text(req_el), "satisfies", req_id, compound_name)
                satisfies.setdefault(req_id, []).append(
                    Fragment(location=compound_name, declared_total=declared_total, text=frag_text)
                )

        for verifies_el in compounddef.iter("verifies"):
            for req_el in verifies_el.findall("requirement"):
                req_id = _requirement_id_from_refid(req_el.get("refid", ""), id_by_refid)
                if not TEST_CASE_RE.match(compound_name):
                    LOGGER.warning( f'@verifies found on "{ compound_name }", which does not look like a test class ( expected TC_NN_Name )' )
                declared_total, frag_text = _parse_fragment_text(_text(req_el), "verifies", req_id, compound_name)
                verifies.setdefault(req_id, []).append(
                    Fragment(location=compound_name, declared_total=declared_total, text=frag_text)
                )

    return satisfies, verifies


def resolve_status(req_id: str, tag: str, frags: list[Fragment]) -> CoverageStatus:
    """Turn the raw @satisfies / @verifies fragments for one requirement into a status.

    The required total is the N of !partial( N ) when any occurrence declares
    one (exits with an error if they disagree), otherwise the number of
    occurrences -- or 1 when there are none, so "nothing found" stays 0/1.
    """
    declared = {f.declared_total for f in frags if f.declared_total is not None}
    if len(declared) > 1:
        detail = ", ".join(f"{f.location}=!partial({f.declared_total})"
                            for f in frags if f.declared_total is not None)
        sexit(f"error: '{req_id}' has mismatching !partial(...) totals on @{tag}: {detail}")

    found = len(frags)
    explicit = bool(declared)
    required = declared.pop() if declared else max(found, 1)

    # An explicit !partial( N ) is a promise about the total: finding fewer
    # occurrences is just "partially done" (0 < found < N), but finding MORE
    # means the declaration (or a stray tag) is wrong. Implicit totals can't
    # trigger this, since they always equal the number of occurrences.
    if found > required:
        locations = ", ".join(f.location for f in frags)
        sexit(f"error: '{req_id}' declares !partial({required}) on @{tag} but has "
              f"{found} occurrences ({locations})")

    return CoverageStatus(found=found, required=required, fragments=frags, explicit=explicit)


# --------------------------------------------------------------------------
# 3. Cross-reference into report rows.
# --------------------------------------------------------------------------

def _natural_key(req_id: str):
    return [int(tok) if tok.isdigit() else tok for tok in regex_split(r"(\d+)", req_id)]


def build_rows(xml_dir: Path, requirements_by_id: dict[str, str],
               satisfies: dict[str, list[Fragment]],
               verifies: dict[str, list[Fragment]]) -> list[RequirementRow]:
    known_ids = set(requirements_by_id)

    unknown = (set(satisfies) | set(verifies)) - known_ids
    for req_id in sorted(unknown):
        LOGGER.warning( f'"{ req_id }" is referenced by @satisfies/@verifies but has no matching @requirement' )

    rows = []
    for req_id in sorted(requirements_by_id, key=_natural_key):
        title, description = load_requirement_details(xml_dir, requirements_by_id[req_id])
        rows.append(RequirementRow(
            req_id=req_id,
            title=title,
            description=description,
            impl=resolve_status(req_id, "satisfies", satisfies.get(req_id, [])),
            verif=resolve_status(req_id, "verifies", verifies.get(req_id, [])),
        ))
    return rows


# --------------------------------------------------------------------------
# 4. Output writers.
# --------------------------------------------------------------------------

def _summary(rows: list[RequirementRow]) -> tuple[int, int, int, int, int]:
    total = len(rows)
    n_impl = sum(1 for r in rows if r.impl.complete)
    n_partial = sum(1 for r in rows if r.impl.partial)
    n_test = sum(1 for r in rows if r.verif.complete)
    n_test_partial = sum(1 for r in rows if r.verif.partial)
    return total, n_impl, n_partial, n_test, n_test_partial


def _description_column(row: RequirementRow) -> str:
    if row.title and row.description:
        return f"{row.title} -- {row.description}"
    return row.title or row.description


def _status_cell(status: CoverageStatus, symbols: tuple[str, str, str]) -> str:
    """symbols = (complete, partial, none), rendered plain unless the
    requirement explicitly declares !partial( x ), in which case the
    found/required fraction is appended."""
    complete_sym, partial_sym, none_sym = symbols
    if not status.is_partial_tracked:
        return complete_sym if status.complete else none_sym
    if status.complete:
        sym = complete_sym
    elif status.partial:
        sym = partial_sym
    else:
        sym = none_sym
    return f"{sym} ({status.found}/{status.required})"


def _fragment_notes(statuses: list[tuple[str, CoverageStatus]]) -> list[tuple[str, CoverageStatus]]:
    return [(rid, st) for rid, st in statuses if st.is_partial_tracked and st.fragments]


def write_markdown(rows: list[RequirementRow], out_path: Path, with_description: bool) -> None:
    total, n_impl, n_partial, n_test, n_test_partial = _summary(rows)

    lines = ["# Requirements traceability report", ""]
    if total:
        summary = (
            f"**Implemented:** {n_impl}/{total} ({100 * n_impl / total:.0f}%)  \n"
            f"**Tested:** {n_test}/{total} ({100 * n_test / total:.0f}%)"
        )
        if n_partial:
            summary += f"  \n**Partially implemented:** {n_partial}"
        if n_test_partial:
            summary += f"  \n**Partially tested:** {n_test_partial}"
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
        cells.append(_status_cell(row.impl, ("✔", "◐", "✘")))
        cells.append(_status_cell(row.verif, ("✔", "◐", "✘")))
        cells.append(", ".join(row.test_names))
        lines.append("| " + " | ".join(cells) + " |")

    for title, pick in (("Partial implementation details", lambda r: r.impl),
                        ("Partial verification details", lambda r: r.verif)):
        notes = _fragment_notes([(r.req_id, pick(r)) for r in rows])
        if not notes:
            continue
        lines += ["", f"## {title}", ""]
        for req_id, status in notes:
            lines.append(f"**{req_id}** ({status.found}/{status.required} fragments):")
            for frag in status.fragments:
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

    total, n_impl, n_partial, n_test, n_test_partial = _summary(rows)

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
        cells.append(_status_cell(row.impl, ("Yes", "Partial", "No")))
        cells.append(_status_cell(row.verif, ("Yes", "Partial", "No")))
        cells.append(", ".join(row.test_names))
        ws.append(cells)

        r = ws.max_row
        for col, status in ((impl_col, row.impl), (test_col, row.verif)):
            cell = ws.cell(row=r, column=col)
            cell.fill = green if status.complete else (yellow if status.partial else red)
            cell.alignment = Alignment(horizontal="center")
            if status.is_partial_tracked and status.fragments:
                note = "\n".join(
                    f"{f.location}: {f.text}" if f.text else f.location
                    for f in status.fragments
                )
                cell.comment = Comment(note, "gen_requirements_report")

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
        ws2.append(["Partially tested", n_test_partial, total, f"{100 * n_test_partial / total:.0f}%"])
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

    satisfies, verifies = collect_satisfies_and_verifies(
        args.xml_dir, all_refids, requirements_by_id)
    rows = build_rows(args.xml_dir, requirements_by_id, satisfies, verifies)

    if args.output_md:
        with CONSOLE.status( '[bold magenta]Generating markdown...' ):
            write_markdown(rows, args.output_md, args.with_description)
        LOGGER.info( f'Wrote "{ args.output_md }"' )
    if args.output_xlsx:
        with CONSOLE.status( '[bold magenta]Generating xlsx...' ):
            write_excel(rows, args.output_xlsx, args.with_description)
        LOGGER.info( f'Wrote "{ args.output_xlsx }"' )

    total, n_impl, n_partial, n_test, n_test_partial = _summary(rows)
    if total:
        pct_impl = 100 * n_impl / total
        pct_test = 100 * n_test / total
        LOGGER.info( f'Implemented: { n_impl } / { total } ( {pct_impl:.0f}% )' + ( f', partially: { n_partial }' if n_partial else '' ) )
        LOGGER.info( f'Tested:      { n_test } / { total } ( {pct_test:.0f}% )' + ( f', partially: { n_test_partial }' if n_test_partial else '' ) )

        if args.fail_under is not None:
            avg = (pct_impl + pct_test) / 2
            if avg < args.fail_under:
                LOGGER.critical( f'coverage {avg:.0f}% is below --fail-under {args.fail_under:.0f}%' )
                sexit(1)


if __name__ == "__main__":
    main()
