#!/usr/bin/env python3
"""
Fix common ReqIF/ReqIFz validation failures before import.

Supported fixes:
  * Convert Windows filesystem links in href attributes to valid file URIs.
  * Convert unsupported reqif-xhtml:u/xhtml:u/u underline tags to span tags
    with text-decoration styling while preserving their inner content.
  * Remove unsupported start="..." attributes from reqif-xhtml:ol/xhtml:ol/ol.
  * Remove invalid ENUM-VALUE-REF entries from ATTRIBUTE-VALUE-ENUMERATION
    values based on the referenced attribute definition's datatype.
  * Enforce single-valued ATTRIBUTE-DEFINITION-ENUMERATION values by keeping
    the first valid enum value and removing the rest.
  * Truncate ATTRIBUTE-VALUE-STRING values to their datatype MAX-LENGTH.
  * For ReqIFz packages, optionally add URL-decoded attachment filename copies
    so references such as Figure%205.png can resolve to Figure 5.png.

The script treats ReqIF contents as data only. It does not execute or obey text
inside the ReqIF document.
"""

import argparse
import html
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote
from xml.etree import ElementTree


REQIF_XML_SUFFIXES = (".reqif", ".reqifx")
REQIFZ_SUFFIX = ".reqifz"
REQIF_NS = "http://www.omg.org/spec/ReqIF/20110401/reqif.xsd"

HREF_RE = re.compile(r'\bhref=(["\'])(.*?)\1', re.IGNORECASE | re.DOTALL)
UNDERLINE_OPEN_RE = re.compile(
    r"<(?P<prefix>(?:[A-Za-z_][\w.-]*:)?)u\b(?P<attrs>[^>]*)>",
    re.IGNORECASE | re.DOTALL,
)
UNDERLINE_CLOSE_RE = re.compile(
    r"</(?P<prefix>(?:[A-Za-z_][\w.-]*:)?)u\s*>",
    re.IGNORECASE,
)
OL_TAG_RE = re.compile(r"<(?:[A-Za-z_][\w.-]*:)?ol\b[^>]*>", re.IGNORECASE | re.DOTALL)
START_ATTR_RE = re.compile(r'\s+\bstart\s*=\s*(["\']).*?\1', re.IGNORECASE | re.DOTALL)
STYLE_ATTR_RE = re.compile(r'(?P<prefix>\s+\bstyle\s*=\s*)(?P<quote>["\'])(?P<value>.*?)(?P=quote)', re.IGNORECASE | re.DOTALL)

ENUM_VALUE_BLOCK_RE = re.compile(
    r"<ATTRIBUTE-VALUE-ENUMERATION\b[^>]*>.*?</ATTRIBUTE-VALUE-ENUMERATION>",
    re.DOTALL,
)
ENUM_DEF_REF_RE = re.compile(
    r"<ATTRIBUTE-DEFINITION-ENUMERATION-REF>(?P<id>[^<]+)</ATTRIBUTE-DEFINITION-ENUMERATION-REF>"
)
ENUM_VALUE_REF_LINE_RE = re.compile(
    r"(?P<line>[ \t]*<ENUM-VALUE-REF>(?P<id>[^<]+)</ENUM-VALUE-REF>\s*)"
)

STRING_VALUE_BLOCK_RE = re.compile(
    r"<ATTRIBUTE-VALUE-STRING\b(?P<attrs>[^>]*)>(?P<body>.*?)</ATTRIBUTE-VALUE-STRING>",
    re.DOTALL,
)
STRING_VALUE_ATTR_RE = re.compile(r'(?P<prefix>\sTHE-VALUE\s*=\s*)(?P<quote>["\'])(?P<value>.*?)(?P=quote)', re.DOTALL)
STRING_DEF_REF_RE = re.compile(
    r"<ATTRIBUTE-DEFINITION-STRING-REF>(?P<id>[^<]+)</ATTRIBUTE-DEFINITION-STRING-REF>"
)


@dataclass
class ReqifMetadata:
    enum_allowed_by_attr: dict[str, set[str]]
    enum_multi_by_attr: dict[str, bool]
    string_max_by_attr: dict[str, int]


