# hotcrp

GitHub Action that uploads a rendered PDF plus metadata from `hotcrp.toml` to a
[HotCRP](https://hotcrp.com) site, then runs HotCRP's format checker.

Uses the HotCRP REST API ([reference](https://hotcrp.com/devel/api/)). Python
stdlib only.

## Usage

```yaml
- uses: lerboe/hotcrp@main
  with:
    token: ${{ secrets.HOTCRP_TOKEN }}
    pdf: build/paper.pdf
    max-pages: 12
```

Full example: [`examples/hotcrp-submit.yml`](examples/hotcrp-submit.yml).

### Token

Create in HotCRP: *Account settings → Developer*. Store as repo secret
`HOTCRP_TOKEN`. Scopes needed:

| Scope | For |
|---|---|
| `submeta:write` | create/update submission metadata (`POST /api/paper`) |
| `document:write` | attach the PDF; `/api/upload` |
| `document:read` | format check (`GET /api/formatcheck`) |

`paper:write` covers all three. Narrow the token to your paper (`#42`) once it exists.

## `hotcrp.toml`

See [`hotcrp.example.toml`](hotcrp.example.toml).

### `[server]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `site` | string | — | Site URL, e.g. `https://conf2027.hotcrp.com` |
| `paper-id` | int or `"new"` | `"new"` | Submission ID. `"new"` creates a submission |
| `status` | `"submitted"` / `"draft"` | `"submitted"` | Mark ready for review or keep as draft |
| `document` | `"submission"` / `"final"` | `"submission"` | Which PDF slot: submission or camera-ready |

### `[paper]`

| Key | Type | HotCRP JSON field |
|---|---|---|
| `pdf` | path, relative to toml | attached as `submission` / `final` |
| `title` | string (required for new papers) | `title` |
| `abstract` | string | `abstract` |
| `topics` | string array (site topic names) | `topics` |
| `collaborators` | string, one per line | `collaborators` |
| `contacts` | email array (non-author contacts) | `contacts` |
| `[[paper.authors]]` | array of tables, ordered | `authors` |
| `[paper.pc_conflicts]` | PC email → conflict type or `true` | `pc_conflicts` |
| `[paper.fields]` | any site-specific fields | merged verbatim into paper JSON |

Author keys: `first`, `last` (or `name`), `email`, `affiliation`, `contact`
(bool; adds the email to `contacts`).

`[paper.fields]` keys are the site's JSON field names. Find them via
`GET /api/{p}/paper?download=1` on an existing submission.

Unknown keys fail fast, so typos don't silently drop fields.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `token` | **required** | API token |
| `pdf` | `[paper].pdf` | PDF path |
| `config` | `hotcrp.toml` | Config path |
| `site`, `paper-id`, `status`, `document` | from toml | Override `[server]` |
| `dry-run` | `false` | Validate only (`dry_run=1`); nothing saved, no format check |
| `validate-first` | `true` | Dry-run before the real save |
| `check-format` | `true` | Run `/api/formatcheck` after upload |
| `fail-on-format-error` | `true` | Fail on format problems (page limit, paper size, font size, ...) |
| `fail-on-warning` | `false` | Fail on validation warnings |
| `max-pages` | — | Fail if server page count exceeds this |
| `max-size-mb` | — | Fail locally if PDF is larger |
| `ignore-unwritable-fields` | `false` | Skip fields locked by the site (e.g. after deadline) |
| `upload-method` | `auto` | `direct` (ZIP body), `chunked` (`/api/upload`, needs document store), `auto` (chunked above 10 MB) |
| `reason` | — | Change reason, included in notification emails |
| `timeout` | `300` | HTTP timeout, seconds |

## Outputs

`paper-id`, `paper-url`, `paper-status`, `valid`, `change-list` (JSON),
`npages`, `nwords`, `format-ok`, `format-problems` (JSON).

## Flow

1. Parse and validate `hotcrp.toml`.
2. Local PDF sanity checks: `%PDF-` header, `%%EOF` trailer, not encrypted, size.
3. `GET /api/whoami` — verify token.
4. Build ZIP: `data.json` (paper object) + `paper.pdf`.
5. `POST /api/paper?p=…&dry_run=1` — validate. Errors/warnings become annotations.
6. `POST /api/paper?p=…` — save. Checks `ok` **and** `valid`.
7. `GET /api/formatcheck?p=…&dt=0|-1&detail=1` — page count, problem fields.
8. Write outputs and a job summary.

After the first upload with `paper-id = "new"`, set `paper-id` to the returned
ID. Otherwise every run creates a new submission.

## Caveats

- The public API docs don't list every paper JSON field. Field names here
  (`abstract`, `topics`, `authors[].first/last/affiliation`, `contacts`,
  `pc_conflicts`, `collaborators`, `final`) follow HotCRP's paper JSON format.
  Run with `dry-run: true` first against your site.
- Author emails in `hotcrp.toml` are public if the repo is public.
- Format check needs a stored document, so it is skipped in dry-run mode.

## Development

```bash
python -m unittest discover -s tests -v
```

Tests run against a mock HotCRP server.
