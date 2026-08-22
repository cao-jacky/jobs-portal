#!/usr/bin/env python3
"""Jobs Portal — a small read/write web service over a folder of position notes.

Serves a single-page ledger of every markdown note under $JOBS_DIR/Positions,
with an inline viewer and an editor that writes back to the original files.

Standard library only, apart from an optional `markdown` package used for
rendering note bodies. Without it a built-in subset renderer is used instead.

Environment:
    JOBS_DIR      folder containing Positions/ (default: cwd)
    PORT          listen port (default: 8080)
    HOST          bind address (default: 0.0.0.0)
    AUTH_TOKEN    if set, every request must carry it as a Bearer token or a
                  `token` query parameter; the page prompts once and stores it
    READ_ONLY     set to 1 to refuse all writes
    BACKUPS       set to 0 to disable pre-write backups (default: on)
"""
from __future__ import annotations

import datetime
import html as html_mod
import json
import os
import re
import io
import shutil
import subprocess
import sys
import tempfile
import zipfile
import unicodedata
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #

JOBS_DIR = Path(os.environ.get("JOBS_DIR", ".")).resolve()
POSITIONS_DIR = JOBS_DIR / "Positions"
BACKUP_DIR = JOBS_DIR / ".portal-backups"
HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "templates" / "index.html"

PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
READ_ONLY = os.environ.get("READ_ONLY", "").strip() in {"1", "true", "yes"}
BACKUPS = os.environ.get("BACKUPS", "1").strip() not in {"0", "false", "no"}

FIELD_ORDER = [
    "job_title",
    "company",
    "location",
    "deadline",
    "job_status",
    "date_added",
    "date_applied",
    "date_rejected",
    "link",
]
STATUSES = [
    "Not applied",
    "Applied",
    "Interview Invitation",
    "Interviewed",
    "Rejected",
    "Not eligible",
    "Skipped",
]
NULLISH = {"", "null", "none", "n/a", "tbd", "-"}

# Sibling folders holding the documents written for a position. Matched by
# normalised folder name so the accent in "Résumés" cannot break lookup.
ARTEFACT_FOLDERS = {
    "cover letters": "letter",
    "coverletters": "letter",
    "résumés": "resume",
    "resumes": "resume",
    "résumes": "resume",
    "cvs": "cv",
}
ARTEFACT_EXTENSIONS = {".pdf", ".odt", ".docx", ".doc", ".md", ".txt", ".rtf"}
CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".rtf": "application/rtf",
}

def normalise_body(text: str) -> str:
    """Flatten cosmetic indentation so pasted adverts do not become code blocks.

    Job adverts copied out of a browser routinely arrive with every bullet
    indented four spaces, which markdown reads as a literal code block. Fenced
    blocks are left exactly as written, so real code in a note still survives.
    """
    out: list[str] = []
    fenced = False
    for line in text.replace("\r\n", "\n").split("\n"):
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
            out.append(line)
            continue
        if fenced or not line.strip():
            out.append(line)
            continue
        stripped = line.lstrip(" \t")
        indent = len(line) - len(stripped)
        if indent >= 4:
            is_list = bool(re.match(r"^([-*+]|\d+[.)])\s", stripped))
            out.append(("  " if is_list and indent >= 8 else "") + stripped)
        else:
            out.append(line)
    return "\n".join(out)


try:  # pragma: no cover - depends on install
    import markdown as _markdown

    def render_markdown(text: str) -> str:
        return _markdown.markdown(
            normalise_body(text), extensions=["tables", "fenced_code", "sane_lists", "nl2br"]
        )

    MARKDOWN_BACKEND = f"markdown {_markdown.__version__}"
except ImportError:  # pragma: no cover - fallback path
    def render_markdown(text: str) -> str:
        return _fallback_markdown(normalise_body(text))

    MARKDOWN_BACKEND = "built-in subset renderer"


# --------------------------------------------------------------------------- #
# markdown fallback (only used when the markdown package is absent)
# --------------------------------------------------------------------------- #

def _inline(text: str) -> str:
    out = html_mod.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", out)
    out = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2" target="_blank" rel="noopener">\1</a>',
        out,
    )
    out = re.sub(
        r"(?<!\"|>)(https?://[^\s<)]+)",
        r'<a href="\1" target="_blank" rel="noopener">\1</a>',
        out,
    )
    return out


