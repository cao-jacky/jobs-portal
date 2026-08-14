# Jobs Portal

A small self-hosted web service for tracking job applications kept as markdown
files. It reads a folder of notes with YAML frontmatter, shows them as a
filterable ledger with summary metrics, renders each note inline, and writes
edits back to the original file.

The files stay the source of truth. There is no database, no index, and no second
copy of the data, so the notes remain editable in any text editor or markdown
app at the same time.

## Why it exists

Plain markdown notes are a good way to track a job search, but a folder of them
answers questions badly: what is still open, what has gone quiet, which deadline
lands next. Note-based database plugins solve this inside a specific editor. This
does it over HTTP instead, so the same view works from any device on the network
without that editor installed.

## Expected layout

```
<JOBS_DIR>/
  Positions/
    2025/
      Company - Job Title.md
    2026/
      Another Company - Another Title.md
```

Each note starts with frontmatter. Unknown keys are preserved and displayed, so
the schema can be extended freely:

```markdown
---
job_title: AI Engineer
company: Example Oy
location: Helsinki
deadline: 2026-08-31
job_status: Applied
date_applied: 2026-08-14
date_rejected:
link: https://example.com/jobs/1
---
## Description

The advert text.
```

`job_status` drives the metrics. The recognised values are `Not applied`,
`Applied`, `Interview Invitation`, `Interviewed`, `Rejected`, `Not eligible`, and
`Skipped`.

## Running it

### Docker Compose

The compose file pulls a prebuilt image from GHCR, so no build context is needed:

```
cp .env.example .env      # then set JOBS_PATH to the folder containing Positions/
docker compose up -d
```

Listens on <http://localhost:8412>.

To build from source instead of pulling, add the build overlay:

