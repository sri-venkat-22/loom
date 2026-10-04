"""
A report of a /project: what it set out to do, how each phase went, what it decided and
what it changed, in one document to read or hand in.

ProjectReport builds it as Markdown from the project's state and shared memory:

  - a cover: the idea, when the project started and finished, the models and loom's version
  - a timeline of the phases: status, verdict, runs, time, cost and fix rounds
  - an executive summary written by the weak model, when asked for
  - each phase's document, its headings one level down
  - the decisions, grouped by phase and kind
  - the files each phase's runs changed (git diff --stat)
  - an appendix with the project's history

export() writes it as Markdown, or converts it with pandoc to HTML, Word (styled by
loom/resources/report-reference.docx) or PDF, when pandoc has a PDF engine.
"""

import json
import logging
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import importlib_resources

from loom import __version__
from loom.memory import DECISION_KINDS, FOUNDER
from loom.orchestrator import (
    describe_metrics,
    format_cost,
    format_duration,
    read_project_file,
)
from loom.phases import DOCS_DIR, PHASES, PHASES_BY_KEY, SPEC
from loom.repo import EMPTY_TREE

FORMATS = ("md", "html", "docx", "pdf")
DEFAULT_FORMAT = "md"
# Where /project report writes, without its extension
DEFAULT_PATH = f"{DOCS_DIR}/report"
REFERENCE_DOCX = "report-reference.docx"
# The PDF engines pandoc can use, the ones loom prefers first
PDF_ENGINES = ("typst", "tectonic", "xelatex", "lualatex", "pdflatex", "weasyprint", "wkhtmltopdf")

# The longest history note and decision shown in a table
MAX_NOTE_CHARS = 200
# How much of the report the weak model reads to write the summary
MAX_SUMMARY_INPUT = 60_000

KIND_TITLES = {
    "decision": "Decisions",
    "approved": "Approvals",
    "edited": "Edits by hand",
    "rejected": "Changes asked for",
    "sent back": "Sent back",
    "override": "Overrides",
    "check": "Template checks",
}
STATUS_LABELS = dict(
    pending="pending", running="interrupted", review="waiting for review", approved="approved"
)

SUMMARY_PROMPT = """Write a one-page executive summary of the software project in this report, for a reader who won't read the rest: what was built and for whom, the key decisions and why, how it was tested and how that went, and what remains to do. Use plain Markdown paragraphs and at most one short bullet list; no headings. Only state what the report says.

<report>
{report}
</report>"""  # noqa: E501


class ReportError(Exception):
    pass


def format_time(stamp):
    """An ISO timestamp from the project's state, like 4 October 2026, 10:12."""
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return stamp or ""
    return f"{when.day} {when:%B %Y, %H:%M}"


def one_line(text, limit=None):
    """text on one line, cut to at most limit characters."""
    text = " ".join(str(text or "").split())
    if limit and len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def cell(text):
    """Text for a Markdown table cell: one line, with its pipes escaped."""
    return one_line(text).replace("|", "\\|")


def table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows]
    return "\n".join(lines)


HEADING_RE = re.compile(r"^(#{1,6})(?=\s)")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def shift_headings(text, levels=1):
    """text with every Markdown heading levels deeper (at most level 6), outside code."""
    lines = []
    fenced = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced:
            line = HEADING_RE.sub(lambda m: "#" * min(6, len(m.group(1)) + levels), line)
        lines.append(line)
    return "\n".join(lines)


def yaml_string(text):
    # A JSON string is a valid YAML double-quoted scalar
    return json.dumps(text, ensure_ascii=False)