def qname(local_name: str) -> str:
    return f"{{{REQIF_NS}}}{local_name}"


def default_output_path(input_path: Path) -> Path:
    suffix = input_path.suffix or ".reqif"
    stem = input_path.stem if input_path.suffix else input_path.name
    return input_path.with_name(f"{stem}_import_fixed{suffix}")


def is_reqifz(path: Path) -> bool:
    return path.suffix.lower() == REQIFZ_SUFFIX


def is_reqif_xml_entry(name: str) -> bool:
    return name.lower().endswith(REQIF_XML_SUFFIXES)


def empty_counts() -> dict[str, int]:
    return {
        "windows_hrefs": 0,
        "underline_open_tags": 0,
        "underline_close_tags": 0,
        "ol_start_attrs": 0,
        "invalid_enum_refs_removed": 0,
        "extra_enum_refs_removed": 0,
        "strings_truncated": 0,
        "string_chars_removed": 0,
        "decoded_attachment_entries_added": 0,
        "attachment_entries_decoded": 0,
    }


def add_counts(total: dict[str, int], counts: dict[str, int]) -> None:
    for key, value in counts.items():
        total[key] += value


def normalize_windows_href(value: str) -> str:
    unescaped_value = html.unescape(value.strip())

    candidate = re.sub(r"^(?:https?://|file:/+)", "", unescaped_value, flags=re.IGNORECASE)
    if not re.match(r"^[A-Za-z]:\\", candidate):
        return value

    normalized_path = candidate.replace("\\", "/")
    file_uri = "file:///" + normalized_path
    return quote(file_uri, safe="/:")


def convert_underline_attrs_to_span_attrs(attrs: str) -> str:
    style_match = STYLE_ATTR_RE.search(attrs)
    underline_style = "text-decoration: underline"
    if not style_match:
        return f'{attrs} style="{underline_style}"'

    style_value = html.unescape(style_match.group("value")).strip()
    if "text-decoration" in style_value.lower():
        return attrs

    separator = "" if not style_value or style_value.endswith(";") else ";"
    fixed_style = f"{style_value}{separator} {underline_style}".strip()
    return (
        attrs[: style_match.start("value")]
        + html.escape(fixed_style, quote=True)
        + attrs[style_match.end("value") :]
    )


def collect_reqif_metadata(text: str) -> ReqifMetadata:
    root = ElementTree.fromstring(text)

    enum_allowed_by_datatype: dict[str, set[str]] = {}
    for datatype in root.iter(qname("DATATYPE-DEFINITION-ENUMERATION")):
        datatype_id = datatype.attrib.get("IDENTIFIER")
        if not datatype_id:
            continue
        enum_allowed_by_datatype[datatype_id] = {
            enum_value.attrib["IDENTIFIER"]
            for enum_value in datatype.iter(qname("ENUM-VALUE"))
            if enum_value.attrib.get("IDENTIFIER")
        }

    enum_allowed_by_attr: dict[str, set[str]] = {}
    enum_multi_by_attr: dict[str, bool] = {}
    for attr_def in root.iter(qname("ATTRIBUTE-DEFINITION-ENUMERATION")):
        attr_id = attr_def.attrib.get("IDENTIFIER")
        datatype_ref = attr_def.find(f".//{qname('DATATYPE-DEFINITION-ENUMERATION-REF')}")
        if not attr_id or datatype_ref is None or not datatype_ref.text:
            continue
        enum_allowed_by_attr[attr_id] = enum_allowed_by_datatype.get(datatype_ref.text, set())
        enum_multi_by_attr[attr_id] = attr_def.attrib.get("MULTI-VALUED", "false").lower() == "true"

    string_max_by_datatype: dict[str, int] = {}
    for datatype in root.iter(qname("DATATYPE-DEFINITION-STRING")):
        datatype_id = datatype.attrib.get("IDENTIFIER")
        max_length = datatype.attrib.get("MAX-LENGTH")
        if not datatype_id or not max_length:
            continue
        try:
            string_max_by_datatype[datatype_id] = int(max_length)
        except ValueError:
            continue

    string_max_by_attr: dict[str, int] = {}
    for attr_def in root.iter(qname("ATTRIBUTE-DEFINITION-STRING")):
        attr_id = attr_def.attrib.get("IDENTIFIER")
        datatype_ref = attr_def.find(f".//{qname('DATATYPE-DEFINITION-STRING-REF')}")
        if not attr_id or datatype_ref is None or not datatype_ref.text:
            continue
        max_length = string_max_by_datatype.get(datatype_ref.text)
        if max_length:
            string_max_by_attr[attr_id] = max_length

    return ReqifMetadata(
        enum_allowed_by_attr=enum_allowed_by_attr,
        enum_multi_by_attr=enum_multi_by_attr,
        string_max_by_attr=string_max_by_attr,
    )


