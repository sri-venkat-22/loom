#!/usr/bin/env python
# flake8: noqa: E501
"""
Make loom/resources/report-reference.docx, the Word reference document that styles
/project report's .docx (pandoc's --reference-doc), from pandoc's own default one.

The styling is a clean academic report: Cambria body text, Calibri headings in navy with
each top-level section on a new page, ruled tables with a shaded header row, A4 pages
with 1-inch margins and a page number in the footer. The title, subtitle, date and table
of contents fill the first page.

Run it with pandoc on the PATH:

    python scripts/make_report_reference.py

The output is byte-for-byte the same each time for the same pandoc.
"""

import re
import subprocess
import sys
import zipfile
from io import BytesIO
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "loom" / "resources" / "report-reference.docx"

NAVY = "1F3864"
GREY = "595959"
RULE = "BFBFBF"
SHADE = "F2F2F2"

FOOTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:p>
    <w:pPr><w:jc w:val="center" /></w:pPr>
    <w:r><w:rPr><w:color w:val="595959" /><w:sz w:val="18" /></w:rPr><w:fldChar w:fldCharType="begin" /></w:r>
    <w:r><w:rPr><w:color w:val="595959" /><w:sz w:val="18" /></w:rPr><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>
    <w:r><w:rPr><w:color w:val="595959" /><w:sz w:val="18" /></w:rPr><w:fldChar w:fldCharType="separate" /></w:r>
    <w:r><w:rPr><w:color w:val="595959" /><w:sz w:val="18" /></w:rPr><w:t>1</w:t></w:r>
    <w:r><w:rPr><w:color w:val="595959" /><w:sz w:val="18" /></w:rPr><w:fldChar w:fldCharType="end" /></w:r>
  </w:p>