class ProjectReport:
    """The report of the project in root, from its ProjectState and decisions. git is
    the project's GitPython Repo, for the changes each run made, and models names the
    models that worked on it."""

    def __init__(self, root, state, decisions, git=None, models=None):
        self.root = Path(root)
        self.state = state
        self.decisions = decisions
        self.git = git
        self.models = models or {}

    @classmethod
    def from_orchestrator(cls, orchestrator):
        coder = orchestrator.coder
        model = coder.main_model
        models = dict(main=model.name)
        if getattr(model, "weak_model", None) and model.weak_model is not model:
            models["weak"] = model.weak_model.name
        git = coder.repo.repo if coder.repo else None
        return cls(
            orchestrator.root, orchestrator.state, orchestrator.memory.decisions(), git, models
        )

    @property
    def title(self):
        idea = self.state.idea.split("\n", 1)[0].strip()
        if len(idea) > 90:
            idea = idea[:89] + "…"
        return f"Project report: {idea}"

    def markdown(self, summary=None):
        """The whole report, as Markdown with a YAML title block."""
        parts = [self.front_matter(), self.cover()]
        if summary:
            parts.append(f"# Executive summary\n\n{summary.strip()}")
        parts += [
            self.timeline(),
            self.documents(),
            self.decisions_log(),
            self.changes(),
            self.history(),
        ]
        return "\n\n".join(part for part in parts if part) + "\n"

    def front_matter(self):
        lines = [
            "---",
            f"title: {yaml_string(self.title)}",
            f"subtitle: {yaml_string('Built with loom, from idea to launch')}",
            f"date: {yaml_string(format_time(self.state.data.get('created')).split(',')[0])}",
            'toc-title: "Contents"',
            "---",
        ]
        return "\n".join(lines)

    # The cover

    def finished(self):
        """When the project finished, or where it is."""
        current = self.state.current
        if current:
            return f"Not yet: in {current.title}"
        return format_time(self.state.phase_data(PHASES[-1].key).get("approved")) or "Yes"

    def cover(self):
        totals = self.state.totals()
        idea = self.state.idea.strip()
        lines = ["# Overview", "", "> " + idea.replace("\n", "\n> "), ""]
        fields = [
            ("Started", format_time(self.state.data.get("created"))),
            ("Finished", self.finished()),
        ]
        if self.models.get("main"):
            fields.append(("Model", self.models["main"]))
        if self.models.get("weak"):
            fields.append(("Weak model", self.models["weak"]))
        template = self.state.template_info
        if template:
            fields.append(("Template", f"{template['name']} ({template.get('source')})"))
        if self.state.tdd:
            fields.append(("Building", "test-driven"))
        fields += self.shipped()
        fields.append(("loom", __version__))
        if totals["runs"]:
            fields.append(("Effort", describe_metrics(totals)))
            fields.append(
                (
                    "Tokens",
                    f"{totals['tokens_sent']:,} sent, {totals['tokens_received']:,} received",
                )
            )
            fields.append(("Commits", str(totals["commits"])))
        lines += [f"- **{name}:** {value}" for name, value in fields]
        return "\n".join(lines)

    def shipped(self):
        """The cover's fields for the latest /project ship."""
        record = self.state.data.get("deployment")
        if not record:
            return []
        fields = []
        if record.get("url"):
            smoke = "smoke test passed" if record.get("healthy") else "smoke test failed"
            fields.append(
                (
                    "Deployed",
                    f"{record['url']} ({record.get('title')}, {record.get('app')}; {smoke})",
                )
            )
        if record.get("rolled_back_to"):
            fields.append(("Rolled back to", record["rolled_back_to"]))
        if record.get("pr"):
            fields.append(("Pull request", record["pr"]))
        if record.get("release"):
            fields.append(("Release", f"{record.get('tag')}: {record['release']}"))
        return fields

    # The timeline

    def timeline(self):
        rows = []
        for phase in PHASES:
            data = self.state.phase_data(phase.key)
            status = STATUS_LABELS.get(data["status"], data["status"])
            if data["status"] == "pending" and data.get("stale"):
                status = "to redo"
            metrics = self.state.metrics(phase.key)
            rows.append(
                [
                    str(phase.number),
                    phase.title,
                    status,
                    data.get("verdict") or "",
                    str(metrics["runs"]),
                    format_duration(metrics["seconds"]) if metrics["runs"] else "",
                    format_cost(metrics["cost"]) if metrics["runs"] else "",
                    str(self.state.fix_rounds(phase.key) or ""),
                ]
            )
        header = ["#", "Phase", "Status", "Verdict", "Runs", "Time", "Cost", "Fix rounds"]
        return "# Timeline\n\n" + table(header, rows)

    # The documents

    def document_text(self, phase):
        try:
            text = read_project_file(self.root, self.root / phase.document)
        except OSError:
            return ""
        return (text or "").strip()

    def documents(self):
        parts = []
        for phase in PHASES:
            text = self.document_text(phase)
            heading = f"# {phase.number}. {phase.title}"
            where = f"*The {phase.document_title}, `{phase.document}`.*"
            if not text:
                parts.append(f"{heading}\n\n*No {phase.document_title} yet.*")
                continue
            parts.append(f"{heading}\n\n{where}{self.checks(phase)}\n\n{shift_headings(text)}")
            if phase.key == "building":
                parts += [part for part in [self.packages()] if part]
                parts += self.test_driven()
        return "\n\n".join(parts)

    def packages(self):
        """Parallel Building's work packages, as a report part, or None."""
        packages = self.state.phase_data("building").get("packages")
        if not packages:
            return None
        rows = [
            [
                pid,
                record.get("title") or "",
                str(record.get("wave") or ""),
                record.get("status", ""),
                str(record.get("attempts") or ""),
                format_cost(record.get("cost") or 0),
            ]
            for pid, record in packages.items()
        ]
        header = ["Package", "Title", "Wave", "Status", "Attempts", "Cost"]
        return "**Parallel builders' work packages:**\n\n" + table(header, rows)

    def test_driven(self):
        """Test-driven Building's acceptance test plan and attempts, as report parts."""
        parts = []
        spec = self.state.phase_data("building").get("spec") or {}
        text = self.document_text(SPEC)
        if text:
            locked = ", ".join(f"`{path}`" for path in spec.get("locked") or {}) or "none"
            parts.append(
                f"*The {SPEC.document_title}, `{SPEC.document}`. Locked tests: {locked}.*"
                f"\n\n{shift_headings(text)}"
            )
        for run in self.state.run_log("building"):
            attempts = run.get("attempts")
            if not attempts:
                continue
            rows = [
                [
                    str(a["attempt"]),
                    {True: "pass", False: "fail"}.get(a["passed"], "not run"),
                    format_duration(a.get("seconds")),
                    format_cost(a.get("cost") or 0),
                    ", ".join(a.get("restored") or []),
                ]
                for a in attempts
            ]
            table_md = table(
                ["Attempt", "Tests", "Time", "Cost so far", "Locked tests put back"], rows
            )
            if run.get("package"):
                what = f", the {run['package']} package"
            elif run.get("step") and run["step"] != "build":
                what = f", {run['step']}"
            else:
                what = ""
            parts.append(f"**Test-driven Building, run {run.get('run')}{what}:**\n\n{table_md}")
            if run.get("skips"):
                skips = "\n".join(f"- `{skip}`" for skip in run["skips"])
                parts.append(f"Test changes that skip tests or expect them to fail:\n\n{skips}")
        return parts

    def checks(self, phase):
        """The template's checks after the phase's last run, as a paragraph, or ""."""
        checks = self.state.phase_data(phase.key).get("checks")
        if not checks:
            return ""
        marks = [f"{'✓' if c['passed'] else '✗'} `{c['command']}`" for c in checks]
        return "\n\n**Template checks:** " + ", ".join(marks)

    # Decisions

    def decisions_log(self):
        if not self.decisions:
            return "# Decisions\n\n*No decisions were recorded.*"
        parts = ["# Decisions"]
        groups = [(None, "Project")] + [(phase.key, phase.title) for phase in PHASES]
        for key, title in groups:
            mine = [d for d in self.decisions if d.get("phase") == key]
            if not mine:
                continue
            parts.append(f"## {title}")
            for kind in DECISION_KINDS:
                found = [d for d in mine if d.get("kind") == kind]
                if not found:
                    continue
                lines = [f"**{KIND_TITLES[kind]}**", ""]
                lines += [f"- {self.describe_decision(d)}" for d in found]
                parts.append("\n".join(lines))
        return "\n\n".join(parts)

    def describe_decision(self, decision):
        who = "the founder" if decision.get("source") == FOUNDER else decision.get("source")
        line = one_line(decision.get("text"))
        if decision.get("reason"):
            line += f" *Why: {one_line(decision['reason'])}*"
        return f"{line} — {who}, {format_time(decision.get('time'))}"

    # Changes

    def diff_stat(self, base, head):
        if not self.git or not head:
            return ""
        try:
            return self.git.git.diff("--stat=100", base or EMPTY_TREE, head).strip()
        except Exception:
            return ""

    def changes(self):
        parts = ["# Changes"]
        any_changes = False
        for phase in PHASES:
            runs = [run for run in self.state.run_log(phase.key) if run.get("commits")]
            if not runs:
                continue
            section = [f"## {phase.number}. {phase.title}"]
            for run in runs:
                stat = self.diff_stat(run.get("base"), run.get("head"))
                if not stat:
                    continue
                span = f"{(run.get('base') or 'start')[:7]}..{run['head'][:7]}"
                commits = run["commits"]
                step = f", {run['step']}" if run.get("step") else ""
                section.append(
                    f"Run {run.get('run')}{step}: {commits} commit{'s' if commits != 1 else ''}"
                    f" ({span})\n\n```\n{stat}\n```"
                )
            if len(section) > 1:
                any_changes = True
                parts += section
        if not any_changes:
            parts.append("*No commits were recorded.*")
        return "\n\n".join(parts)

    # History

    def history(self):
        rows = []
        for entry in self.state.history:
            phase = PHASES_BY_KEY.get(entry.get("phase"))
            rows.append(
                [
                    format_time(entry.get("time")),
                    phase.title if phase else "",
                    entry.get("event", ""),
                    one_line(entry.get("note"), MAX_NOTE_CHARS),
                ]
            )
        return "# Appendix: project history\n\n" + table(["Time", "Phase", "Event", "Note"], rows)

    # The executive summary

    def write_summary(self, model, markdown=None):
        """A one-page executive summary of the report, written by model (the weak
        model). Raises ReportError if it fails."""
        report = markdown or self.markdown()
        if len(report) > MAX_SUMMARY_INPUT:
            report = report[:MAX_SUMMARY_INPUT] + "\n\n[The rest of the report is cut.]"
        messages = [dict(role="user", content=SUMMARY_PROMPT.format(report=report))]
        try:
            summary = model.simple_send_with_retries(messages)
        except Exception as err:
            raise ReportError(f"{model.name} couldn't write the summary: {err}")
        if not summary or not summary.strip():
            raise ReportError(f"{model.name} wrote no summary.")
        # The summary goes under its own heading
        return shift_headings(summary.strip(), 2)