def sanitize_xhtml_text(text: str) -> tuple[str, dict[str, int]]:
    counts = empty_counts()

    def replace_href(match: re.Match) -> str:
        quote_char = match.group(1)
        original_value = match.group(2)
        fixed_value = normalize_windows_href(original_value)
        if fixed_value == original_value:
            return match.group(0)

        counts["windows_hrefs"] += 1
        return f"href={quote_char}{html.escape(fixed_value, quote=True)}{quote_char}"

    def replace_ol_tag(match: re.Match) -> str:
        tag = match.group(0)
        fixed_tag, replacements = START_ATTR_RE.subn("", tag)
        counts["ol_start_attrs"] += replacements
        return fixed_tag

    text = HREF_RE.sub(replace_href, text)

    def replace_underline_open(match: re.Match) -> str:
        prefix = match.group("prefix")
        attrs = convert_underline_attrs_to_span_attrs(match.group("attrs"))
        return f"<{prefix}span{attrs}>"

    def replace_underline_close(match: re.Match) -> str:
        return f"</{match.group('prefix')}span>"

    text, open_count = UNDERLINE_OPEN_RE.subn(replace_underline_open, text)
    counts["underline_open_tags"] = open_count

    text, close_count = UNDERLINE_CLOSE_RE.subn(replace_underline_close, text)
    counts["underline_close_tags"] = close_count

    text = OL_TAG_RE.sub(replace_ol_tag, text)
    return text, counts


def fix_enum_values(text: str, metadata: ReqifMetadata) -> tuple[str, dict[str, int]]:
    counts = empty_counts()

    def replace_block(match: re.Match) -> str:
        block = match.group(0)
        attr_match = ENUM_DEF_REF_RE.search(block)
        if not attr_match:
            return block

        attr_id = attr_match.group("id")
        allowed_values = metadata.enum_allowed_by_attr.get(attr_id)
        if allowed_values is None:
            return block

        is_multi_valued = metadata.enum_multi_by_attr.get(attr_id, False)
        kept_valid_values = 0

        def replace_value_ref(value_match: re.Match) -> str:
            nonlocal kept_valid_values
            enum_id = value_match.group("id")
            is_valid = enum_id in allowed_values
            if not is_valid:
                counts["invalid_enum_refs_removed"] += 1
                return ""

            kept_valid_values += 1
            if not is_multi_valued and kept_valid_values > 1:
                counts["extra_enum_refs_removed"] += 1
                return ""

            return value_match.group("line")

        return ENUM_VALUE_REF_LINE_RE.sub(replace_value_ref, block)

    return ENUM_VALUE_BLOCK_RE.sub(replace_block, text), counts


def encode_xml_attr_value(value: str) -> str:
    return html.escape(value, quote=True).replace("\r", "&#13;").replace("\n", "&#10;")