</w:ftr>
"""

FOOTER_ID = "rId90"
FOOTER_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
FOOTER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"

# A4, with 1-inch margins, and the footer
SECTION = f"""<w:sectPr>
      <w:footerReference w:type="default" r:id="{FOOTER_ID}" />
      <w:footnotePr>
        <w:numRestart w:val="eachSect" />
      </w:footnotePr>
      <w:pgSz w:w="11906" w:h="16838" />
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="720" w:footer="720" w:gutter="0" />
    </w:sectPr>"""

# style id: (paragraph properties, run properties) that replace the default's
STYLES = {
    "BodyText": ('<w:spacing w:before="0" w:after="140" w:line="288" w:lineRule="auto" />', ""),
    "Compact": ('<w:spacing w:before="20" w:after="20" w:line="264" w:lineRule="auto" />', ""),
    "Title": (
        (
            '<w:spacing w:before="2880" w:after="160" w:line="240" w:lineRule="auto" />'
            '<w:contextualSpacing /><w:jc w:val="center" />'
        ),
        (
            '<w:rFonts w:asciiTheme="majorHAnsi" w:eastAsiaTheme="majorEastAsia"'
            ' w:hAnsiTheme="majorHAnsi" w:cstheme="majorBidi" /><w:b /><w:bCs />'
            f'<w:color w:val="{NAVY}" /><w:sz w:val="52" /><w:szCs w:val="52" />'
        ),
    ),
    "Subtitle": (
        '<w:spacing w:before="0" w:after="240" /><w:jc w:val="center" />',
        (
            f'<w:b w:val="0" /><w:bCs w:val="0" /><w:i /><w:color w:val="{GREY}" />'
            '<w:sz w:val="28" /><w:szCs w:val="28" />'
        ),
    ),
    "Author": (
        (
            '<w:keepNext /><w:keepLines /><w:spacing w:before="0" w:after="60" /><w:jc'
            ' w:val="center" />'
        ),
        (
            f'<w:b w:val="0" /><w:bCs w:val="0" /><w:color w:val="{GREY}" /><w:sz w:val="24" />'
            '<w:szCs w:val="24" />'
        ),
    ),
    "Date": (
        (
            '<w:keepNext /><w:keepLines /><w:spacing w:before="0" w:after="720" /><w:jc'
            ' w:val="center" />'
        ),
        (
            f'<w:b w:val="0" /><w:bCs w:val="0" /><w:color w:val="{GREY}" /><w:sz w:val="24" />'
            '<w:szCs w:val="24" />'
        ),
    ),
    "Heading1": (
        (
            "<w:keepNext /><w:keepLines /><w:pageBreakBefore />"
            f'<w:pBdr><w:bottom w:val="single" w:sz="8" w:space="4" w:color="{NAVY}" /></w:pBdr>'
            '<w:spacing w:before="0" w:after="240" /><w:outlineLvl w:val="0" />'
        ),
        (
            '<w:rFonts w:asciiTheme="majorHAnsi" w:eastAsiaTheme="majorEastAsia"'
            ' w:hAnsiTheme="majorHAnsi" w:cstheme="majorBidi" /><w:b /><w:bCs />'
            f'<w:color w:val="{NAVY}" /><w:sz w:val="34" /><w:szCs w:val="34" />'
        ),
    ),
    "Heading2": (
        (
            '<w:keepNext /><w:keepLines /><w:spacing w:before="320" w:after="120" />'
            '<w:outlineLvl w:val="1" />'
        ),
        (
            '<w:rFonts w:asciiTheme="majorHAnsi" w:eastAsiaTheme="majorEastAsia"'
            ' w:hAnsiTheme="majorHAnsi" w:cstheme="majorBidi" /><w:b /><w:bCs />'
            f'<w:color w:val="{NAVY}" /><w:sz w:val="28" /><w:szCs w:val="28" />'
        ),
    ),
    "Heading3": (
        (
            '<w:keepNext /><w:keepLines /><w:spacing w:before="240" w:after="80" />'
            '<w:outlineLvl w:val="2" />'
        ),
        (
            '<w:rFonts w:asciiTheme="majorHAnsi" w:eastAsiaTheme="majorEastAsia"'
            ' w:hAnsiTheme="majorHAnsi" w:cstheme="majorBidi" /><w:b /><w:bCs />'
            f'<w:color w:val="{NAVY}" /><w:sz w:val="24" /><w:szCs w:val="24" />'
        ),
    ),
    "Heading4": (
        (
            '<w:keepNext /><w:keepLines /><w:spacing w:before="200" w:after="60" />'
            '<w:outlineLvl w:val="3" />'
        ),
        (
            '<w:rFonts w:asciiTheme="majorHAnsi" w:eastAsiaTheme="majorEastAsia"'
            ' w:hAnsiTheme="majorHAnsi" w:cstheme="majorBidi" /><w:b /><w:bCs /><w:i /><w:iCs />'
            f'<w:color w:val="{GREY}" /><w:sz w:val="22" /><w:szCs w:val="22" />'
        ),
    ),
    "BlockText": (
        (
            f'<w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="{RULE}" /></w:pBdr>'
            '<w:spacing w:before="80" w:after="140" /><w:ind w:left="360" w:right="360" />'
        ),
        f'<w:i /><w:iCs /><w:color w:val="{GREY}" />',
    ),
    "TOCHeading": (
        # Under the title, not on a page of its own like the Heading 1 it's based on
        (
            '<w:keepNext /><w:keepLines /><w:pageBreakBefore w:val="0" />'
            '<w:pBdr><w:bottom w:val="nil" /></w:pBdr>'
            '<w:spacing w:before="240" w:after="120" /><w:outlineLvl w:val="9" />'
        ),
        (
            '<w:rFonts w:asciiTheme="majorHAnsi" w:eastAsiaTheme="majorEastAsia"'
            ' w:hAnsiTheme="majorHAnsi" w:cstheme="majorBidi" /><w:b /><w:bCs />'
            f'<w:color w:val="{NAVY}" /><w:sz w:val="28" /><w:szCs w:val="28" />'
        ),
    ),
    "VerbatimChar": ("", '<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" /><w:sz w:val="19" />'),
    "Hyperlink": ("", f'<w:color w:val="{NAVY}" /><w:u w:val="single" />'),
}

TABLE = f"""<w:style w:type="table" w:default="1" w:styleId="Table">
    <w:name w:val="Table" />
    <w:basedOn w:val="TableNormal" />
    <w:semiHidden />
    <w:unhideWhenUsed />
    <w:qFormat />
    <w:pPr>
      <w:spacing w:before="40" w:after="40" w:line="240" w:lineRule="auto" />
    </w:pPr>
    <w:rPr>
      <w:sz w:val="19" />
      <w:szCs w:val="19" />
    </w:rPr>
    <w:tblPr>
      <w:tblInd w:w="0" w:type="dxa" />
      <w:tblBorders>
        <w:top w:val="single" w:sz="8" w:space="0" w:color="{NAVY}" />
        <w:bottom w:val="single" w:sz="8" w:space="0" w:color="{NAVY}" />
        <w:insideH w:val="single" w:sz="4" w:space="0" w:color="{RULE}" />
      </w:tblBorders>
      <w:tblCellMar>
        <w:top w:w="40" w:type="dxa" />
        <w:left w:w="100" w:type="dxa" />
        <w:bottom w:w="40" w:type="dxa" />
        <w:right w:w="100" w:type="dxa" />
      </w:tblCellMar>
    </w:tblPr>
    <w:tblStylePr w:type="firstRow">
      <w:rPr>
        <w:b />
        <w:bCs />
      </w:rPr>
      <w:tcPr>
        <w:tcBorders>
          <w:bottom w:val="single" w:sz="8" w:space="0" w:color="{NAVY}" />
        </w:tcBorders>
        <w:shd w:val="clear" w:color="auto" w:fill="{SHADE}" />
        <w:vAlign w:val="bottom" />
      </w:tcPr>
    </w:tblStylePr>
  </w:style>"""


def style_block(styles, style_id):
    match = re.search(rf'<w:style [^>]*w:styleId="{style_id}".*?</w:style>', styles, re.S)
    if not match:
        raise SystemExit(f"pandoc's reference.docx has no {style_id} style")
    return match


def restyle(styles):
    """styles.xml with loom's fonts, sizes and colors."""
    # Body text: Cambria 11pt, a little more line spacing
    styles = styles.replace(
        '<w:sz w:val="24" />\n        <w:szCs w:val="24" />\n        <w:lang',
        '<w:sz w:val="22" />\n        <w:szCs w:val="22" />\n        <w:lang',
        1,
    )
    styles = styles.replace(
        '<w:spacing w:after="200" />', '<w:spacing w:after="140" w:line="288" w:lineRule="auto" />'
    )
    for style_id, (ppr, rpr) in STYLES.items():
        block = style_block(styles, style_id).group(0)
        new = re.sub(r"\s*<w:pPr>.*?</w:pPr>", "", block, flags=re.S)
        new = re.sub(r"\s*<w:rPr>.*?</w:rPr>", "", new, flags=re.S)
        new = re.sub(r"\s*<w:numPr>.*?</w:numPr>", "", new, flags=re.S)
        props = ""
        if ppr:
            props += f"\n    <w:pPr>{ppr}</w:pPr>"
        if rpr:
            props += f"\n    <w:rPr>{rpr}</w:rPr>"
        new = re.sub(r"\s*</w:style>$", lambda m: props + "\n  </w:style>", new)
        styles = styles.replace(block, new)
    table = style_block(styles, "Table")
    return styles[: table.start()] + TABLE + styles[table.end() :]


