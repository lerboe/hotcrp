import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import hotcrp_upload as hu  # noqa: E402

PDF = b"%PDF-1.5\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"

TOML = """
[server]
site = "{site}"
paper-id = "new"

[paper]
pdf = "paper.pdf"
title = "  A Title "
abstract = "An abstract."
topics = ["Systems"]

[[paper.authors]]
first = "Ada"
last = "Lovelace"
email = "ada@example.org"
affiliation = "Engines"
contact = true

[[paper.authors]]
name = "Bob Smith"

[paper.pc_conflicts]
"pc@example.com" = "collaborator"

[paper.fields]
artifact_url = "https://example.org"
"""


class Mock(BaseHTTPRequestHandler):
    calls: list = []
    formatcheck = {"ok": True, "npages": 12, "nwords": 5000, "problem_fields": [],
                   "has_error": False, "message_list": []}
    save_response = None

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _common(self, method):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        Mock.calls.append((method, u.path, q, self.headers.get("Content-Type"), body))
        if self.headers.get("Authorization") != "bearer hct_test":
            return self._send({"ok": False, "message_list": [
                {"status": 2, "message": "<0>Bad token"}]}, 403)
        if u.path == "/api/whoami":
            return self._send({"ok": True, "email": "ada@example.org", "roles": ["author"]})
        if u.path == "/api/paper":
            if Mock.save_response:
                return self._send(Mock.save_response)
            dry = q.get("dry_run") == "1"
            r = {"ok": True, "valid": True, "pid": 7, "change_list": ["new", "title"],
                 "message_list": []}
            if dry:
                r["dry_run"] = True
            else:
                r["paper"] = {"object": "paper", "pid": 7, "status": "submitted"}
            return self._send(r)
        if u.path == "/api/formatcheck":
            return self._send(Mock.formatcheck)
        if u.path == "/api/upload":
            return self._send({"ok": True, "token": "hcupTEST"})
        self._send({"ok": False}, 404)

    def do_GET(self):
        self._common("GET")

    def do_POST(self):
        self._common("POST")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Mock)
        cls.site = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Mock.calls = []
        Mock.save_response = None
        Mock.formatcheck = dict(Base.default_fc)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "paper.pdf").write_bytes(PDF)
        (self.dir / "hotcrp.toml").write_text(TOML.format(site=self.site))

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *extra, token="hct_test"):
        import os
        old = os.environ.get("HOTCRP_TOKEN")
        os.environ["HOTCRP_TOKEN"] = token
        try:
            return hu.main(["--config", str(self.dir / "hotcrp.toml"), *extra])
        finally:
            if old is None:
                os.environ.pop("HOTCRP_TOKEN", None)
            else:
                os.environ["HOTCRP_TOKEN"] = old

    def paper_posts(self):
        return [c for c in Mock.calls if c[1] == "/api/paper"]


Base.default_fc = dict(Mock.formatcheck)


class TestConfig(Base):
    def test_build_json(self):
        cfg = hu.load_config(self.dir / "hotcrp.toml")
        j = hu.build_paper_json(cfg, "paper.pdf")
        self.assertEqual(j["title"], "A Title")
        self.assertEqual(j["pid"], "new")
        self.assertEqual(j["authors"][0], {"first": "Ada", "last": "Lovelace",
                                           "email": "ada@example.org",
                                           "affiliation": "Engines"})
        self.assertEqual(j["authors"][1], {"name": "Bob Smith"})
        self.assertEqual(j["contacts"], ["ada@example.org"])
        self.assertEqual(j["submission"]["content_file"], "paper.pdf")
        self.assertEqual(j["artifact_url"], "https://example.org")
        self.assertEqual(j["status"], "submitted")

    def bad(self, text, needle):
        (self.dir / "hotcrp.toml").write_text(text)
        with self.assertRaises(hu.UploadError) as cm:
            hu.load_config(self.dir / "hotcrp.toml")
        self.assertIn(needle, str(cm.exception))

    def test_errors(self):
        self.bad("[paper]\ntitel = 'x'", "unknown keys")
        self.bad("[server]\nstatus = 'done'", "status")
        self.bad("[server]\npaper-id = -3", "paper id")
        self.bad("[[paper.authors]]\nemail = 'nope'", "invalid email")
        self.bad("[[paper.authors]]\naffiliation = 'X'", "needs a name")
        self.bad("[paper.fields]\ntitle = 'x'", "redefine")
        self.bad("[paper]\ntitle = [", "invalid TOML")

    def test_local_pdf_checks(self):
        self.assertEqual(hu.check_pdf_locally(PDF, None), [])
        self.assertTrue(hu.check_pdf_locally(b"hello", None))
        self.assertTrue(hu.check_pdf_locally(PDF, 0.00001))