def truncate_string_values(text: str, metadata: ReqifMetadata) -> tuple[str, dict[str, int]]:
    counts = empty_counts()

    def replace_block(match: re.Match) -> str:
        attrs = match.group("attrs")
        body = match.group("body")
        attr_ref_match = STRING_DEF_REF_RE.search(body)
        value_match = STRING_VALUE_ATTR_RE.search(attrs)
        if not attr_ref_match or not value_match:
            return match.group(0)

        max_length = metadata.string_max_by_attr.get(attr_ref_match.group("id"))
        if not max_length:
            return match.group(0)

        value = html.unescape(value_match.group("value"))
        if len(value) <= max_length:
            return match.group(0)

        truncated_value = value[:max_length]
        encoded_value = encode_xml_attr_value(truncated_value)
        fixed_attrs = (
            attrs[: value_match.start("value")]
            + encoded_value
            + attrs[value_match.end("value") :]
        )

        counts["strings_truncated"] += 1
        counts["string_chars_removed"] += len(value) - len(truncated_value)
        return f"<ATTRIBUTE-VALUE-STRING{fixed_attrs}>{body}</ATTRIBUTE-VALUE-STRING>"

    return STRING_VALUE_BLOCK_RE.sub(replace_block, text), counts


def fix_reqif_text(text: str, args: argparse.Namespace) -> tuple[str, dict[str, int]]:
    metadata = collect_reqif_metadata(text)
    total_counts = empty_counts()

    text, counts = sanitize_xhtml_text(text)
    add_counts(total_counts, counts)

    if not args.no_fix_enums:
        text, counts = fix_enum_values(text, metadata)
        add_counts(total_counts, counts)

    if not args.no_truncate_strings:
        text, counts = truncate_string_values(text, metadata)
        add_counts(total_counts, counts)

    return text, total_counts


def scan_plain_reqif(input_path: Path, args: argparse.Namespace) -> dict[str, int]:
    text = input_path.read_text(encoding=args.encoding)
    _, counts = fix_reqif_text(text, args)
    return counts


def write_plain_reqif(input_path: Path, output_path: Path, args: argparse.Namespace) -> dict[str, int]:
    text = input_path.read_text(encoding=args.encoding)
    fixed_text, counts = fix_reqif_text(text, args)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(fixed_text, encoding=args.encoding)
    return counts


def clone_zip_info(source: zipfile.ZipInfo, filename: str) -> zipfile.ZipInfo:
    target = zipfile.ZipInfo(filename=filename, date_time=source.date_time)
    target.comment = source.comment
    target.extra = source.extra
    target.internal_attr = source.internal_attr
    target.external_attr = source.external_attr
    target.compress_type = source.compress_type
    target.create_system = source.create_system
    return target


def attachment_names_for_entry(entry_name: str, mode: str) -> list[str]:
    decoded_name = unquote(entry_name)
    if mode == "preserve" or decoded_name == entry_name:
        return [entry_name]
    if mode == "decode":
        return [decoded_name]
    if mode == "duplicate-decoded":
        return [entry_name, decoded_name]
    raise ValueError(f"Unsupported attachment filename mode: {mode}")


def patch_reqifz(
    input_path: Path,
    output_path: Path,
    args: argparse.Namespace,
    dry_run: bool,
) -> tuple[dict[str, int], int]:
    total_counts = empty_counts()
    scanned_entries = 0
    written_names: set[str] = set()

    if not zipfile.is_zipfile(input_path):
        raise ValueError(f"Input has .reqifz extension but is not a readable ZIP archive: {input_path}")

    with zipfile.ZipFile(input_path, "r") as source_zip:
        target_zip = None
        try:
            if not dry_run:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                target_zip = zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED)

            source_names = set(source_zip.namelist())
            for entry in source_zip.infolist():
                content = source_zip.read(entry.filename)
                output_names = [entry.filename]

                if is_reqif_xml_entry(entry.filename):
                    scanned_entries += 1
                    text = content.decode(args.encoding)
                    fixed_text, counts = fix_reqif_text(text, args)
                    add_counts(total_counts, counts)
                    content = fixed_text.encode(args.encoding)
                else:
                    output_names = attachment_names_for_entry(entry.filename, args.attachment_filename_mode)
                    if args.attachment_filename_mode == "decode" and output_names[0] != entry.filename:
                        total_counts["attachment_entries_decoded"] += 1
                    elif args.attachment_filename_mode == "duplicate-decoded":
                        for output_name in output_names:
                            if output_name != entry.filename and output_name not in source_names:
                                total_counts["decoded_attachment_entries_added"] += 1

                if target_zip is not None:
                    for output_name in output_names:
                        if output_name in written_names:
                            continue
                        written_names.add(output_name)
                        target_zip.writestr(clone_zip_info(entry, output_name), content)
        finally:
            if target_zip is not None:
                target_zip.close()

    return total_counts, scanned_entries


