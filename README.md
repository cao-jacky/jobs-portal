# Jobs Portal

A small web service over the `Jobs/` notes folder. Same ledger as the static
dashboard, plus an inline viewer for each note and an editor that writes back to
the original markdown file. Built to run in Docker on the home lab with the notes
folder bind-mounted in.

The notes stay the source of truth. There is no database, no index, and no copy
of the data. Every request reads the files, and every save rewrites the file.

## Running it

### Docker Compose

Edit the volume line in `docker-compose.yml` so the left side points at the folder
that contains `Positions/`, then:

```
docker compose up -d --build
```

It listens on <http://localhost:8412> by default.

### Directly

```
JOBS_DIR="/path/to/Jobs" python3 app.py
```

Python 3.11 or newer, no virtualenv needed. `pip install markdown` is optional
and improves note rendering; without it a built-in subset renderer is used.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `JOBS_DIR` | `.` | Folder containing `Positions/`. In the image this is `/data`. |
| `PORT` | `8080` | Listen port inside the container. |
| `HOST` | `0.0.0.0` | Bind address. |
| `AUTH_TOKEN` | unset | When set, every request needs it as a `Bearer` token. The page prompts once and stores it in `localStorage`. `/healthz` stays open so the Docker healthcheck works. |
| `READ_ONLY` | unset | Set to `1` to refuse every write. The page hides the save controls and shows a read-only badge. |
| `BACKUPS` | `1` | Copy a note into `.portal-backups/` before overwriting it. Set to `0` to stop. |

## Using it

- **Click any row** to open the note. The advert renders inline; the drawer header
  carries the path, location, status, and deadline.
- **Edit tab** exposes every frontmatter field plus the note body. `Cmd/Ctrl+S`
  saves, `Esc` closes, and the tab warns before discarding unsaved changes.
- **The status dropdown in the table** writes immediately, so marking something
  Applied is one click and does not need the drawer.
- **New position** creates `Positions/<year>/<Company> - <Title>.md` with the
  standard frontmatter and an empty Description, ready for a pasted advert.
- **Deep links**: the open note is in the URL hash, so a note can be bookmarked
  or shared, and the back button works.
- **Open deadlines** and **ageing applications** are clickable and jump to the note.

## How writing works, and why it is careful

Editing real notes in place is the risky part of this service, so writes are
surgical rather than regenerative:

- Only fields whose value actually changed are rewritten. Everything else keeps
  its original bytes, including key casing (`Location:` stays `Location:`) and
  literal values like `deadline: null`.
- Values wrapped across several lines are tracked as one field, so a long
  `job_title` with a continuation line is not truncated.
- The body is only rewritten if it actually differs, so opening and saving a note
  without touching the body leaves it byte-identical.
- A save that changes nothing is detected and skipped, and the response says
  `"unchanged": true`.
- Every write goes to a temporary file and is then atomically renamed, so an
  interrupted save cannot truncate a note.
- A copy of the previous version is kept under `.portal-backups/` mirroring the
  original path, with a timestamp in the filename.
- If the note changed on disk after it was opened, saving returns `409` and asks
  for a reload rather than overwriting whatever changed. Editing the same note in
  Obsidian at the same time is therefore safe.

This was verified by round-tripping all 103 notes through read-then-save and
confirming every file was byte-identical afterwards.

Indentation in note bodies is flattened before rendering, because adverts pasted
from a browser usually arrive with four-space-indented bullets that markdown
would otherwise show as code blocks. Fenced blocks are left alone. This affects
rendering only, never the stored file.

## API

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/` | The page. |
| `GET` | `/healthz` | Unauthenticated liveness, note count, renderer in use. |
| `GET` | `/api/positions` | Every note as JSON with derived date arithmetic. |
| `GET` | `/api/note?path=Positions/…md` | One note: frontmatter, raw body, rendered HTML, mtime. |
| `PUT` | `/api/note` | Save. Body: `{path, frontmatter?, body?, mtime?}`. Send `mtime` to get conflict detection. |
| `POST` | `/api/notes` | Create. Body: `{frontmatter:{company, job_title, …}, body?, year?}`. |

Paths are resolved and rejected unless they land inside `Positions/` and end in
`.md`, so `../../etc/passwd` and `Positions/../PROFILE.md` both fail.

## Deploying on the home lab

Two things to get right:

1. **File ownership.** The container writes as whatever uid it runs as. On a Linux
   host, uncomment `user: "1000:1000"` in the compose file and set it to the owner
   of the notes folder (`id -u`, `id -g`), otherwise new files and backups end up
   owned by root and Obsidian or Syncthing may struggle with them.
2. **Exposure.** There is no HTTPS and no login beyond the optional shared token.
   Keep it on the LAN or behind the existing reverse proxy and VPN, and set
   `AUTH_TOKEN` if it is reachable from anywhere but the local machine. Anything
   that can reach this port can rewrite the notes.

If Syncthing is also syncing the folder, add `.portal-backups` to its ignore
patterns unless the backups are wanted on every device.

## Relationship to the static dashboard

`Jobs/build_dashboard.py` still works and still produces a single self-contained
`dashboard.html` with no server. Keep it for an offline snapshot. The portal is
the live, writable version of the same view; the two share the design tokens and
the derived metrics but not any code.
