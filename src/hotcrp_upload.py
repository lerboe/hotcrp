#!/usr/bin/env python3
"""Upload a PDF plus metadata from hotcrp.toml to a HotCRP site.

Stdlib only (Python >= 3.11 for tomllib). See README.md for the
hotcrp.toml schema and the action inputs.

API reference: https://hotcrp.com/devel/api/
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

USER_AGENT = "hotcrp-ci-action/1"
CHUNK_SIZE = 5 * 1024 * 1024

# HotCRP document types: 0 = submission PDF, -1 = final (camera-ready) PDF.
DOCUMENT_KINDS = {"submission": 0, "final": -1}
STATUSES = {"submitted", "draft"}

# Keys in [paper] that map 1:1 onto HotCRP paper JSON fields.
SIMPLE_PAPER_KEYS = ("title", "abstract", "collaborators")
AUTHOR_KEYS = {"first", "last", "name", "email", "affiliation"}

# HotCRP message_list status codes.
MSG_ERROR = 2
MSG_WARNING = 1


class UploadError(Exception):
    pass


# --------------------------------------------------------------------------
# GitHub Actions helpers


def gh_annotate(level: str, msg: str, title: str | None = None) -> None:
    """Emit a workflow command (::error::, ::warning::, ::notice::)."""
    msg = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    props = f" title={title}" if title else ""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level}{props}::{msg}")
    else:
        print(f"[{level}] {msg}", file=sys.stderr)


def gh_output(name: str, value: Any) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if isinstance(value, (list, dict)):
        value = json.dumps(value)
    elif isinstance(value, bool):
        value = "true" if value else "false"
    value = "" if value is None else str(value)
    if path:
        delim = f"EOF_{uuid.uuid4().hex}"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}<<{delim}\n{value}\n{delim}\n")
    else:
        print(f"output {name}={value}")


def gh_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def strip_sigil(message: str) -> str:
    """HotCRP messages start with a format sigil like <0>, <1>, <5>."""
    return re.sub(r"^<\d+>", "", message or "")


# --------------------------------------------------------------------------
# Config


@dataclass
class Config:
    site: str | None = None
    paper_id: int | str = "new"
    status: str = "submitted"
    document: str = "submission"
    pdf: str | None = None
    paper: dict[str, Any] = field(default_factory=dict)


def _expect(cond: bool, msg: str) -> None:
    if not cond:
        raise UploadError(f"hotcrp.toml: {msg}")


def parse_paper_id(value: Any) -> int | str:
    if value is None or value == "" or value == "new":
        return "new"
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    raise UploadError(f"invalid paper id {value!r} (positive integer or \"new\")")


def load_config(path: Path) -> Config:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise UploadError(f"config file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise UploadError(f"{path}: invalid TOML: {e}")

    unknown = set(data) - {"server", "paper"}
    _expect(not unknown, f"unknown top-level tables: {sorted(unknown)}")

    cfg = Config()
    server = data.get("server", {})
    _expect(isinstance(server, dict), "[server] must be a table")
    unknown = set(server) - {"site", "paper-id", "status", "document"}
    _expect(not unknown, f"unknown keys in [server]: {sorted(unknown)}")
    cfg.site = server.get("site")
    cfg.paper_id = parse_paper_id(server.get("paper-id"))
    cfg.status = server.get("status", cfg.status)
    _expect(cfg.status in STATUSES, f"[server].status must be one of {sorted(STATUSES)}")
    cfg.document = server.get("document", cfg.document)
    _expect(cfg.document in DOCUMENT_KINDS,
            f"[server].document must be one of {sorted(DOCUMENT_KINDS)}")

    paper = data.get("paper", {})
    _expect(isinstance(paper, dict), "[paper] must be a table")
    cfg.pdf = paper.pop("pdf", None)
    cfg.paper = paper
    validate_paper(paper)
    return cfg


def validate_paper(paper: dict[str, Any]) -> None:
    allowed = set(SIMPLE_PAPER_KEYS) | {"authors", "topics", "contacts",
                                        "pc_conflicts", "fields"}
    unknown = set(paper) - allowed
    _expect(not unknown,
            f"unknown keys in [paper]: {sorted(unknown)} "
            "(put site-specific submission fields under [paper.fields])")

    for k in SIMPLE_PAPER_KEYS:
        if k in paper:
            _expect(isinstance(paper[k], str), f"[paper].{k} must be a string")
    if "title" in paper:
        _expect(paper["title"].strip() != "", "[paper].title is empty")

    authors = paper.get("authors", [])
    _expect(isinstance(authors, list), "[[paper.authors]] must be an array of tables")
    for i, a in enumerate(authors, 1):
        _expect(isinstance(a, dict), f"author #{i} must be a table")
        unknown = set(a) - AUTHOR_KEYS - {"contact"}
        _expect(not unknown, f"author #{i}: unknown keys {sorted(unknown)}")
        _expect(any(a.get(k) for k in ("first", "last", "name", "email")),
                f"author #{i}: needs a name or email")
        if "email" in a:
            _expect(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a["email"] or "") is not None,
                    f"author #{i}: invalid email {a['email']!r}")
        if "contact" in a:
            _expect(isinstance(a["contact"], bool), f"author #{i}: contact must be a boolean")
            _expect(not a["contact"] or a.get("email"),
                    f"author #{i}: contact = true requires an email")

    for key in ("topics", "contacts"):
        if key in paper:
            _expect(isinstance(paper[key], list) and all(isinstance(t, str) for t in paper[key]),
                    f"[paper].{key} must be an array of strings")

    if "pc_conflicts" in paper:
        pcc = paper["pc_conflicts"]
        _expect(isinstance(pcc, dict) and all(isinstance(v, (str, bool)) for v in pcc.values()),
                "[paper.pc_conflicts] must map PC emails to a conflict type string or true")

    if "fields" in paper:
        _expect(isinstance(paper["fields"], dict), "[paper.fields] must be a table")
        clash = set(paper["fields"]) & (allowed | {"pid", "object", "status",
                                                    "submission", "final"})
        _expect(not clash, f"[paper.fields] must not redefine {sorted(clash)}")


def build_paper_json(cfg: Config, pdf_name: str) -> dict[str, Any]:
    p = cfg.paper
    out: dict[str, Any] = {"object": "paper", "pid": cfg.paper_id}
    for k in SIMPLE_PAPER_KEYS:
        if k in p:
            out[k] = p[k].strip()
    if "authors" in p:
        out["authors"] = [
            {k: v.strip() for k, v in a.items() if k in AUTHOR_KEYS and v}
            for a in p["authors"]
        ]
    contacts = list(p.get("contacts", []))
    contacts += [a["email"] for a in p.get("authors", [])
                 if a.get("contact") and a["email"] not in contacts]
    if contacts:
        out["contacts"] = contacts
    if "topics" in p:
        out["topics"] = list(p["topics"])
    if "pc_conflicts" in p:
        out["pc_conflicts"] = dict(p["pc_conflicts"])
    out.update(p.get("fields", {}))
    out[cfg.document] = {"content_file": pdf_name, "mimetype": "application/pdf"}
    out["status"] = cfg.status
    return out


# --------------------------------------------------------------------------
# Local PDF checks


def check_pdf_locally(data: bytes, max_size_mb: float | None) -> list[str]:
    """Return a list of problems. Cheap sanity checks only; the server does
    the real format check."""
    problems = []
    if not data.startswith(b"%PDF-"):
        problems.append("file does not start with %PDF- (not a PDF?)")
    if b"%%EOF" not in data[-2048:]:
        problems.append("no %%EOF marker near end of file (truncated PDF?)")
    if b"/Encrypt" in data:
        problems.append("PDF appears to be encrypted; HotCRP cannot format-check it")
    if max_size_mb is not None and len(data) > max_size_mb * 1024 * 1024:
        problems.append(f"file is {len(data) / 1048576:.1f} MB, limit is {max_size_mb} MB")
    return problems


# --------------------------------------------------------------------------
# HTTP


def encode_multipart(fields: dict[str, Any],
                     files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    boundary = f"----hotcrpci{uuid.uuid4().hex}"
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    for name, (filename, content, ctype) in files.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                  f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode())
        buf.write(content)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


class Client:
    def __init__(self, site: str, token: str, timeout: float = 300):
        site = site.rstrip("/")
        if not re.match(r"^https?://", site):
            raise UploadError(f"site must be an http(s) URL: {site!r}")
        if site.startswith("http://") and not re.match(r"^http://(localhost|127\.)", site):
            gh_annotate("warning", "site uses plain http; the API token is sent unencrypted")
        self.site = site
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                body: bytes | None = None, content_type: str | None = None) -> dict[str, Any]:
        url = f"{self.site}/api/{path}"
        if params:
            q = {k: ("1" if v is True else "0" if v is False else v)
                 for k, v in params.items() if v is not None}
            url += "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"bearer {self.token}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except urllib.error.URLError as e:
            raise UploadError(f"{method} {path}: cannot reach {self.site}: {e.reason}")
        try:
            result = json.loads(raw)
        except ValueError:
            snippet = raw[:200].decode("utf-8", "replace")
            raise UploadError(f"{method} {path}: HTTP {status}, non-JSON response: {snippet!r}")
        if not isinstance(result, dict):
            raise UploadError(f"{method} {path}: unexpected response {result!r}")
        result.setdefault("_http_status", status)
        return result

    # -- endpoints

    def whoami(self) -> dict[str, Any]:
        return self.request("GET", "whoami")

    def upload_chunked(self, data: bytes, filename: str, mimetype: str) -> str:
        """POST /api/upload in chunks; returns an upload token."""
        token = None
        offset = 0
        size = len(data)
        while True:
            chunk = data[offset:offset + CHUNK_SIZE]
            fields: dict[str, Any] = {"offset": offset, "length": len(chunk)}
            if token is None:
                fields.update(start=1, size=size, temp=1, mimetype=mimetype, filename=filename)
            else:
                fields["token"] = token
            last = offset + len(chunk) >= size
            if last:
                fields["finish"] = 1
            body, ctype = encode_multipart(fields, {"blob": (filename, chunk, mimetype)})
            r = self.request("POST", "upload", body=body, content_type=ctype)
            if not r.get("ok"):
                raise UploadError("upload failed: " + format_messages(r) +
                                  " (the upload API needs the site's document store; "
                                  "try upload-method: direct)")
            token = r.get("token") or token
            offset += len(chunk)
            if last:
                if not token:
                    raise UploadError("upload finished without a token")
                return token

    def save_paper(self, pid: int | str, *, zip_data: bytes | None = None,
                   upload_token: str | None = None, dry_run: bool = False,
                   ignore_unwritable: bool = False, reason: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"p": pid, "dry_run": dry_run or None,
                                  "ignore_unwritable_fields": ignore_unwritable or None,
                                  "reason": reason or None}
        if upload_token:
            params["upload"] = upload_token
            return self.request("POST", "paper", params=params)
        return self.request("POST", "paper", params=params, body=zip_data,
                            content_type="application/zip")

    def formatcheck(self, pid: int, dt: int) -> dict[str, Any]:
        return self.request("GET", "formatcheck", params={"p": pid, "dt": dt, "detail": True})


def format_messages(r: dict[str, Any], min_status: int = MSG_WARNING) -> str:
    msgs = [m for m in r.get("message_list", []) if m.get("status", 0) >= min_status]
    if not msgs:
        return f"HTTP {r.get('_http_status')}, no details"
    return "; ".join(
        (f"{m['field']}: " if m.get("field") else "") + strip_sigil(m.get("message", ""))
        for m in msgs)


def report_messages(r: dict[str, Any], title: str) -> tuple[int, int]:
    """Annotate message_list entries; return (errors, warnings)."""
    errors = warnings = 0
    for m in r.get("message_list", []):
        st = m.get("status", 0)
        text = (f"{m['field']}: " if m.get("field") else "") + strip_sigil(m.get("message", ""))
        if not text.strip():
            continue
        if st >= MSG_ERROR:
            errors += 1
            gh_annotate("error", text, title)
        elif st == MSG_WARNING:
            warnings += 1
            gh_annotate("warning", text, title)
        else:
            print(f"  {text}")
    return errors, warnings


# --------------------------------------------------------------------------
# Main


def make_zip(paper_json: dict[str, Any], pdf_name: str, pdf_data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("data.json", json.dumps(paper_json, indent=2, ensure_ascii=False))
        z.writestr(pdf_name, pdf_data, compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


def str2bool(v: str) -> bool:
    v = (v or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("", "0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"not a boolean: {v!r}")


def opt_float(v: str) -> float | None:
    return float(v) if v and v.strip() else None


def opt_int(v: str) -> int | None:
    return int(v) if v and v.strip() else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=env("INPUT_CONFIG", "hotcrp.toml"))
    ap.add_argument("--pdf", default=env("INPUT_PDF", ""))
    ap.add_argument("--site", default=env("INPUT_SITE", ""))
    ap.add_argument("--paper-id", default=env("INPUT_PAPER_ID", ""))
    ap.add_argument("--status", default=env("INPUT_STATUS", ""))
    ap.add_argument("--document", default=env("INPUT_DOCUMENT", ""))
    ap.add_argument("--dry-run", type=str2bool, default=env("INPUT_DRY_RUN", "false"))
    ap.add_argument("--validate-first", type=str2bool,
                    default=env("INPUT_VALIDATE_FIRST", "true"))
    ap.add_argument("--check-format", type=str2bool, default=env("INPUT_CHECK_FORMAT", "true"))
    ap.add_argument("--fail-on-format-error", type=str2bool,
                    default=env("INPUT_FAIL_ON_FORMAT_ERROR", "true"))
    ap.add_argument("--fail-on-warning", type=str2bool,
                    default=env("INPUT_FAIL_ON_WARNING", "false"))
    ap.add_argument("--max-pages", type=opt_int, default=env("INPUT_MAX_PAGES", ""))
    ap.add_argument("--max-size-mb", type=opt_float, default=env("INPUT_MAX_SIZE_MB", ""))
    ap.add_argument("--ignore-unwritable-fields", type=str2bool,
                    default=env("INPUT_IGNORE_UNWRITABLE_FIELDS", "false"))
    ap.add_argument("--upload-method", choices=["direct", "chunked", "auto"],
                    default=env("INPUT_UPLOAD_METHOD", "") or "auto")
    ap.add_argument("--reason", default=env("INPUT_REASON", ""))
    ap.add_argument("--timeout", type=float, default=float(env("INPUT_TIMEOUT", "") or 300))
    args = ap.parse_args(argv)
    args.token = env("HOTCRP_TOKEN", "") or env("INPUT_TOKEN", "")
    return args


def run(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    config_dir = Path(args.config).parent

    # Inputs override hotcrp.toml.
    if args.site:
        cfg.site = args.site
    if args.paper_id:
        cfg.paper_id = parse_paper_id(args.paper_id)
    if args.status:
        if args.status not in STATUSES:
            raise UploadError(f"status must be one of {sorted(STATUSES)}")
        cfg.status = args.status
    if args.document:
        if args.document not in DOCUMENT_KINDS:
            raise UploadError(f"document must be one of {sorted(DOCUMENT_KINDS)}")
        cfg.document = args.document
    if not cfg.site:
        raise UploadError("no HotCRP site: set the `site` input or [server].site")
    if not args.token:
        raise UploadError("no API token: set the `token` input (from a secret)")
    if cfg.document == "final" and cfg.paper_id == "new":
        raise UploadError("document = \"final\" requires an existing paper-id")
    if cfg.paper_id == "new" and "title" not in cfg.paper:
        raise UploadError("new submissions need [paper].title")

    pdf_path = Path(args.pdf) if args.pdf else (config_dir / cfg.pdf if cfg.pdf else None)
    if not pdf_path:
        raise UploadError("no PDF: set the `pdf` input or [paper].pdf")
    try:
        pdf_data = pdf_path.read_bytes()
    except OSError as e:
        raise UploadError(f"cannot read PDF {pdf_path}: {e}")

    print(f"PDF: {pdf_path} ({len(pdf_data)} bytes)")
    problems = check_pdf_locally(pdf_data, args.max_size_mb)
    for p in problems:
        gh_annotate("error", p, "PDF check")
    if problems:
        return 1

    pdf_name = "paper.pdf"
    paper_json = build_paper_json(cfg, pdf_name)
    print("Paper JSON:")
    print(json.dumps({k: v for k, v in paper_json.items()}, indent=2, ensure_ascii=False))
    zip_data = make_zip(paper_json, pdf_name, pdf_data)

    client = Client(cfg.site, args.token, timeout=args.timeout)

    who = client.whoami()
    if not who.get("ok") or not who.get("email"):
        raise UploadError("token check (GET /api/whoami) failed: " + format_messages(who))
    print(f"Authenticated as {who['email']} (roles: {', '.join(who.get('roles') or []) or '-'})")

    upload_token = None
    method = args.upload_method
    if method == "auto":
        method = "chunked" if len(zip_data) > 2 * CHUNK_SIZE else "direct"
    if method == "chunked":
        print(f"Uploading {len(zip_data)} bytes via /api/upload")
        upload_token = client.upload_chunked(zip_data, "upload.zip", "application/zip")

    def save(dry: bool) -> dict[str, Any]:
        return client.save_paper(cfg.paper_id, zip_data=zip_data, upload_token=upload_token,
                                 dry_run=dry, ignore_unwritable=args.ignore_unwritable_fields,
                                 reason=args.reason)

    def check_save(r: dict[str, Any], label: str) -> bool:
        errors, warnings = report_messages(r, label)
        ok = bool(r.get("ok")) and bool(r.get("valid"))
        if not ok and errors == 0:
            gh_annotate("error", f"{label} failed: " + format_messages(r, MSG_ERROR), label)
        if ok and warnings and args.fail_on_warning:
            gh_annotate("error", f"{label}: {warnings} warning(s) and fail-on-warning is set",
                        label)
            return False
        return ok

    if args.dry_run or args.validate_first:
        print("Validating (dry run)...")
        r = save(True)
        valid = check_save(r, "Validation")
        gh_output("valid", valid)
        gh_output("change-list", r.get("change_list") or [])
        if not valid:
            return 1
        print(f"Validation passed. Changes: {', '.join(r.get('change_list') or []) or 'none'}")
        if args.dry_run:
            gh_output("paper-id", r.get("pid") or cfg.paper_id)
            gh_summary(f"### HotCRP dry run\n\nValid. Changes: "
                       f"`{', '.join(r.get('change_list') or []) or 'none'}`\n")
            if args.check_format:
                print("Note: format check is skipped in dry-run mode (needs a stored document).")
            return 0
        if upload_token:  # upload tokens may be single-use; re-upload
            upload_token = client.upload_chunked(zip_data, "upload.zip", "application/zip")

    print("Saving submission...")
    r = save(False)
    valid = check_save(r, "Upload")
    gh_output("valid", valid)
    gh_output("change-list", r.get("change_list") or [])
    if not valid:
        return 1
    pid = r.get("pid") or (r.get("paper") or {}).get("pid")
    status = (r.get("paper") or {}).get("status", cfg.status)
    url = f"{client.site}/paper/{pid}"
    gh_output("paper-id", pid)
    gh_output("paper-url", url)
    gh_output("paper-status", status)
    print(f"Saved paper #{pid} ({status}): {url}")
    if cfg.paper_id == "new":
        gh_annotate("notice", f"Created paper #{pid}. Set [server].paper-id = {pid} in "
                    f"{args.config} so later runs update it instead of creating a new one.",
                    "HotCRP")
    summary = [f"### HotCRP upload\n", f"- Paper: [#{pid}]({url}) ({status})",
               f"- Changes: `{', '.join(r.get('change_list') or []) or 'none'}`"]

    rc = 0
    if args.check_format and pid:
        print("Running format check...")
        fc = client.formatcheck(int(pid), DOCUMENT_KINDS[cfg.document])
        if not fc.get("ok"):
            gh_annotate("warning", "format check unavailable: " + format_messages(fc),
                        "Format check")
        else:
            npages = fc.get("npages")
            problems = fc.get("problem_fields") or []
            has_error = bool(fc.get("has_error"))
            report_messages(fc, "Format check")
            gh_output("npages", npages)
            gh_output("nwords", fc.get("nwords"))
            gh_output("format-problems", problems)
            gh_output("format-ok", not problems and not has_error)
            print(f"Format check: {npages} pages, {fc.get('nwords')} words, "
                  f"problems: {', '.join(problems) or 'none'}")
            summary.append(f"- Pages: {npages}, words: {fc.get('nwords')}")
            summary.append(f"- Format problems: {', '.join(problems) or 'none'}")
            if args.fail_on_format_error and (has_error or problems):
                gh_annotate("error", "format check reported problems: "
                            + (", ".join(problems) or "see messages"), "Format check")
                rc = 1
            if args.max_pages is not None and isinstance(npages, int) \
                    and npages > args.max_pages:
                gh_annotate("error", f"{npages} pages exceeds max-pages {args.max_pages}",
                            "Format check")
                rc = 1
    gh_summary("\n".join(summary) + "\n")
    return rc


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parse_args(argv))
    except UploadError as e:
        gh_annotate("error", str(e), "hotcrp-ci")
        return 1


if __name__ == "__main__":
    sys.exit(main())