def print_counts(counts: dict[str, int]) -> None:
    print(f"Windows hrefs normalized: {counts['windows_hrefs']}")
    print(f"Underline opening tags converted to spans: {counts['underline_open_tags']}")
    print(f"Underline closing tags converted to spans: {counts['underline_close_tags']}")
    print(f"Ordered-list start attributes removed: {counts['ol_start_attrs']}")
    print(f"Invalid enum refs removed: {counts['invalid_enum_refs_removed']}")
    print(f"Extra enum refs removed from single-valued enums: {counts['extra_enum_refs_removed']}")
    print(f"String values truncated: {counts['strings_truncated']}")
    print(f"String characters removed: {counts['string_chars_removed']}")
    print(f"Decoded attachment entries added: {counts['decoded_attachment_entries_added']}")
    print(f"Attachment entries renamed to decoded names: {counts['attachment_entries_decoded']}")
    print(f"Total XML sanitizations: {sum(counts[k] for k in counts if k not in {'decoded_attachment_entries_added', 'attachment_entries_decoded'})}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fix ReqIF/ReqIFz validation failures that block import."
    )
    parser.add_argument("input", type=Path, help="ReqIF/ReqIFx/ReqIFz file to fix")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output path. Defaults to '<input>_import_fixed.<ext>'.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Modify the input file in place instead of writing a copy.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing a file.",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help='Text encoding used to read/write embedded ReqIF XML. Default: "utf-8".',
    )
    parser.add_argument(
        "--attachment-filename-mode",
        choices=("preserve", "duplicate-decoded", "decode"),
        default="duplicate-decoded",
        help=(
            "How to handle percent-encoded attachment entry names in ReqIFz files. "
            "'duplicate-decoded' keeps original entries and adds decoded copies."
        ),
    )
    parser.add_argument(
        "--no-fix-enums",
        action="store_true",
        help="Do not remove invalid or extra enum refs.",
    )
    parser.add_argument(
        "--no-truncate-strings",
        action="store_true",
        help="Do not truncate string values longer than their datatype MAX-LENGTH.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.expanduser()

    if not input_path.is_file():
        print(f"Input file does not exist: {input_path}", file=sys.stderr)
        return 1

    if args.in_place and args.output:
        print("Use either --in-place or --output, not both.", file=sys.stderr)
        return 1

    output_path = input_path if args.in_place else (args.output.expanduser() if args.output else default_output_path(input_path))
    if output_path.exists() and output_path != input_path and not args.force and not args.dry_run:
        print(f"Output file already exists, pass --force to overwrite: {output_path}", file=sys.stderr)
        return 2

    temp_output_path = None
    actual_output_path = output_path
    if args.in_place and is_reqifz(input_path) and not args.dry_run:
        temp_output_path = input_path.with_name(f".{input_path.name}.tmp-import-fixed")
        actual_output_path = temp_output_path

    try:
        if is_reqifz(input_path):
            counts, scanned_entries = patch_reqifz(
                input_path,
                actual_output_path,
                args,
                dry_run=args.dry_run,
            )
        elif args.dry_run:
            counts = scan_plain_reqif(input_path, args)
            scanned_entries = 1
        else:
            counts = write_plain_reqif(input_path, actual_output_path, args)
            scanned_entries = 1
    except (ElementTree.ParseError, UnicodeDecodeError, zipfile.BadZipFile, ValueError) as error:
        print(f"Failed to fix {input_path}: {error}", file=sys.stderr)
        return 1

    print(f"Input: {input_path}")
    if is_reqifz(input_path):
        print(f"ReqIF XML entries scanned: {scanned_entries}")
        print(f"Attachment filename mode: {args.attachment_filename_mode}")
    print_counts(counts)

    if args.dry_run:
        print(f"Dry run only. Output would be: {output_path}")
        return 0

    if temp_output_path is not None:
        temp_output_path.replace(input_path)

    print(f"Output: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
