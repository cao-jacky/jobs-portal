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
import shutil
import sys
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


def enrich(row: dict, today: datetime.date) -> dict:
    applied = parse_date(row["applied"])
    rejected = parse_date(row["rejected"])
    deadline = parse_date(row["deadline"])
    row["daysToOutcome"] = (rejected - applied).days if applied and rejected else None
    row["daysOpen"] = (today - applied).days if applied and not rejected else None
    row["daysToDeadline"] = (deadline - today).days if deadline else None
    row["appliedMonth"] = row["applied"][:7] if applied else None
    return row


def row_for(path: Path, today: datetime.date) -> dict:
    text = path.read_text(encoding="utf-8", errors="replace")
    front, _order, body = split_note(text)
    return enrich(
        {
            "path": relative(path),
            "company": front.get("company") or "Unknown",
            "title": front.get("job_title") or "Unknown",
            "location": front.get("location") or "Unknown",
            "status": front.get("job_status") or "Unknown",
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
    rows = []
    if POSITIONS_DIR.is_dir():
        for path in sorted(POSITIONS_DIR.rglob("*.md")):
            try:
                rows.append(row_for(path, today))
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
            self._json({"ok": True, "positions": len(list_notes()), "markdown": MARKDOWN_BACKEND})
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
            }))
            self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if route == "/api/positions":
            self._json({"positions": list_notes(), "today": datetime.date.today().isoformat()})
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
        new_body = payload["body"] if isinstance(payload.get("body"), str) else None
        if new_body is not None and new_body.strip() == current_body.strip():
            new_body = None                      # body untouched, leave those bytes alone

        updated = apply_edits(existing, edits, new_body)
        if updated == existing:
            today = datetime.date.today()
            self._json({
                "saved": relative(path),
                "backup": None,
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
            "row": row_for(path, today),
            "mtime": path.stat().st_mtime,
        })

    def do_POST(self) -> None:
        if not self._authorised():
            self._error(HTTPStatus.UNAUTHORIZED, "a token is required")
            return
        if urllib.parse.urlparse(self.path).path != "/api/notes":
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
        ordered.setdefault("job_status", "Not applied")
        if not ordered.get("job_status"):
            ordered["job_status"] = "Not applied"
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
    print(f"  listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