class TestFlow(Base):
    def test_full_upload(self):
        self.assertEqual(self.run_main(), 0)
        posts = self.paper_posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2].get("dry_run"), "1")
        self.assertNotIn("dry_run", posts[1][2])
        self.assertEqual(posts[1][2]["p"], "new")
        self.assertEqual(posts[1][3], "application/zip")
        z = zipfile.ZipFile(io.BytesIO(posts[1][4]))
        self.assertEqual(z.read("paper.pdf"), PDF)
        self.assertEqual(json.loads(z.read("data.json"))["title"], "A Title")
        fc = [c for c in Mock.calls if c[1] == "/api/formatcheck"]
        self.assertEqual(fc[0][2], {"p": "7", "dt": "0", "detail": "1"})

    def test_dry_run(self):
        self.assertEqual(self.run_main("--dry-run", "true"), 0)
        self.assertEqual(len(self.paper_posts()), 1)
        self.assertFalse([c for c in Mock.calls if c[1] == "/api/formatcheck"])

    def test_bad_token(self):
        self.assertEqual(self.run_main(token="wrong"), 1)
        self.assertFalse(self.paper_posts())

    def test_invalid(self):
        Mock.save_response = {"ok": False, "valid": False, "message_list": [
            {"status": 2, "field": "abstract", "message": "<0>Entry required"}]}
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(len(self.paper_posts()), 1)

    def test_warning_fails_when_requested(self):
        Mock.save_response = {"ok": True, "valid": True, "pid": 7, "message_list": [
            {"status": 1, "field": "title", "message": "<0>Avoid superlatives"}]}
        self.assertEqual(self.run_main("--fail-on-warning", "true"), 1)
        self.assertEqual(self.run_main("--validate-first", "false"), 0)

    def test_format_problems(self):
        Mock.formatcheck = {"ok": True, "npages": 20, "problem_fields": ["pagelimit"],
                            "has_error": True, "message_list": []}
        self.assertEqual(self.run_main(), 1)
        self.assertEqual(self.run_main("--fail-on-format-error", "false"), 0)

    def test_max_pages(self):
        self.assertEqual(self.run_main("--max-pages", "10"), 1)
        self.assertEqual(self.run_main("--max-pages", "12"), 0)

    def test_existing_paper_and_final(self):
        self.assertEqual(self.run_main("--paper-id", "7", "--document", "final"), 0)
        z = zipfile.ZipFile(io.BytesIO(self.paper_posts()[-1][4]))
        data = json.loads(z.read("data.json"))
        self.assertEqual(data["pid"], 7)
        self.assertIn("final", data)
        self.assertNotIn("submission", data)
        fc = [c for c in Mock.calls if c[1] == "/api/formatcheck"]
        self.assertEqual(fc[0][2]["dt"], "-1")

    def test_final_requires_pid(self):
        self.assertEqual(self.run_main("--document", "final"), 1)

    def test_chunked(self):
        self.assertEqual(self.run_main("--upload-method", "chunked"), 0)
        posts = self.paper_posts()
        self.assertTrue(all(p[2].get("upload") == "hcupTEST" for p in posts))
        self.assertTrue(all(p[4] == b"" for p in posts))
        ups = [c for c in Mock.calls if c[1] == "/api/upload"]
        self.assertIn(b'name="start"', ups[0][4])
        self.assertIn(b'name="finish"', ups[0][4])


class TestGitLab(Base):
    def test_dotenv_and_env_inputs(self):
        import os
        from unittest import mock
        dotenv = self.dir / "hotcrp.env"
        summary = self.dir / "summary.md"
        env = {"GITLAB_CI": "true", "HOTCRP_DOTENV": str(dotenv),
               "HOTCRP_SUMMARY_FILE": str(summary), "HOTCRP_TOKEN": "hct_test",
               "HOTCRP_CONFIG": str(self.dir / "hotcrp.toml"),
               "HOTCRP_MAX_PAGES": "11", "HOTCRP_PAPER_ID": "7"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("GITHUB_ACTIONS", None)
            os.environ.pop("GITHUB_OUTPUT", None)
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            self.assertEqual(hu.main([]), 1)  # 12 pages > 11
        vals = dict(l.split("=", 1) for l in dotenv.read_text().splitlines())
        self.assertEqual(vals["HOTCRP_RESULT_PAPER_ID"], "7")
        self.assertEqual(vals["HOTCRP_RESULT_NPAGES"], "12")
        self.assertEqual(vals["HOTCRP_RESULT_FORMAT_PROBLEMS"], "[]")
        self.assertIn("HotCRP upload", summary.read_text())
        self.assertEqual(self.paper_posts()[-1][2]["p"], "7")


class TestInputParity(unittest.TestCase):
    """action.yml inputs, templates/upload.yml inputs and script env names agree."""

    def test_parity(self):
        import re
        root = Path(__file__).resolve().parent.parent
        src = (root / "src" / "hotcrp_upload.py").read_text()
        script = set(re.findall(r'env\("INPUT_([A-Z_]+)"', src)) | {"TOKEN"}
        action = (root / "action.yml").read_text()
        inputs_block = action.split("\ninputs:\n", 1)[1].split("\noutputs:\n", 1)[0]
        gh_inputs = set(re.findall(r"^  ([a-z-]+):\n", inputs_block, re.M))
        gh = {i.upper().replace("-", "_") for i in gh_inputs}
        self.assertEqual(gh, script)
        for i in gh_inputs:
            self.assertIn(f"INPUT_{i.upper().replace('-', '_')}: ${{{{ inputs.{i} }}}}",
                          action.replace("HOTCRP_TOKEN: ${{ inputs.token }}",
                                         "INPUT_TOKEN: ${{ inputs.token }}"))
        tpl = (root / "templates" / "upload.yml").read_text()
        gl = set(re.findall(r'HOTCRP_([A-Z_]+): "\$\[\[ inputs\.([a-z-]+) \]\]"', tpl))
        for var, inp in gl:
            self.assertEqual(var, inp.upper().replace("-", "_"))
        self.assertEqual({v for v, _ in gl}, script - {"TOKEN"})


if __name__ == "__main__":
    unittest.main()