```
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

### Portainer

Add a stack pointing at this repository, or paste `docker-compose.yml` into the
web editor, then set `JOBS_PATH` as a stack environment variable.

Two things to know:

- The image must be pullable. Portainer runs `docker compose pull` before
  starting a stack, so a compose file naming a local-only image such as
  `jobs-portal:latest` fails with `pull access denied … repository does not
  exist`. That is why this compose file references the GHCR image and carries no
  `build:` section.
- If the pull is denied for `ghcr.io/...` rather than Docker Hub, the package
  visibility is the thing to check: GHCR packages can be private even when their
  repository is public. Either set the package to public on its GitHub page, or
  run `docker login ghcr.io` on the Docker host with a token holding
  `read:packages`. Verify from the host with
  `docker manifest inspect ghcr.io/cao-jacky/jobs-portal:latest`, which succeeds
  anonymously when the package is public.

### Directly

```
JOBS_DIR="/path/to/notes" python3 app.py
```

Python 3.11 or newer. No virtualenv required and no dependencies are needed:
`pip install markdown` is optional and improves note rendering, and without it a
built-in subset renderer is used.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `JOBS_DIR` | `.` | Folder containing `Positions/`. `/data` in the image. |
| `PORT` | `8080` | Listen port. |
| `HOST` | `0.0.0.0` | Bind address. |
| `AUTH_TOKEN` | unset | When set, every request needs it as a `Bearer` token or `?token=`. The page prompts once and stores it in `localStorage`. `/healthz` stays open so container healthchecks still work. |
| `READ_ONLY` | unset | Set to `1` to refuse all writes. The UI hides its save controls. |
| `BACKUPS` | `1` | Copy a note into `.portal-backups/` before overwriting. Set to `0` to disable. |

## Using it

- **Click a row** to open the note, with the advert rendered inline and the path,
  location, status, and deadline in the header.
- **The Edit tab** exposes every frontmatter field plus the raw body.
  `Cmd/Ctrl+S` saves and `Esc` closes. Unsaved changes are kept as a draft rather
  than warned about, so closing never loses work.
- **The status dropdown in the table** saves immediately, so moving something to
  Applied is one click.
- **New position** opens the same panel as a form: every frontmatter field, a
  year-folder select, and a body textarea to paste the advert into. The file is
  named `Positions/<year>/<Company> - <Title>.md` from the company and title.
- **Deep links**: the open note is reflected in the URL hash, so notes can be
  bookmarked and the back button works.
- **Summary panels** list deadlines still unapplied and applications that have
  gone quiet for more than 21 days. Both are clickable.
- **Each row leads with a status colour dot**, so the ledger can be scanned
  without reading the status column.
- **Charts** cover the funnel, applications sent per month, and the status
  breakdown. Bars and rows report their exact figures on hover or keyboard focus.

Light and dark themes both ship, following the viewer's system preference.

### Drafts

Edits are kept in the browser as they are typed, so nothing is lost by closing
the panel, navigating away, or reloading before saving:

- Every change is stashed in `localStorage` shortly after each keystroke, keyed by
  note path, and restored when the note is reopened. A banner reports the draft's
  age and offers to discard it.
- The draft is cleared only once the file has actually been written.
- Rows holding a draft are marked with a hollow ring in the ledger, so unsaved
  work is visible without opening each note.
- If the file changed on disk while a draft was held, the banner says so before
  anything is overwritten.
- Drafts cover the new-position form too, so a pasted advert survives a reload
  before the note exists.

Drafts are a convenience and never a requirement: if `localStorage` is
unavailable, editing still works and only the stashing is skipped.

## How writing works

Editing files in place is the risky part of a tool like this, so writes are
surgical rather than regenerative:

- Only fields whose value actually changed are rewritten. Everything else keeps
  its original bytes, including key casing (`Location:` is not normalised to
  `location:`) and literal values such as `deadline: null`.
- Frontmatter values wrapped across several lines are tracked as a single field,
  so a long title with a continuation line is never truncated.
- The body is rewritten only when it differs, so opening a note and saving it
  without touching the body leaves the file byte-identical.
- A save that would change nothing is detected and skipped, and the response
  reports `"unchanged": true`.
- Writes go to a temporary file and are then atomically renamed, so an
  interrupted save cannot truncate a note.
- The previous version is copied into `.portal-backups/`, mirroring the original
  path with a timestamp in the filename.
- If a note changed on disk after it was opened, saving returns `409` and asks for
  a reload instead of overwriting, so editing the same note in another
  application at the same time is safe.

These properties are covered by a round-trip test: read every note through the
API, save it back unmodified, and assert that every file is byte-identical
afterwards. It found four real bugs when it was first run, including one case of
dropped data.

Body indentation is flattened before rendering, because adverts pasted from a
browser commonly arrive with four-space-indented bullets that markdown would
otherwise render as code blocks. Fenced code blocks are left as written. This
affects rendering only and never the stored file.

## API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/` | The page. |
| `GET` | `/healthz` | Unauthenticated liveness, note count, active renderer. |
| `GET` | `/api/positions` | All notes as JSON, with derived date arithmetic. |
| `GET` | `/api/note?path=Positions/…md` | One note: frontmatter, raw body, rendered HTML, mtime. |
| `PUT` | `/api/note` | Save. `{path, frontmatter?, body?, mtime?}`. Passing `mtime` enables conflict detection. |
| `POST` | `/api/notes` | Create. `{frontmatter:{company, job_title, …}, body?, year?}`. |

Client-supplied paths are resolved and rejected unless they land inside
`Positions/` and end in `.md`, so traversal attempts such as `../../etc/passwd`
and `Positions/../secrets.md` both fail.

## Deployment notes

- **File ownership.** The container writes as whatever uid it runs as. On Linux,
  set `user: "<uid>:<gid>"` in the compose file to the owner of the notes folder,
  or new files and backups end up owned by root.
- **Exposure.** There is no TLS and no user accounts, only the optional shared
  token. Anything that can reach the port can rewrite the notes, so keep it on a
  trusted network or behind a reverse proxy, and set `AUTH_TOKEN` if it is
  reachable from anywhere else.
- **Sync tools.** If the folder is replicated by a file-sync tool, consider adding
  `.portal-backups` to its ignore rules.

## Licence

MIT. See [LICENSE](LICENSE).