def retheme(theme):
    """The theme's fonts: Calibri for headings, Cambria for body text."""
    for slot, font, panose in [
        ("major", "Calibri", "020F0502020204030204"),
        ("minor", "Cambria", "02040503050406030204"),
    ]:
        theme = re.sub(
            rf'(<a:{slot}Font>\s*<a:latin) typeface="[^"]*"(?: panose="[^"]*")?',
            rf'\1 typeface="{font}" panose="{panose}"',
            theme,
            count=1,
        )
    return theme


def main():
    try:
        default = subprocess.run(
            ["pandoc", "--print-default-data-file", "reference.docx"],
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as err:
        raise SystemExit(f"Unable to run pandoc: {err}")

    parts = {}
    with zipfile.ZipFile(BytesIO(default)) as source:
        for name in source.namelist():
            parts[name] = source.read(name)

    parts["word/styles.xml"] = restyle(parts["word/styles.xml"].decode("utf-8")).encode("utf-8")
    parts["word/theme/theme1.xml"] = retheme(parts["word/theme/theme1.xml"].decode()).encode()

    # Word fills in the table of contents when it opens the report
    settings = parts["word/settings.xml"].decode("utf-8")
    if "w:updateFields" not in settings:
        settings = settings.replace(
            "<w:footnotePr>", '<w:updateFields w:val="true" />\n  <w:footnotePr>', 1
        )
    parts["word/settings.xml"] = settings.encode("utf-8")

    document = parts["word/document.xml"].decode("utf-8")
    document = re.sub(r"<w:sectPr>.*?</w:sectPr>", SECTION, document, flags=re.S)
    parts["word/document.xml"] = document.encode("utf-8")
    parts["word/footer1.xml"] = FOOTER.encode("utf-8")

    rels = parts["word/_rels/document.xml.rels"].decode("utf-8")
    rels = rels.replace(
        "</Relationships>",
        (
            f'<Relationship Type="{FOOTER_REL}" Id="{FOOTER_ID}" Target="footer1.xml"'
            " /></Relationships>"
        ),
    )
    parts["word/_rels/document.xml.rels"] = rels.encode("utf-8")
    types = parts["[Content_Types].xml"].decode("utf-8")
    types = types.replace(
        "</Types>",
        f'<Override PartName="/word/footer1.xml" ContentType="{FOOTER_TYPE}" /></Types>',
    )
    parts["[Content_Types].xml"] = types.encode("utf-8")

    # Fixed timestamps and order, so the file only changes when its contents do
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target:
        names = ["[Content_Types].xml"] + sorted(n for n in parts if n != "[Content_Types].xml")
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            target.writestr(info, parts[name])
    OUT.write_bytes(out.getvalue())
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    sys.exit(main())
