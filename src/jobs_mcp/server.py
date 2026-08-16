"""MCP server for the Jobs Portal.

A thin stdio server that drives a running portal over its HTTP API, so an LLM
client can do the same work by hand: read the pipeline, add a position from a
pasted advert, move a status, and draft, check and render a cover letter.

The portal stays the only thing that touches the files, which means every write
made through here inherits its safety properties: surgical frontmatter edits,
atomic replace, timestamped backups and conflict detection.

Environment:
    PORTAL_URL    base URL of the portal (default http://localhost:8412)
    PORTAL_TOKEN  the portal's AUTH_TOKEN, when it has one set
    PORTAL_ALLOW_WRITES  set to 0 to expose only the read tools
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.mcpserver import MCPServer

PORTAL_URL = os.environ.get("PORTAL_URL", "http://localhost:8412").rstrip("/")
PORTAL_TOKEN = os.environ.get("PORTAL_TOKEN", "").strip()
ALLOW_WRITES = os.environ.get("PORTAL_ALLOW_WRITES", "1").strip() not in {"0", "false", "no"}

mcp = MCPServer(
    "jobs",
    instructions=(
        f"Job application tracker backed by markdown notes, served by the portal at {PORTAL_URL}. "
        "Positions live one note per role with frontmatter (company, job_title, location, deadline, "
        "job_status, date_added, date_applied, date_rejected, link) and the advert in the body.\n\n"
        "Before drafting any cover letter, read the cover-letter-rules and profile resources: they "
        "carry the model letter, the structural moves, the banned constructions, the no-gap rule and "
        "the evidence bank, and a letter written without them will not match the voice or the record. "
        "Use check_letter before saving, and render_letter_pdf to confirm it comes out as one page."
    ),
)


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #

class PortalError(RuntimeError):
    """Raised with the portal's own message so the model sees why something failed."""


def call(method: str, path: str, payload: dict | None = None) -> Any:
    url = f"{PORTAL_URL}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if PORTAL_TOKEN:
        request.add_header("Authorization", f"Bearer {PORTAL_TOKEN}")
    try:
        with urllib.request.urlopen(request, timeout=200) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except json.JSONDecodeError:
            pass
        raise PortalError(f"the portal refused this ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise PortalError(
            f"no portal reachable at {PORTAL_URL} ({exc.reason}). Is the container running, "
            "and is PORTAL_URL pointing at it?"
        ) from exc


def writable() -> None:
    if not ALLOW_WRITES:
        raise PortalError("this MCP server is configured read-only (PORTAL_ALLOW_WRITES=0)")


def summarise(row: dict) -> dict:
    """The fields worth spending tokens on when listing many positions."""
    return {
        "path": row["path"],
        "company": row["company"],
        "title": row["title"],
        "location": row["location"],
        "status": row["status"],
        "added": row.get("added"),
        "applied": row.get("applied"),
        "deadline": row.get("deadline"),
        "daysOpen": row.get("daysOpen"),
        "daysToDeadline": row.get("daysToDeadline"),
        "documents": [d["kind"] for d in row.get("docs", [])],
    }


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

@mcp.tool()
def list_positions(status: str = "", year: str = "", query: str = "", limit: int = 50) -> dict:
    """List tracked positions, newest first.

    Args:
        status: exact job_status to filter by, such as "Applied" or "Not applied".
        year: the year folder, such as "2026".
        query: case-insensitive substring matched against company, title and location.
        limit: how many to return; the total is always reported.
    """
    rows = call("GET", "/api/positions")["positions"]
    if status:
        rows = [r for r in rows if r["status"].lower() == status.lower()]
    if year:
        rows = [r for r in rows if r["year"] == year]
    if query:
        needle = query.lower()
        rows = [r for r in rows
                if needle in f"{r['company']} {r['title']} {r['location']}".lower()]
    return {"total": len(rows), "returned": min(len(rows), limit),
            "positions": [summarise(r) for r in rows[:limit]]}