# Converting the report


def pandoc_version():
    """The version of pandoc pypandoc finds, or None when there's none it can run."""
    import pypandoc

    # pypandoc logs every pandoc it tries and can't run
    log = logging.getLogger("pypandoc")
    level = log.level
    log.setLevel(logging.CRITICAL)
    try:
        return pypandoc.get_pandoc_version()
    except OSError:
        return None
    finally:
        log.setLevel(level)


def pdf_engine():
    """The first PDF engine pandoc can use that's installed, or None."""
    for engine in PDF_ENGINES:
        if shutil.which(engine):
            return engine
    return None


def reference_docx():
    return importlib_resources.files("loom.resources").joinpath(REFERENCE_DOCX)


def convert(markdown, fmt, path, engine=None):
    """Convert markdown to fmt (html, docx or pdf) at path with pandoc."""
    import pypandoc

    args = ["--standalone", "--toc", "--toc-depth=1"]
    if fmt == "pdf":
        args.append(f"--pdf-engine={engine}")
    # Agents write GitHub Markdown, which pandoc's own Markdown reads, with the title
    # block. Without smart quotes and dashes, so --yes-always stays as it is.
    source = "markdown-implicit_figures-smart"
    to = "html5" if fmt == "html" else fmt
    with importlib_resources.as_file(reference_docx()) as reference:
        if fmt == "docx":
            args.append(f"--reference-doc={reference}")
        try:
            pypandoc.convert_text(
                markdown, to, format=source, outputfile=str(path), extra_args=args
            )
        except (OSError, RuntimeError) as err:
            raise ReportError(f"pandoc couldn't write {path.name}: {str(err).strip()}")


HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ font: 16px/1.6 Georgia, "Times New Roman", serif; color: #1f2328; max-width: 46rem;
  margin: 2.5rem auto; padding: 0 1rem; }}
h1, h2, h3, h4 {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  line-height: 1.25; }}
h1 {{ border-bottom: 1px solid #d0d7de; padding-bottom: .3rem; margin-top: 2.5rem; }}
table {{ border-collapse: collapse; margin: 1rem 0; font-size: .9rem; }}
th, td {{ border: 1px solid #d0d7de; padding: .3rem .6rem; text-align: left; }}
th {{ background: #f6f8fa; }}
code, pre {{ font: .85rem/1.45 Menlo, Consolas, monospace; background: #f6f8fa; }}
pre {{ padding: .8rem; overflow: auto; }}
blockquote {{ margin: 0; padding: 0 1rem; color: #59636e; border-left: .25rem solid #d0d7de; }}
.subtitle {{ color: #59636e; margin-top: -.5rem; }}
</style>
</head>
<body>
<header><h1 class="title">{title}</h1><p class="subtitle">{subtitle}</p></header>
{body}
</body>
</html>
"""


def front_matter(markdown):
    """(the YAML title block's fields, the rest of the Markdown)."""
    match = re.match(r"---\n(.*?)\n---\n", markdown, re.S)
    if not match:
        return {}, markdown
    fields = {}
    for line in match.group(1).splitlines():
        name, _, value = line.partition(":")
        try:
            fields[name.strip()] = json.loads(value.strip())
        except ValueError:
            fields[name.strip()] = value.strip()
    return fields, markdown[match.end() :]


def html_without_pandoc(markdown):
    """The report as an HTML page, made by markdown-it (which rich brings) when there's
    no pandoc."""
    from html import escape

    from markdown_it import MarkdownIt

    fields, body = front_matter(markdown)
    renderer = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    return HTML_PAGE.format(
        title=escape(fields.get("title", "Project report")),
        subtitle=escape(fields.get("subtitle", "")),
        body=renderer.render(body),
    )


def export(markdown, fmt, path, warn=None):
    """Write the report's markdown at path as fmt, which is md, html, docx or pdf. When
    the format needs pandoc (or a PDF engine) that isn't installed, warn(message) says so
    and loom writes what it can instead: Word and HTML for a PDF, HTML for Word.

    Returns the paths written."""
    if fmt not in FORMATS:
        raise ReportError(f"Unknown format {fmt!r}: use one of {', '.join(FORMATS)}")
    warn = warn or (lambda message: None)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "md":
        path.write_bytes(markdown.encode("utf-8"))
        return [path]

    has_pandoc = pandoc_version() is not None
    if fmt == "html":
        if has_pandoc:
            convert(markdown, "html", path)
        else:
            path.write_bytes(html_without_pandoc(markdown).encode("utf-8"))
        return [path]

    if not has_pandoc:
        warn(
            f"Writing {fmt} needs pandoc, which isn't installed"
            " (https://pandoc.org/installing.html), so loom wrote the report as HTML instead."
        )
        return export(markdown, "html", path.with_suffix(".html"))

    if fmt == "docx":
        convert(markdown, "docx", path)
        return [path]

    engine = pdf_engine()
    if engine:
        convert(markdown, "pdf", path, engine)
        return [path]
    warn(
        "Writing a PDF needs a PDF engine for pandoc, like typst (https://typst.app), and none"
        " is installed, so loom wrote the report as Word and HTML instead."
    )
    return export(markdown, "docx", path.with_suffix(".docx")) + export(
        markdown, "html", path.with_suffix(".html")
    )


def export_bytes(markdown, fmt):
    """The report as fmt, as (bytes, the format it's really in): a PDF can't be made
    without a PDF engine, which raises ReportError, and Word falls back to HTML."""
    with tempfile.TemporaryDirectory() as tmp:
        notes = []
        written = export(markdown, fmt, Path(tmp) / f"report.{fmt}", warn=notes.append)
        if fmt == "pdf" and written[0].suffix != ".pdf":
            raise ReportError(notes[0] if notes else "Unable to write a PDF.")
        path = written[0]
        return path.read_bytes(), path.suffix[1:]


def default_path(root, fmt):
    return Path(root) / f"{DEFAULT_PATH}.{fmt}"
