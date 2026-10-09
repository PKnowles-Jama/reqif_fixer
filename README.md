# ReqIF Tools

Utilities in this folder help prepare ReqIF and ReqIFz files for import.

## Import Fixer

Use `fix_reqif_for_import.py` when a ReqIF validator reports import-blocking
schema or semantic issues. It fixes the same XHTML problems as the sanitizer
below, converting unsupported underline tags to styled spans instead of dropping
the visual formatting. It also fixes invalid/single-valued enum references,
string values longer than their datatype `MAX-LENGTH`, and percent-encoded
ReqIFz attachment entry names.

For the supplied `22122.reqifz`, run:

```bash
backend/tools/reqif/fix_reqif_for_import.py \
  "/Users/jhenriquez/Downloads/22122.reqifz" \
  -o "/Users/jhenriquez/Downloads/22122_import_fixed.reqifz" \
  --force
```

The default attachment mode keeps the original packaged attachment entries and
adds URL-decoded copies such as `Figure 5.png`, so stricter validators can
resolve `Figure%205.png` references without removing the original entries.

Use `--dry-run` first to preview the changes without writing a file.

## XHTML Sanitizer

Use `sanitize_reqif_xhtml.py` when a ReqIF/ReqIFz file is valid XML but fails
ReqIF schema validation or import because of unsupported XHTML content.

The sanitizer currently fixes:

- Windows filesystem links in XHTML `href` attributes, for example
  `http://W:\documents\A B` becomes `file:///W:/documents/A%20B`.
- Unsupported underline tags such as `<reqif-xhtml:u>` while preserving the
  text inside the tag.
- Unsupported `start="..."` attributes on ordered lists such as
  `<reqif-xhtml:ol start="2">`.

For `.reqifz` packages, only embedded `.reqif` or `.reqifx` XML entries are
patched. Attachments and all other archive entries are copied through unchanged.

### Dry Run

```bash
backend/tools/reqif/sanitize_reqif_xhtml.py \
  "/path/to/input.reqifz" \
  --dry-run
```

### Write A Fixed Copy

```bash
backend/tools/reqif/sanitize_reqif_xhtml.py \
  "/path/to/input.reqifz"
```

The default output is written beside the input:

```text
/path/to/input_xhtml_sanitized.reqifz
```

### Choose Output Path

```bash
backend/tools/reqif/sanitize_reqif_xhtml.py \
  "/path/to/input.reqifz" \
  -o "/path/to/fixed.reqifz"
```

Pass `--force` if the output file already exists.

### Plain ReqIF Files

The same commands work for `.reqif` and `.reqifx` files:

```bash
backend/tools/reqif/sanitize_reqif_xhtml.py \
  "/path/to/input.reqif"
```