def _fallback_markdown(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    parts: list[str] = []
    buffer: list[str] = []
    list_open: str | None = None

    def flush_paragraph() -> None:
        if buffer:
            parts.append("<p>" + "<br>".join(_inline(b) for b in buffer) + "</p>")
            buffer.clear()

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            parts.append(f"</{list_open}>")
            list_open = None

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            close_list()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush_paragraph()
            close_list()
            level = len(heading.group(1))
            parts.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            flush_paragraph()
            close_list()
            parts.append("<hr>")
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        ordered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if bullet or ordered:
            flush_paragraph()
            want = "ul" if bullet else "ol"
            if list_open != want:
                close_list()
                parts.append(f"<{want}>")
                list_open = want
            content = (bullet or ordered).group(1)
            parts.append(f"<li>{_inline(content)}</li>")
            continue
        close_list()
        buffer.append(stripped)

    flush_paragraph()
    close_list()
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# note parsing and writing
# --------------------------------------------------------------------------- #

def clean(value: str | None) -> str | None:
    value = (value or "").strip().strip('"').strip("'")
    return None if value.lower() in NULLISH else value


def parse_date(value: str | None):
    if not value:
        return None
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if not match:
        return None
    try:
        return datetime.date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


FRONT_RE = re.compile(r"^(---[ \t]*\n)(.*?)(\n---[ \t]*\n?)", re.S)
KEY_RE = re.compile(r"^([A-Za-z0-9_.\-]+)[ \t]*:(.*)$")


def parse_front(block: str) -> dict[str, dict]:
    """Map lowercased key -> {raw_key, value, start, end} over the block's lines.

    `start`/`end` are inclusive line indices, so a value wrapped across several
    lines is tracked as one field and can be replaced as a unit.
    """
    lines = block.split("\n")
    fields: dict[str, dict] = {}
    previous: str | None = None
    for index, line in enumerate(lines):
        if line[:1] in (" ", "\t") and previous is not None:
            field = fields[previous]
            field["end"] = index
            field["value"] = f"{field['value']} {line.strip()}".strip()
            continue
        match = KEY_RE.match(line)
        if match:
            key = match.group(1)
            previous = key.lower()
            fields[previous] = {
                "raw_key": key,
                "value": match.group(2).strip(),
                "start": index,
                "end": index,
            }
        else:
            previous = None
    return fields


def split_note(text: str) -> tuple[dict[str, str | None], list[str], str]:
    """Return (cleaned frontmatter, key order, body) for display and stats."""
    match = FRONT_RE.match(text)
    if not match:
        return {}, [], text
    fields = parse_front(match.group(2))
    order = sorted(fields, key=lambda k: fields[k]["start"])
    front = {key: clean(fields[key]["value"]) for key in order}
    return front, order, text[match.end():]


def apply_edits(text: str, edits: dict[str, str | None], new_body: str | None) -> str:
    """Rewrite only what changed, leaving every other byte of the note alone.

    Untouched fields keep their original key casing, spacing, and literal value
    (including `null`), and the body is left byte-identical unless replaced.
    """
    match = FRONT_RE.match(text)
    if not match:
        # No frontmatter to patch, so build one and keep the existing text as body.
        head = ["---"]
        for key in FIELD_ORDER:
            value = edits.get(key)
            head.append(f"{key}: {value}" if value else f"{key}:")
        for key, value in edits.items():
            if key not in FIELD_ORDER:
                head.append(f"{key}: {value}" if value else f"{key}:")
        head.append("---")
        body = new_body if new_body is not None else text
        if not body.startswith("\n"):
            body = "\n" + body
        return "\n".join(head) + body

    prefix, block, suffix = match.group(1), match.group(2), match.group(3)
    body = text[match.end():] if new_body is None else new_body
    lines = block.split("\n")
    fields = parse_front(block)

    pending: list[tuple[int, int, str | None]] = []   # (start, end, replacement)
    appended: list[str] = []
    for key, value in edits.items():
        value = (value or "").strip()
        field = fields.get(key)
        if field is None:
            appended.append(f"{key}: {value}" if value else f"{key}:")
            continue
        old = (field["value"] or "").strip()
        if old == value:
            continue                                   # unchanged, do not touch the line
        if old.lower() in NULLISH and value.lower() in NULLISH:
            continue                                   # `null` and empty both mean nothing
        raw_key = field["raw_key"]
        pending.append((field["start"], field["end"], f"{raw_key}: {value}" if value else f"{raw_key}:"))

    for start, end, replacement in sorted(pending, key=lambda item: -item[0]):
        lines[start:end + 1] = [replacement]
    lines.extend(appended)

    if new_body is not None:
        body = body.replace("\r\n", "\n")
        if not body.startswith("\n"):
            body = "\n" + body
        if not body.endswith("\n"):
            body += "\n"
    return prefix + "\n".join(lines) + suffix + body


def compose_note(front: dict[str, str | None], order: list[str], body: str) -> str:
    """Build a brand-new note. Only used when creating, never when editing."""
    keys = [k for k in order if k in front]
    keys += [k for k in front if k not in keys]
    lines = ["---"]
    for key in keys:
        value = front.get(key)
        lines.append(f"{key}: {value}" if value else f"{key}:")
    lines.append("---")
    body = body.replace("\r\n", "\n")
    if not body.startswith("\n"):
        body = "\n" + body
    if not body.endswith("\n"):
        body += "\n"
    return "\n".join(lines) + body


def safe_path(rel: str) -> Path:
    """Resolve a client-supplied path, refusing anything outside Positions/."""
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        raise ValueError("path is required")
    candidate = (JOBS_DIR / rel).resolve()
    try:
        candidate.relative_to(POSITIONS_DIR)
    except ValueError as exc:
        raise ValueError("path must be inside Positions/") from exc
    if candidate.suffix.lower() != ".md":
        raise ValueError("only .md notes can be opened")
    return candidate


def relative(path: Path) -> str:
    return path.relative_to(JOBS_DIR).as_posix()


def safe_doc_path(rel: str) -> Path:
    """Resolve a document path, refusing anything outside the document folders."""
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        raise ValueError("path is required")
    candidate = (JOBS_DIR / rel).resolve()
    if candidate.suffix.lower() not in ARTEFACT_EXTENSIONS:
        raise ValueError("that file type cannot be served")
    for folder in artefact_dirs():
        try:
            candidate.relative_to(folder)
        except ValueError:
            continue
        if not candidate.is_file():
            raise ValueError("file not found")
        return candidate
    raise ValueError("path must be inside a document folder")


def enrich(row: dict, today: datetime.date) -> dict:
    applied = parse_date(row["applied"])
    rejected = parse_date(row["rejected"])
    deadline = parse_date(row["deadline"])
    row["daysToOutcome"] = (rejected - applied).days if applied and rejected else None
    row["daysOpen"] = (today - applied).days if applied and not rejected else None
    row["daysToDeadline"] = (deadline - today).days if deadline else None
    row["appliedMonth"] = row["applied"][:7] if applied else None
    return row


def fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold().strip()


def artefact_dirs() -> dict[Path, str]:
    """Locate the document folders that sit beside Positions/."""
    found: dict[Path, str] = {}
    try:
        for child in JOBS_DIR.iterdir():
            if child.is_dir():
                kind = ARTEFACT_FOLDERS.get(fold(child.name))
                if kind:
                    found[child] = kind
    except OSError:
        pass
    return found


def artefact_index() -> dict[str, list[dict]]:
    """Map a note's filename stem to the documents written for that position.

    Two naming conventions are in use, `Company - Title.pdf` and
    `Cover Letter - Company - Title.pdf`, so a stem matches when it equals the
    note's stem or ends with it.
    """
    index: dict[str, list[dict]] = {}
    for folder, kind in artefact_dirs().items():
        for path in folder.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in ARTEFACT_EXTENSIONS:
                continue
            if path.name.startswith((".", "~")):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            index.setdefault(fold(path.stem), []).append({
                "kind": kind,
                "path": relative(path),
                "name": path.name,
                "ext": path.suffix.lower().lstrip("."),
                "bytes": stat.st_size,
                "mtime": stat.st_mtime,
            })
    return index


def docs_for(stem: str, index: dict[str, list[dict]]) -> list[dict]:
    key = fold(stem)
    out: list[dict] = []
    for candidate, entries in index.items():
        if candidate == key or candidate.endswith(key):
            out.extend(entries)
    out.sort(key=lambda d: (d["kind"], d["ext"] != "pdf", d["name"]))
    return out


def row_for(path: Path, today: datetime.date, index: dict[str, list[dict]] | None = None) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    front, _order, body = split_note(text)
    docs = docs_for(path.stem, index if index is not None else artefact_index())
    return enrich(
        {
            "path": relative(path),
            "docs": docs,
            "company": front.get("company") or "Unknown",
            "title": front.get("job_title") or "Unknown",
            "location": front.get("location") or "Unknown",
            "status": front.get("job_status") or "Unknown",
            "added": front.get("date_added"),
            "applied": front.get("date_applied"),
            "rejected": front.get("date_rejected"),
            "deadline": front.get("deadline"),
            "link": front.get("link"),
            "year": path.parent.name,
            "assessed": "## Suitability" in body,
            "words": len(body.split()),
        },
        today,
    )


def list_notes() -> list[dict]:
    today = datetime.date.today()
    index = artefact_index()
    rows = []
    if POSITIONS_DIR.is_dir():
        for path in sorted(POSITIONS_DIR.rglob("*.md")):
            try:
                rows.append(row_for(path, today, index))
            except OSError as exc:
                print(f"skipping {path}: {exc}", file=sys.stderr)
    rows.sort(key=lambda r: (r["applied"] or r["deadline"] or "0000", r["company"]), reverse=True)
    return rows


def backup(path: Path) -> str | None:
    if not BACKUPS or not path.exists():
        return None
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / path.relative_to(JOBS_DIR).parent / f"{path.stem}.{stamp}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return relative(target)


def write_note(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def slug_filename(company: str, title: str) -> str:
    name = f"{company.strip()} - {title.strip()}"
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return f"{name}.md"


# Coordinates for locations seen in the notes, plus places likely to turn up.
# Anything unmatched is reported separately rather than guessed at.
GEOCODES = {
    "helsinki": (60.1699, 24.9384),
    "espoo": (60.2055, 24.6559),
    "otaniemi": (60.1841, 24.8301),
    "keilaniemi": (60.1755, 24.8329),
    "vantaa": (60.2941, 25.0400),
    "jorvas": (60.1533, 24.5300),
    "kirkkonummi": (60.1226, 24.4382),
    "hyvinkää": (60.6306, 24.8592),
    "tampere": (61.4978, 23.7610),
    "turku": (60.4518, 22.2666),
    "oulu": (65.0121, 25.4651),
    "jyväskylä": (62.2426, 25.7473),
    "lahti": (60.9827, 25.6612),
    "vaasa": (63.0951, 21.6165),
    "stockholm": (59.3293, 18.0686),
    "gothenburg": (57.7089, 11.9746),
    "oslo": (59.9139, 10.7522),
    "copenhagen": (55.6761, 12.5683),
    "trondheim": (63.4305, 10.3951),
    "tallinn": (59.4370, 24.7536),
    "berlin": (52.5200, 13.4050),
    "hamburg": (53.5511, 9.9937),
    "munich": (48.1351, 11.5820),
    "prague": (50.0755, 14.4378),
    "wroclaw": (51.1079, 17.0385),
    "london": (51.5072, -0.1276),
    "durham": (54.7761, -1.5733),
    "palo alto": (37.4419, -122.1430),
}
PLACELESS = {"remote", "hybrid", "anywhere", "eu", "europe", "nordics", "unknown", ""}

# Words that say nothing about what a role wants. Grouped so the list stays
# maintainable rather than becoming an unexplained blob.
FUNCTION_WORDS = """
a an the and or but if then than that this these those of in on at to for with without from by as
is are was were be been being am have has had having do does did doing will would shall should can
could may might must ought you your yours yourself we our ours us ourselves they their theirs them
he him his she her hers it its i me my mine who whom whose which what when where why how all any
both each few more most others some such no nor not only own same so too very just also about into
over under again further once here there while during before after above below up down out off
between through against because both either neither every another via per within across upon
""".split()

RECRUITING_BOILERPLATE = """
work working works worked role roles job jobs position positions team teams company companies
experience experienced description descriptions suitability verdict requirement requirements
responsibility responsibilities task tasks offer offers offering support supporting apply
application applications applicant candidate candidates skill skills ability abilities
opportunity opportunities join joining looking seeking hire hiring recruitment recruiting
salary benefits perks culture office hybrid remote onsite fulltime permanent contract
employer employee employees colleague colleagues staff people person persons individual
you'll we'll we're it's don't
please thank thanks welcome forward hear us contact email phone send submit link deadline
period process interview interviews cv resume letter
"""

FILLER = """
part parts one two three four five first second third next last best better good great strong
able ensure ensuring provide providing providing based various different every many much need
needs needed want wants may must plus level levels environment environments world global
international leading market business businesses customer customers client clients service
services product products project projects process processes solution solutions
new using use used uses within across including include includes etc well also relevant
required key focus impact complex modern digital diverse background take bring together grow
growing value values field fields information related area areas way ways thing things
lot make makes making get gets getting go goes going come comes see look looks
day days week weeks month months year years time times now today
other others another like likes similar help helps helping contribute contributing
become becoming keep keeping bring brings given give gives given e.g i.e ie eg
across around along among since about upon whether either whatever however therefore
location locations career careers professional professionals interest interested real
find finds expect expects expected challenge challenges enable enables enabling
create creates creating deliver delivers delivered delivery drive drives driven driving
improve improves improving implement implements lead leads led expert experts
"""

WEB_NOISE = """
http https www com net org fi se no dk eu io ai dev co uk html php aspx utm src ref
linkedin greenhouse workday teamtailor smartrecruiters lever jobvite indeed glassdoor
oyj plc ltd inc gmbh
"""

STOPWORDS = set(FUNCTION_WORDS) | set(RECRUITING_BOILERPLATE.split()) \
    | set(FILLER.split()) | set(WEB_NOISE.split())

TLD_RE = re.compile(r"\.(com|net|org|fi|se|no|dk|eu|io|ai|dev|co|uk|de|us|info|jobs)$")
TOKEN_RE = re.compile(r"[a-zäöå][a-zäöå+#.\-]{2,}")


def strip_noise(text: str) -> str:
    """Remove anything that is plumbing rather than prose before counting words."""
    text = re.sub(r"<[^>]+>", " ", text)                          # stray html
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r" \1 ", text)     # markdown links, keep the label
    text = re.sub(r"\bhttps?://\S+", " ", text)                   # bare urls
    text = re.sub(r"\bwww\.\S+", " ", text)
    text = re.sub(r"\S+@\S+\.\S+", " ", text)                     # email addresses
    text = re.sub(r"\b[\w.-]+\.(?:com|net|org|fi|se|no|dk|eu|io|ai|dev|co|uk)\b", " ", text, flags=re.I)
    text = re.sub(r"[#*_`>|]", " ", text)                         # markdown punctuation
    return text


# Dotted tokens are almost always URL debris. These are the exceptions.
DOTTED_KEEP = {"node.js", "next.js", "vue.js", "d3.js", ".net", "asp.net", "socket.io"}


def useful_token(token: str) -> bool:
    token = token.strip(".-+")
    if len(token) < 3 or token in STOPWORDS:
        return False
    if any(ch.isdigit() for ch in token):
        return False
    if "." in token and token not in DOTTED_KEEP:
        return False
    if TLD_RE.search(token):
        return False
    return True


# Order and coverage matter: -ation is deliberately absent so that it is reduced
# by -ion instead, which lands collaboration in the same family as collaborative.
SUFFIXES = ("ings", "ing", "ements", "ement", "ments", "ment",
            "ities", "ity", "ives", "ive", "ions", "ion", "ers", "er", "ies", "es", "ed", "s")


def stem(word: str) -> str:
    """Collapse a word to a rough family key.

    Deliberately crude: it only has to put `develop`, `developing` and
    `development` in one bucket so the cloud stops showing three entries for one
    idea. The surface form actually displayed is the commonest one in the family,
    so an ugly stem is never shown to anyone.
    """
    if word in DOTTED_KEEP or "-" in word:
        return word
    previous = None
    while word != previous:
        previous = word
        for suffix in SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[: -len(suffix)]
                break
    if len(word) > 4 and word.endswith("y"):
        word = word[:-1]
    return word.rstrip("e") or previous


def geocode(location: str | None) -> tuple[float, float] | None:
    """Best-effort coordinates. Parenthetical qualifiers and separators are ignored."""
    if not location:
        return None
    cleaned = re.sub(r"\(.*?\)", " ", location).strip()
    for part in re.split(r"[,/;|]| and |&", cleaned):
        key = fold(part)
        if key in PLACELESS:
            continue
        if key in GEOCODES:
            return GEOCODES[key]
        for name, point in GEOCODES.items():          # "Otaniemi, Espoo" style values
            if name in key:
                return point
    return None


def term_stats(rows: list[dict], limit: int = 70) -> list[dict]:
    """Count how many adverts each term appears in, split into two halves of time.

    Document frequency rather than raw count, so one advert repeating a word
    twenty times cannot dominate, and a term is only reported once it shows up in
    at least three adverts.
    """
    dated = sorted([r for r in rows if r.get("applied")], key=lambda r: r["applied"])
    midpoint = dated[len(dated) // 2]["applied"] if dated else None
    totals: dict[str, int] = {}
    early: dict[str, int] = {}
    late: dict[str, int] = {}
    where: dict[str, list[str]] = {}
    surfaces: dict[str, dict[str, int]] = {}
    for row in rows:
        path = JOBS_DIR / row["path"]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _front, _order, body = split_note(text)
        # Drop the Suitability section: it is Jacky's own assessment, not the
        # advert, and would otherwise pollute what the market is asking for.
        body = re.split(r"^##\s+Suitability\s*$", body, maxsplit=1, flags=re.M)[0]
        body = "\n".join(l for l in body.split("\n") if not l.lstrip().startswith("#"))
        words: dict[str, str] = {}
        for token in TOKEN_RE.findall(strip_noise(body).lower()):
            if not useful_token(token):
                continue
            token = token.strip(".-+")
            family = stem(token)
            words.setdefault(family, token)
            surfaces.setdefault(family, {})
            surfaces[family][token] = surfaces[family].get(token, 0) + 1
        bucket = None
        if midpoint and row.get("applied"):
            bucket = late if row["applied"] >= midpoint else early
        for family in words:
            totals[family] = totals.get(family, 0) + 1
            where.setdefault(family, []).append(row["path"])
            if bucket is not None:
                bucket[family] = bucket.get(family, 0) + 1
    early_total = sum(1 for r in dated if r["applied"] < midpoint) if midpoint else 0
    late_total = len(dated) - early_total
    out = [
        {
            "term": max(surfaces.get(term, {term: 1}).items(), key=lambda kv: (kv[1], -len(kv[0])))[0],
            "notes": count,
            "earlyShare": round(early.get(term, 0) / early_total, 4) if early_total else 0,
            "lateShare": round(late.get(term, 0) / late_total, 4) if late_total else 0,
            "paths": where.get(term, []),
        }
        for term, count in totals.items() if count >= 3
    ]
    out.sort(key=lambda d: -d["notes"])
    return {"terms": out[:limit], "earlyNotes": early_total, "lateNotes": late_total,
            "splitAt": midpoint}


# --------------------------------------------------------------------------- #
# cover letters: reading, writing, and rendering .odt documents
# --------------------------------------------------------------------------- #

SOFFICE_CANDIDATES = [
    os.environ.get("SOFFICE_BIN", ""),
    "/usr/bin/soffice",
    "/usr/lib/libreoffice/program/soffice",
    "/opt/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
]
# The attribute group must be lazy: a greedy one consumes the slash of a
# self-closing <text:p .../> and then swallows the paragraph after it.
TEXT_P_RE = re.compile(rb"<text:p\b([^>]*?)(?:/>|>(.*?)</text:p>)", re.S)
BODY_RE = re.compile(rb"(<office:text[^>]*>)(.*)(</office:text>)", re.S)
STYLE_ATTR_RE = re.compile(rb'text:style-name="([^"]*)"')
TAG_RE = re.compile(rb"<[^>]+>")


def soffice_path() -> str | None:
    for candidate in SOFFICE_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return shutil.which("soffice") or shutil.which("libreoffice")


SOFFICE = soffice_path()


def odt_paragraphs(data: bytes) -> tuple[list[dict], bytes]:
    """Pull the paragraphs out of an .odt, with the raw content.xml alongside."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        content = archive.read("content.xml")
    body = BODY_RE.search(content)
    if not body:
        return [], content
    paragraphs = []
    for match in TEXT_P_RE.finditer(body.group(2)):
        attrs, inner = match.group(1), match.group(2) or b""
        style = STYLE_ATTR_RE.search(attrs)
        text = html_mod.unescape(TAG_RE.sub(b"", inner).decode("utf-8", "replace"))
        paragraphs.append({"style": (style.group(1).decode() if style else ""), "text": text})
    return paragraphs, content


def odt_blocks(paragraphs: list[dict]) -> list[dict]:
    """Collapse the empty spacer paragraphs that separate real ones."""
    return [p for p in paragraphs if p["text"].strip()]


def rebuild_odt(data: bytes, blocks: list[str], styles: list[str]) -> bytes:
    """Rewrite the paragraph text, leaving styles.xml and the style definitions alone.

    Page geometry, fonts and the automatic style definitions all live outside the
    body, so replacing only the body keeps a letter's formatting identical to the
    document it was built from.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        parts = {name: archive.read(name) for name in names}

    content = parts["content.xml"]
    body = BODY_RE.search(content)
    if not body:
        raise ValueError("this .odt has no office:text body to rewrite")

    pieces = []
    for index, text in enumerate(blocks):
        style = styles[index] if index < len(styles) else (styles[-1] if styles else "P2")
        escaped = html_mod.escape(text, quote=False).encode("utf-8")
        pieces.append(b'<text:p text:style-name="%s">%s</text:p>' % (style.encode(), escaped))
        if index != len(blocks) - 1:                     # one empty paragraph between blocks
            pieces.append(b'<text:p text:style-name="%s"/>' % style.encode())
    new_content = content[:body.start(2)] + b"".join(pieces) + content[body.end(2):]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as out:
        if "mimetype" in parts:                          # must stay first and uncompressed
            out.writestr(zipfile.ZipInfo("mimetype"), parts["mimetype"], compress_type=zipfile.ZIP_STORED)
        for name in names:
            if name == "mimetype":
                continue
            out.writestr(name, new_content if name == "content.xml" else parts[name])
    return buffer.getvalue()


def render_pdf(odt: Path) -> Path:
    """Convert an .odt to PDF beside it using LibreOffice."""
    if not SOFFICE:
        raise RuntimeError("no LibreOffice available in this container, so PDFs cannot be rendered")
    with tempfile.TemporaryDirectory() as scratch:
        # A private profile keeps concurrent conversions from fighting over one lock.
        result = subprocess.run(
            [SOFFICE, f"-env:UserInstallation=file://{scratch}/profile", "--headless",
             "--convert-to", "pdf", "--outdir", str(odt.parent), str(odt)],
            capture_output=True, text=True, timeout=180,
        )
    produced = odt.with_suffix(".pdf")
    if not produced.exists():
        raise RuntimeError((result.stderr or result.stdout or "conversion produced no file").strip()[:400])
    return produced


BANNED_PHRASES = ["i.e.", "e.g.", "delve", "leverage", "passionate about",
                  "i am confident that", "in today's fast-paced"]


def letter_checks(blocks: list[dict]) -> dict:
    """Score a letter against the standing rules in COVER_LETTER_PROMPT.md."""
    body = " ".join(b["text"] for b in blocks[1:-2]) if len(blocks) > 3 else \
           " ".join(b["text"] for b in blocks)
    lower = body.lower()
    words = len(body.split())
    return {
        "words": words,
        "wordsOk": 360 <= words <= 450,
        "colons": body.count(":"),
        "emDashes": body.count("\u2014"),
        "banned": sorted({phrase for phrase in BANNED_PHRASES if phrase in lower}),
        "paragraphs": len(blocks),
    }


def letter_dir_for(note_path: str) -> Path:
    """Where a letter for this note belongs: Cover Letters/<year>/."""
    for folder, kind in artefact_dirs().items():
        if kind == "letter":
            return folder / Path(note_path).parent.name
    return JOBS_DIR / "Cover Letters" / Path(note_path).parent.name


def letter_template() -> Path | None:
    """The document whose styles a new letter should inherit."""
    configured = os.environ.get("LETTER_TEMPLATE", "").strip()
    if configured:
        candidate = (JOBS_DIR / configured).resolve()
        if candidate.is_file():
            return candidate
    newest = None
    for folder, kind in artefact_dirs().items():
        if kind != "letter":
            continue
        for path in folder.rglob("*.odt"):
            if path.name.startswith((".", "~")):
                continue
            if newest is None or path.stat().st_mtime > newest.stat().st_mtime:
                newest = path
    return newest


# The funnel the whole portal is about: tracked, applied, resolved. Drawn rather
# than shipped as a binary so the image stays readable and reviewable in the diff.
FAVICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" role="img" aria-label="Jobs Portal">'
    b'<rect width="32" height="32" rx="7" fill="#2a78d6"/>'
    b'<rect x="6" y="8" width="20" height="4" rx="2" fill="#ffffff"/>'
    b'<rect x="6" y="14" width="14" height="4" rx="2" fill="#ffffff" fill-opacity="0.82"/>'
    b'<rect x="6" y="20" width="8" height="4" rx="2" fill="#ffffff" fill-opacity="0.64"/>'
    b"</svg>"
)

# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "JobsPortal"
    protocol_version = "HTTP/1.1"

    # -- helpers ---------------------------------------------------------- #

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _authorised(self) -> bool:
        if not AUTH_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and header[7:].strip() == AUTH_TOKEN:
            return True
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return query.get("token", [""])[0] == AUTH_TOKEN

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"request body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    # -- routes ----------------------------------------------------------- #

    def do_GET(self) -> None:
        route = urllib.parse.urlparse(self.path).path
        if route == "/healthz":
            self._json({"ok": True, "positions": len(list_notes()), "markdown": MARKDOWN_BACKEND,
                        "pdf": SOFFICE or False})
            return
        if route in {"/favicon.svg", "/favicon.ico"}:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(FAVICON)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(FAVICON)
            return
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "a token is required")
            return
        if route in {"/", "/index.html"}:
            try:
                page = TEMPLATE.read_text(encoding="utf-8")
            except OSError as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"template missing: {exc}")
                return
            page = page.replace("__CONFIG__", json.dumps({
                "statuses": STATUSES,
                "fields": FIELD_ORDER,
                "readOnly": READ_ONLY,
                "authRequired": bool(AUTH_TOKEN),
                "today": datetime.date.today().isoformat(),
                "jobsDir": str(JOBS_DIR),
                "canRenderPdf": bool(SOFFICE),
            }))
            self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if route == "/api/positions":
            self._json({"positions": list_notes(), "today": datetime.date.today().isoformat()})
            return
        if route == "/api/insights":
            rows = list_notes()
            places: dict[str, dict] = {}
            unplaced: dict[str, int] = {}
            for row in rows:
                point = geocode(row.get("location"))
                label = (row.get("location") or "Unknown").strip() or "Unknown"
                if point is None:
                    unplaced[label] = unplaced.get(label, 0) + 1
                    continue
                entry = places.setdefault(label, {"label": label, "lat": point[0], "lon": point[1],
                                                  "count": 0, "companies": []})
                entry["count"] += 1
                if row["company"] not in entry["companies"]:
                    entry["companies"].append(row["company"])
            self._json({
                "termStats": term_stats(rows),
                "places": sorted(places.values(), key=lambda p: -p["count"]),
                "unplaced": [{"label": k, "count": v} for k, v in
                             sorted(unplaced.items(), key=lambda kv: -kv[1])],
            })
            return
        if route == "/api/letter":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                doc = safe_doc_path(query.get("path", [""])[0])
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if doc.suffix.lower() != ".odt":
                self._error(HTTPStatus.BAD_REQUEST, "only .odt letters can be edited")
                return
            try:
                blocks = odt_blocks(odt_paragraphs(doc.read_bytes())[0])
            except (KeyError, zipfile.BadZipFile, ValueError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, f"this file cannot be read as an .odt: {exc}")
                return
            pdf = doc.with_suffix(".pdf")
            self._json({
                "path": relative(doc),
                "blocks": blocks,
                "checks": letter_checks(blocks),
                "pdf": relative(pdf) if pdf.is_file() else None,
                "pdfMtime": pdf.stat().st_mtime if pdf.is_file() else None,
                "mtime": doc.stat().st_mtime,
                "canRenderPdf": bool(SOFFICE),
            })
            return
        if route == "/api/guides":
            # The markdown documents sitting at the top of the jobs folder are the
            # rules the letters are written to. Exposed read-only so an MCP client
            # can follow the same method rather than inventing its own.
            guides = [
                {"name": g.name, "bytes": g.stat().st_size, "mtime": g.stat().st_mtime}
                for g in sorted(JOBS_DIR.glob("*.md"))
                if g.is_file() and not g.name.startswith(".")
            ] if JOBS_DIR.is_dir() else []
            self._json({"guides": guides})
            return
        if route == "/api/guide":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (query.get("name", [""])[0] or "").strip()
            candidate = (JOBS_DIR / name).resolve()
            if "/" in name or not name.endswith(".md") or candidate.parent != JOBS_DIR \
                    or not candidate.is_file():
                self._error(HTTPStatus.BAD_REQUEST, "name must be a markdown file at the top of the jobs folder")
                return
            self._json({"name": name, "text": candidate.read_text(encoding="utf-8", errors="replace")})
            return
        if route == "/api/file":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                doc = safe_doc_path(query.get("path", [""])[0])
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            payload = doc.read_bytes()
            disposition = "inline" if doc.suffix.lower() == ".pdf" else "attachment"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", CONTENT_TYPES.get(doc.suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Disposition",
                             f'{disposition}; filename="{doc.name}"'.encode("ascii", "replace").decode())
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            return
        if route == "/api/note":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                path = safe_path(query.get("path", [""])[0])
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if not path.is_file():
                self._error(HTTPStatus.NOT_FOUND, "note not found")
                return
            text = path.read_text(encoding="utf-8", errors="replace")
            front, order, body = split_note(text)
            self._json({
                "path": relative(path),
                "frontmatter": front,
                "order": order,
                "body": body.lstrip("\n"),
                "html": render_markdown(body),
                "docs": docs_for(path.stem, artefact_index()),
                "mtime": path.stat().st_mtime,
            })
            return
        self._error(HTTPStatus.NOT_FOUND, "no such route")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_PUT(self) -> None:
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "a token is required")
            return
        path_only = urllib.parse.urlparse(self.path).path
        if path_only == "/api/letter":
            if READ_ONLY:
                self._error(HTTPStatus.FORBIDDEN, "the portal is running read-only")
                return
            try:
                payload = self._body_json()
                doc = safe_doc_path(payload.get("path", ""))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if doc.suffix.lower() != ".odt" or not doc.is_file():
                self._error(HTTPStatus.BAD_REQUEST, "only an existing .odt letter can be saved")
                return
            if payload.get("mtime") and abs(float(payload["mtime"]) - doc.stat().st_mtime) > 0.001:
                self._error(HTTPStatus.CONFLICT,
                            "the letter changed on disk since it was opened, reload before saving")
                return
            blocks = payload.get("blocks")
            if not isinstance(blocks, list) or not blocks:
                self._error(HTTPStatus.BAD_REQUEST, "blocks must be a non-empty list")
                return
            texts = [str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in blocks]
            styles = [str(b.get("style", "")) if isinstance(b, dict) else "" for b in blocks]
            original = odt_blocks(odt_paragraphs(doc.read_bytes())[0])
            for index, style in enumerate(styles):
                if not style:
                    styles[index] = original[index]["style"] if index < len(original) else "P2"
            try:
                rebuilt = rebuild_odt(doc.read_bytes(), texts, styles)
            except (ValueError, zipfile.BadZipFile) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            saved_backup = backup(doc)
            temp = doc.with_suffix(doc.suffix + ".tmp")
            temp.write_bytes(rebuilt)
            os.replace(temp, doc)
            fresh = odt_blocks(odt_paragraphs(doc.read_bytes())[0])
            self._json({"saved": relative(doc), "backup": saved_backup,
                        "checks": letter_checks(fresh), "mtime": doc.stat().st_mtime})
            return
        if path_only != "/api/note":
            self._error(HTTPStatus.NOT_FOUND, "no such route")
            return
        if READ_ONLY:
            self._error(HTTPStatus.FORBIDDEN, "the portal is running read-only")
            return
        try:
            payload = self._body_json()
            path = safe_path(payload.get("path", ""))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "note not found")
            return

        existing = path.read_text(encoding="utf-8", errors="replace")

        if "mtime" in payload and payload["mtime"]:
            if abs(float(payload["mtime"]) - path.stat().st_mtime) > 0.001:
                self._error(
                    HTTPStatus.CONFLICT,
                    "the note changed on disk since it was opened, reload before saving",
                )
                return

        edits: dict[str, str | None] = {}
        incoming = payload.get("frontmatter")
        if isinstance(incoming, dict):
            for key, value in incoming.items():
                key = str(key).strip().lower()
                if key:
                    edits[key] = str(value) if value is not None else ""

        _front, _order, current_body = split_note(existing)

        # Stamp the date a status transition implies, but only when the field
        # would otherwise be left empty, so a date set by hand is never touched.
        auto_filled: dict[str, str] = {}
        if "job_status" in edits:
            was = (_front.get("job_status") or "").strip()
            now = (edits["job_status"] or "").strip()
            if now != was:
                stamp = datetime.date.today().isoformat()
                target = "date_applied" if now == "Applied" else (
                    "date_rejected" if now in {"Rejected", "Not eligible"} else None
                )
                if target:
                    proposed = edits.get(target, _front.get(target))
                    if not (proposed or "").strip():
                        edits[target] = stamp
                        auto_filled[target] = stamp

        new_body = payload["body"] if isinstance(payload.get("body"), str) else None
        if new_body is not None and new_body.strip() == current_body.strip():
            new_body = None                      # body untouched, leave those bytes alone

        updated = apply_edits(existing, edits, new_body)
        if updated == existing:
            today = datetime.date.today()
            self._json({
                "saved": relative(path),
                "backup": None,
                "autoFilled": {},
                "unchanged": True,
                "row": row_for(path, today),
                "mtime": path.stat().st_mtime,
            })
            return

        saved_backup = backup(path)
        write_note(path, updated)
        today = datetime.date.today()
        self._json({
            "saved": relative(path),
            "backup": saved_backup,
            "autoFilled": auto_filled,
            "row": row_for(path, today),
            "mtime": path.stat().st_mtime,
        })

    def do_DELETE(self) -> None:
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "a token is required")
            return
        if urllib.parse.urlparse(self.path).path != "/api/note":
            self._error(HTTPStatus.NOT_FOUND, "no such route")
            return
        if READ_ONLY:
            self._error(HTTPStatus.FORBIDDEN, "the portal is running read-only")
            return
        try:
            payload = self._body_json()
            path = safe_path(payload.get("path", ""))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if not path.is_file():
            self._error(HTTPStatus.NOT_FOUND, "note not found")
            return
        saved_backup = backup(path)
        path.unlink()
        self._json({"deleted": relative(path), "backup": saved_backup})

    def do_POST(self) -> None:
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "a token is required")
            return
        path_only = urllib.parse.urlparse(self.path).path
        if path_only == "/api/letter/pdf":
            if READ_ONLY:
                self._error(HTTPStatus.FORBIDDEN, "the portal is running read-only")
                return
            try:
                payload = self._body_json()
                doc = safe_doc_path(payload.get("path", ""))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            if doc.suffix.lower() != ".odt" or not doc.is_file():
                self._error(HTTPStatus.BAD_REQUEST, "only an existing .odt can be rendered")
                return
            try:
                pdf = render_pdf(doc)
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
                return
            pages = len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))
            self._json({"pdf": relative(pdf), "pages": pages, "mtime": pdf.stat().st_mtime})
            return
        if path_only == "/api/letter":
            if READ_ONLY:
                self._error(HTTPStatus.FORBIDDEN, "the portal is running read-only")
                return
            try:
                payload = self._body_json()
                note = safe_path(payload.get("notePath", ""))
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            template = letter_template()
            if template is None:
                self._error(HTTPStatus.BAD_REQUEST,
                            "no existing .odt letter to inherit styles from, so there is no template")
                return
            target = letter_dir_for(relative(note)) / f"{note.stem}.odt"
            if target.exists():
                self._error(HTTPStatus.CONFLICT, f"{relative(target)} already exists")
                return
            front, _order, _body = split_note(note.read_text(encoding="utf-8", errors="replace"))
            company = front.get("company") or "the team"
            title = front.get("job_title") or "the role"
            starter = [
                "Dear Hiring Team,",
                f"I am writing to express my interest and suitability for the position of {title} at {company}.",
                "",
                "Yours sincerely,",
                "Jacky Cao",
            ]
            source = odt_blocks(odt_paragraphs(template.read_bytes())[0])
            styles = [b["style"] for b in source]
            if len(styles) < len(starter):
                styles += [styles[-1] if styles else "P2"] * (len(starter) - len(styles))
            styles = [styles[0], "P2", "P2", styles[-2] if len(styles) > 1 else "P1", styles[-1]]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(rebuild_odt(template.read_bytes(), starter, styles))
            blocks = odt_blocks(odt_paragraphs(target.read_bytes())[0])
            self._json({"created": relative(target), "blocks": blocks,
                        "checks": letter_checks(blocks), "mtime": target.stat().st_mtime,
                        "template": relative(template)}, HTTPStatus.CREATED)
            return
        if path_only != "/api/notes":
            self._error(HTTPStatus.NOT_FOUND, "no such route")
            return
        if READ_ONLY:
            self._error(HTTPStatus.FORBIDDEN, "the portal is running read-only")
            return
        try:
            payload = self._body_json()
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return

        front = payload.get("frontmatter") or {}
        company = str(front.get("company") or "").strip()
        title = str(front.get("job_title") or "").strip()
        if not company or not title:
            self._error(HTTPStatus.BAD_REQUEST, "company and job_title are both required")
            return
        year = str(payload.get("year") or datetime.date.today().year).strip()
        if not re.fullmatch(r"\d{4}", year):
            self._error(HTTPStatus.BAD_REQUEST, "year must be four digits")
            return

        target = POSITIONS_DIR / year / slug_filename(company, title)
        try:
            target = safe_path(relative(target))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        if target.exists():
            self._error(HTTPStatus.CONFLICT, f"{relative(target)} already exists")
            return

        ordered = {key: clean(str(front.get(key) or "")) for key in FIELD_ORDER}
        for key, value in front.items():          # keep any extra keys the client sent
            key = str(key).strip().lower()
            if key and key not in ordered:
                ordered[key] = clean(str(value or ""))
        if not ordered.get("job_status"):
            ordered["job_status"] = "Not applied"
        if not ordered.get("date_added"):
            ordered["date_added"] = datetime.date.today().isoformat()
        body = payload.get("body") or "## Description\n"
        write_note(target, compose_note(ordered, FIELD_ORDER, body))
        self._json({"created": relative(target), "row": row_for(target, datetime.date.today())},
                   HTTPStatus.CREATED)


def main() -> None:
    if not POSITIONS_DIR.is_dir():
        print(
            f"error: {POSITIONS_DIR} does not exist.\n"
            f"Set JOBS_DIR to the folder that contains Positions/.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    notes = list_notes()
    print(f"Jobs Portal serving {len(notes)} notes from {POSITIONS_DIR}")
    print(f"  markdown: {MARKDOWN_BACKEND}")
    print(f"  writes:   {'disabled (READ_ONLY)' if READ_ONLY else 'enabled'}"
          f"{'' if not BACKUPS or READ_ONLY else f', backups in {BACKUP_DIR}'}")
    print(f"  auth:     {'bearer token required' if AUTH_TOKEN else 'open (no token set)'}")
    print(f"  pdf:      {SOFFICE or 'unavailable, letters can be edited but not rendered'}")
    print(f"  listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