@mcp.tool()
def get_position(path: str) -> dict:
    """Read one position note: its frontmatter and the full advert text.

    Args:
        path: the note path as returned by list_positions, such as
            "Positions/2026/Aspia - AI Engineer.md".
    """
    note = call("GET", "/api/note?path=" + urllib.parse.quote(path))
    return {"path": note["path"], "frontmatter": note["frontmatter"], "advert": note["body"]}


@mcp.tool()
def search_adverts(term: str, limit: int = 25) -> dict:
    """Find positions whose advert text mentions a term.

    Useful for questions like which roles asked for Kubernetes, or which
    mentioned a salary range.

    Args:
        term: a single word or phrase to look for in the advert bodies.
        limit: how many matches to return.
    """
    rows = call("GET", "/api/positions")["positions"]
    needle = term.lower()
    hits = []
    for row in rows:
        note = call("GET", "/api/note?path=" + urllib.parse.quote(row["path"]))
        if needle in note["body"].lower():
            index = note["body"].lower().index(needle)
            hits.append({**summarise(row),
                         "context": note["body"][max(0, index - 120): index + 160].strip()})
        if len(hits) >= limit:
            break
    return {"term": term, "matches": len(hits), "positions": hits}


@mcp.tool()
def pipeline_stats() -> dict:
    """Summarise the application funnel: what is live, stale, resolved and converting."""
    rows = call("GET", "/api/positions")["positions"]
    sent = [r for r in rows if r["status"] in
            {"Applied", "Rejected", "Interviewed", "Interview Invitation", "Not eligible"} or r["applied"]]
    live = [r for r in rows if r["status"] == "Applied"]
    wins = [r for r in rows if r["status"] in {"Interviewed", "Interview Invitation"}]
    resolved = [r for r in rows if r["status"] in
                {"Rejected", "Not eligible", "Interviewed", "Interview Invitation"}]
    gaps = sorted(r["daysToOutcome"] for r in rows
                  if r["daysToOutcome"] is not None and r["daysToOutcome"] >= 0)
    ageing = [r for r in live if (r["daysOpen"] or 0) > 21]
    deadlines = [r for r in rows if r["status"] == "Not applied" and r["daysToDeadline"] is not None]
    return {
        "tracked": len(rows),
        "sent": len(sent),
        "live": len(live),
        "ageingPast21Days": [summarise(r) for r in
                             sorted(ageing, key=lambda r: -(r["daysOpen"] or 0))[:10]],
        "interviews": len(wins),
        "interviewRateOfSent": round(len(wins) / len(sent) * 100, 1) if sent else 0,
        "interviewRateOfResolved": round(len(wins) / len(resolved) * 100, 1) if resolved else 0,
        "medianDaysToRejection": gaps[len(gaps) // 2] if gaps else None,
        "openDeadlines": [summarise(r) for r in sorted(deadlines, key=lambda r: r["daysToDeadline"])],
    }


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

@mcp.tool()
def create_position(company: str, job_title: str, advert: str, location: str = "",
                    deadline: str = "", link: str = "", year: str = "") -> dict:
    """Add a position from a pasted advert.

    The note is created as "Not applied" with today's date_added, named
    "<company> - <job_title>.md" under the year folder.

    Args:
        company: the employer's name, used in the filename.
        job_title: the role title, used in the filename.
        advert: the advert text, which becomes the note body.
        location: city or "Remote", if the advert states one.
        deadline: closing date as YYYY-MM-DD, if the advert states one.
        link: URL of the original posting.
        year: four-digit year folder; defaults to the current year.
    """
    writable()
    body = advert if advert.lstrip().startswith("#") else f"## Description\n\n{advert.strip()}\n"
    payload = {
        "frontmatter": {"company": company, "job_title": job_title, "location": location,
                        "deadline": deadline, "link": link, "job_status": "Not applied"},
        "body": body,
    }
    if year:
        payload["year"] = year
    result = call("POST", "/api/notes", payload)
    return {"created": result["created"], "position": summarise(result["row"])}


@mcp.tool()
def update_position(path: str, job_status: str = "", deadline: str = "", location: str = "",
                    link: str = "", date_applied: str = "", date_rejected: str = "") -> dict:
    """Change fields on a position note. Only the arguments given are written.

    Moving job_status to "Applied" stamps date_applied, and to "Rejected" or
    "Not eligible" stamps date_rejected, unless that date is already set.

    Args:
        path: the note path, as returned by list_positions.
        job_status: one of Not applied, Applied, Interview Invitation, Interviewed,
            Rejected, Not eligible, Skipped.
        deadline: closing date as YYYY-MM-DD.
        location: city or "Remote".
        link: URL of the posting.
        date_applied: override the applied date as YYYY-MM-DD.
        date_rejected: override the rejection date as YYYY-MM-DD.
    """
    writable()
    fields = {k: v for k, v in {
        "job_status": job_status, "deadline": deadline, "location": location, "link": link,
        "date_applied": date_applied, "date_rejected": date_rejected,
    }.items() if v}
    if not fields:
        raise PortalError("give at least one field to change")
    result = call("PUT", "/api/note", {"path": path, "frontmatter": fields})
    return {"saved": result["saved"], "autoFilled": result.get("autoFilled", {}),
            "position": summarise(result["row"])}


# --------------------------------------------------------------------------- #
# cover letters
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_letter(path: str) -> dict:
    """Read the cover letter for a position, paragraph by paragraph.

    Args:
        path: either the note path, or the letter's own .odt path.
    """
    if path.endswith(".md"):
        row = next((r for r in call("GET", "/api/positions")["positions"] if r["path"] == path), None)
        if row is None:
            raise PortalError(f"no position at {path}")
        doc = next((d for d in row.get("docs", []) if d["kind"] == "letter" and d["ext"] == "odt"), None)
        if doc is None:
            raise PortalError(f"no .odt letter has been written for {row['company']} yet")
        path = doc["path"]
    letter = call("GET", "/api/letter?path=" + urllib.parse.quote(path))
    return {"path": letter["path"], "paragraphs": [b["text"] for b in letter["blocks"]],
            "checks": letter["checks"], "pdf": letter["pdf"], "canRenderPdf": letter["canRenderPdf"]}


@mcp.tool()
def save_letter(path: str, paragraphs: list[str]) -> dict:
    """Replace the text of a cover letter, keeping its formatting.

    Page size, margins, fonts and styles come from the document itself and are
    never rewritten, so only the words change. The salutation and sign-off are
    paragraphs like any other, so pass the whole letter in order.

    Args:
        path: the letter's .odt path.
        paragraphs: the full letter, one string per paragraph, in order.
    """
    writable()
    if not paragraphs:
        raise PortalError("a letter needs at least one paragraph")
    letter = call("GET", "/api/letter?path=" + urllib.parse.quote(path))
    result = call("PUT", "/api/letter", {
        "path": path,
        "blocks": [{"text": text} for text in paragraphs],
        "mtime": letter["mtime"],
    })
    return {"saved": result["saved"], "checks": result["checks"], "backup": result.get("backup")}


@mcp.tool()
def create_letter(note_path: str) -> dict:
    """Start a cover letter for a position, inheriting formatting from the most recent one.

    Args:
        note_path: the position note the letter is for.
    """
    writable()
    result = call("POST", "/api/letter", {"notePath": note_path})
    return {"created": result["created"], "template": result["template"],
            "paragraphs": [b["text"] for b in result["blocks"]]}


@mcp.tool()
def check_letter(text: str) -> dict:
    """Check draft letter text against the standing rules, without saving anything.

    Reports the body word count against the 360 to 450 range, colons, em dashes
    and banned phrasing. Pass the body only, without the salutation and sign-off,
    or pass the whole letter and read the numbers accordingly.

    Args:
        text: the draft, with paragraphs separated by blank lines.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    words = len(text.split())
    banned = [phrase for phrase in
              ["i.e.", "e.g.", "delve", "leverage", "passionate about",
               "i am confident that", "in today's fast-paced"]
              if phrase in text.lower()]
    return {
        "words": words,
        "wordsOk": 360 <= words <= 450,
        "paragraphs": len(paragraphs),
        "colons": text.count(":"),
        "colonsOk": text.count(":") <= 1,
        "emDashes": text.count("\u2014"),
        "emDashesOk": "\u2014" not in text,
        "banned": banned,
        "verdict": "ready" if (360 <= words <= 450 and text.count(":") <= 1
                               and "\u2014" not in text and not banned) else "needs work",
    }


@mcp.tool()
def render_letter_pdf(path: str) -> dict:
    """Render a letter to PDF and report the page count, which should be one.

    Args:
        path: the letter's .odt path.
    """
    writable()
    result = call("POST", "/api/letter/pdf", {"path": path})
    return {"pdf": result["pdf"], "pages": result["pages"],
            "onePage": result["pages"] == 1}


# --------------------------------------------------------------------------- #
# the rules, exposed so a client follows the same method rather than its own
# --------------------------------------------------------------------------- #

def guide(name: str) -> str:
    return call("GET", "/api/guide?name=" + urllib.parse.quote(name))["text"]


@mcp.resource("jobs://guide/cover-letter-rules", title="Cover letter rules",
              mime_type="text/markdown")
def cover_letter_rules() -> str:
    """The model letter, its structural moves, the banned constructions and the no-gap rule."""
    return guide("COVER_LETTER_PROMPT.md")


@mcp.resource("jobs://guide/profile", title="Profile and evidence bank", mime_type="text/markdown")
def profile() -> str:
    """Who the applicant is, the career tracks, and which evidence leads for each."""
    return guide("PROFILE.md")


@mcp.resource("jobs://guide/application-process", title="Application process",
              mime_type="text/markdown")
def application_process() -> str:
    """The end-to-end process: suitability check, resume, letter, wrap-up."""
    return guide("JOB_APPLICATION.md")


@mcp.resource("jobs://guide/job-search", title="Job search strategy", mime_type="text/markdown")
def job_search() -> str:
    """Target tiers, what converts, and the categories to stop applying to."""
    return guide("JOB_SEARCH.md")


@mcp.resource("jobs://pipeline", title="Current pipeline", mime_type="application/json")
def pipeline() -> str:
    """The live funnel numbers, so advice is grounded in the actual record."""
    return json.dumps(pipeline_stats(), indent=2)


@mcp.prompt(title="Draft a cover letter")
def draft_cover_letter(position_path: str) -> str:
    """Assemble everything needed to draft a letter for one position."""
    note = call("GET", "/api/note?path=" + urllib.parse.quote(position_path))
    front = note["frontmatter"]
    steps = [
        "Work in this order, and do not skip the first step:",
        "1. Read the jobs://guide/cover-letter-rules resource in full. It contains the model "
        "letter that is the only one on record to have produced an interview, the structural "
        "moves to reuse, the banned constructions, and the rule that a letter never volunteers "
        "a gap. Read jobs://guide/profile for the evidence bank and which track leads.",
        "2. Identify the top three responsibilities in the advert below. The first is what the "
        "mapping sentence in paragraph two must name.",
        "3. Draft the letter, 360 to 450 words, using only evidence from the profile. Invent "
        "nothing. Where a stated requirement has no honest match, use adjacency, ramp-up "
        "evidence, willingness where the advert asks for it, or silence.",
        "4. Run check_letter on the draft and fix whatever it reports.",
        "5. Only then create_letter and save_letter, and render_letter_pdf to confirm one page.",
        "",
        "Answer anything the advert explicitly asks for, such as a salary expectation, notice "
        "period, start date or language level, in one clause in the close.",
    ]
    header = f"Draft a cover letter for {front.get('company')}, {front.get('job_title')}."
    return "\n\n".join([header, "\n".join(steps), "--- the advert ---", note["body"]])


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
