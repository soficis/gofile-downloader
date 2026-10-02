"""Regression tests locking gofile-downloader.py observable behavior.

The target module is a single-file distributable whose filename contains
hyphens, so it is loaded by path rather than imported by name.
"""

import importlib.util
import io
import re
import sys
import threading
from hashlib import md5, sha256
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "gofile-downloader.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("gofile_downloader", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gf = _load_module()


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, payload=None, body=b"", headers=None, status_code=200):
        self._payload = payload
        self._body = body
        self.headers = headers or {}
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise gf.GofileError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]


class FakeSession:
    """Records calls and replays queued responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.cookies = _FakeCookieJar()
        self.headers = dict(gf.GofileClient._DEFAULT_HEADERS)

    def _next(self):
        if not self.responses:
            raise AssertionError("FakeSession ran out of queued responses")
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._next()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._next()

    def set_cookie(self, *args, **kwargs):
        pass


class _FakeCookieJar:
    def __init__(self):
        self.store = {}

    def set(self, name, value, **kwargs):
        self.store[name] = value


# --------------------------------------------------------------------------
# generate_website_token
# --------------------------------------------------------------------------


def test_generate_website_token_is_sha256_of_the_documented_recipe(monkeypatch):
    monkeypatch.setattr(gf, "time", lambda: 1_700_000_000)
    expected = sha256(
        f"UA::{gf.WEB_LOCALE}::TOKEN::{1_700_000_000 // 14400}::{gf.WEBSITE_TOKEN_SALT}".encode()
    ).hexdigest()
    assert gf.generate_website_token("UA", "TOKEN") == expected


def test_generate_website_token_depends_on_user_agent_and_account(monkeypatch):
    monkeypatch.setattr(gf, "time", lambda: 1_700_000_000)
    base = gf.generate_website_token("UA", "TOKEN")
    assert gf.generate_website_token("OTHER", "TOKEN") != base
    assert gf.generate_website_token("UA", "OTHER") != base


def test_generate_website_token_rotates_on_the_four_hour_boundary(monkeypatch):
    monkeypatch.setattr(gf, "time", lambda: 0)
    first = gf.generate_website_token("UA", "TOKEN")
    monkeypatch.setattr(gf, "time", lambda: 14400)
    assert gf.generate_website_token("UA", "TOKEN") != first


def test_generate_website_token_accepts_empty_account_token():
    assert len(gf.generate_website_token("UA", "")) == 64


# --------------------------------------------------------------------------
# extract_content_id
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://gofile.io/d/bBmuF5aQ", "bBmuF5aQ"),
        ("bBmuF5aQ", "bBmuF5aQ"),
        ("https://gofile.io/d/abc123?x=1", "abc123"),
    ],
)
def test_extract_content_id(value, expected):
    assert gf.extract_content_id(value) == expected


def test_extract_content_id_reads_query_params_when_path_is_unhelpful():
    assert gf.extract_content_id("https://example.com/?contentId=zzz") == "zzz"


def test_extract_content_id_rejects_unparseable_input():
    with pytest.raises(ValueError):
        gf.extract_content_id("")


# --------------------------------------------------------------------------
# load_token_from_file
# --------------------------------------------------------------------------


def test_load_token_from_file_reads_first_non_empty_line(tmp_path, monkeypatch):
    (tmp_path / "api.txt").write_text("\n\nTOKENLINE\nignored\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert gf.load_token_from_file() == "TOKENLINE"


def test_load_token_from_file_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gf, "__file__", str(tmp_path / "nonexistent.py"))
    assert gf.load_token_from_file() is None


# --------------------------------------------------------------------------
# CLI parsing
# --------------------------------------------------------------------------


def _parse(argv):
    _, ns = gf.parse_cli_args(argv)
    return ns


def test_global_token_flag_parses():
    assert _parse(["--token", "GLOBAL", "download", "u"]).token == "GLOBAL"


def test_token_flag_parses_after_the_subcommand():
    assert _parse(["download", "u", "--token", "SUB"]).token == "SUB"


def test_global_token_survives_when_subcommand_omits_it():
    assert _parse(["--token", "GLOBAL", "download", "u"]).token == "GLOBAL"
    assert _parse(["download", "u"]).token is None


@pytest.mark.parametrize("sub", ["mirror", "download", "resolve", "upload"])
def test_every_subcommand_accepts_token(sub):
    argv = ["upload", "f"] if sub == "upload" else [sub, "src"]
    assert _parse([*argv, "--token", "T"]).token == "T"


def test_no_recursive_disables_recursion():
    assert _parse(["resolve", "u", "--no-recursive"]).recursive is False
    assert _parse(["resolve", "u"]).recursive is True


def test_download_defaults():
    ns = _parse(["download", "u"])
    assert ns.overwrite is False
    assert ns.file_id is None
    assert ns.dest == Path.cwd()


# --------------------------------------------------------------------------
# Manager._normalize_http_url
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("gofile.io/d/abc", "https://gofile.io/d/abc"),
        ("https://gofile.io/d/abc", "https://gofile.io/d/abc"),
        ("not a url", None),
    ],
)
def test_normalize_http_url(value, expected):
    assert gf.Manager._normalize_http_url(value) == expected


# --------------------------------------------------------------------------
# GofileClient auth plumbing
# --------------------------------------------------------------------------


def _client(token="TOKEN"):
    client = gf.GofileClient(token=token)
    client.session = FakeSession()
    return client


def test_user_agent_property_reflects_the_session_header():
    client = _client()
    assert client.user_agent == client.session.headers["User-Agent"]


def test_website_headers_carry_token_and_locale():
    client = _client()
    client.session.headers["User-Agent"] = "UA"
    headers = client._website_headers("ACCT")
    assert headers["X-BL"] == gf.WEB_LOCALE
    assert headers["X-Website-Token"] == gf.generate_website_token("UA", "ACCT")


def test_ensure_account_creates_and_caches_a_guest_token():
    client = gf.GofileClient(token=None)
    client.session = FakeSession([FakeResponse(payload={"status": "ok", "data": {"token": "T0K"}})])
    assert client.ensure_account() == "T0K"
    # cached: a second call must not consume another queued response
    assert client.ensure_account() == "T0K"
    method, url, _ = client.session.calls[0]
    assert method == "POST" and url.endswith("/accounts")


def test_ensure_account_returns_an_existing_token_without_a_request():
    client = _client("PRESET")
    assert client.ensure_account() == "PRESET"
    assert client.session.calls == []


def test_get_content_sends_token_headers_and_drops_the_wt_param():
    client = _client()
    client.session.responses = [
        FakeResponse(payload={"status": "ok", "data": {"id": "x", "type": "file"}})
    ]
    client.get_content("x")
    _, url, kwargs = client.session.calls[0]
    assert "wt=" not in url
    assert kwargs["headers"]["X-BL"] == gf.WEB_LOCALE
    assert "X-Website-Token" in kwargs["headers"]
    assert kwargs["params"]["cache"] == "true"


def test_get_content_hashes_the_password_like_upstream():
    client = _client()
    client.session.responses = [
        FakeResponse(payload={"status": "ok", "data": {"id": "x", "type": "file"}})
    ]
    client.get_content("x", password="secret")
    _, _, kwargs = client.session.calls[0]
    assert kwargs["params"]["password"] == sha256(b"secret").hexdigest()


def test_get_content_raises_on_non_ok_status():
    client = _client()
    client.session.responses = [FakeResponse(payload={"status": "error-token"})]
    with pytest.raises(gf.GofileError):
        client.get_content("x")


def test_attach_token_sets_a_real_account_token_cookie():
    client = gf.GofileClient(token=None)
    client.session = FakeSession()
    client._attach_token("TOK")
    assert client.session.cookies.store.get("accountToken") == "TOK"
    assert client.session.headers["Authorization"] == "Bearer TOK"


def test_get_server_surfaces_failure_instead_of_defaulting_to_store1():
    client = _client()
    client.session.responses = [FakeResponse(payload={"status": "error-server"})]
    with pytest.raises(gf.GofileError):
        client.get_server()


# --------------------------------------------------------------------------
# GofileClient content traversal
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"id": "a"}, "a"),
        ({"contentId": "b"}, "b"),
        ({"folderId": "c"}, "c"),
        ({"nope": 1}, None),
    ],
)
def test_extract_child_id_falls_back_across_key_names(entry, expected):
    assert gf.GofileClient._extract_child_id(entry) == expected


def test_build_file_descriptor_captures_md5():
    descriptor = gf.GofileClient._build_file_descriptor(
        {"id": "f", "name": "n.bin", "size": 5, "md5": "ABC", "link": "http://x/y"}, "parent"
    )
    assert descriptor["md5"] == "ABC"
    assert descriptor["directLink"] == "http://x/y"
    assert descriptor["parentContentId"] == "parent"


def test_build_file_descriptor_returns_none_without_a_link():
    assert gf.GofileClient._build_file_descriptor({"id": "f", "name": "n"}, "p") is None


def test_file_entries_for_node_reads_children_dict_and_filters_folders():
    node = {
        "id": "root",
        "type": "folder",
        "children": {
            "a": {"id": "a", "type": "file", "name": "a", "link": "http://x/a"},
            "b": {"id": "b", "type": "folder", "name": "b"},
        },
    }
    entries = gf.GofileClient._file_entries_for_node(_client(), node, "root")
    assert [e["name"] for e in entries] == ["a"]


def test_file_entries_for_node_handles_a_top_level_file():
    node = {"id": "root", "type": "file", "name": "solo", "link": "http://x/solo"}
    entries = gf.GofileClient._file_entries_for_node(_client(), node, "root")
    assert len(entries) == 1 and entries[0]["name"] == "solo"


def test_verify_payload_accepts_a_matching_payload(tmp_path):
    part = tmp_path / "f.part"
    part.write_bytes(b"hello")
    gf.GofileClient._verify_payload(part, 5, 5, None, "x")


def test_verify_payload_rejects_a_size_mismatch(tmp_path):
    part = tmp_path / "f.part"
    part.write_bytes(b"hello")
    with pytest.raises(gf.GofileError, match="Incomplete download"):
        gf.GofileClient._verify_payload(part, 3, 5, None, "x")


def test_verify_payload_rejects_a_checksum_mismatch(tmp_path):
    part = tmp_path / "f.part"
    part.write_bytes(b"hello")
    with pytest.raises(gf.GofileError, match="Checksum mismatch"):
        gf.GofileClient._verify_payload(part, 5, 5, "deadbeef", "x")


def test_verify_payload_is_case_insensitive_on_checksums(tmp_path):
    part = tmp_path / "f.part"
    part.write_bytes(b"hello")
    digest = "5d41402abc4b2a76b9719d911017c592"
    gf.GofileClient._verify_payload(part, 5, 5, digest.upper(), digest)


# --------------------------------------------------------------------------
# GofileClient.download
# --------------------------------------------------------------------------


def _download_client(body, contents):
    client = gf.GofileClient(token="TOKEN")
    client.session = FakeSession([FakeResponse(body=body, headers={"Content-Length": str(len(body))})])
    client.get_content = lambda **kwargs: contents
    return client


def test_download_writes_the_file_and_removes_the_partial(tmp_path):
    body = b"payload-bytes"
    contents = {
        "id": "c",
        "type": "file",
        "name": "out.bin",
        "size": len(body),
        "md5": md5(body).hexdigest(),
        "link": "http://x/out.bin",
    }
    client = _download_client(body, contents)
    target = client.download("c", tmp_path)
    assert target.read_bytes() == body
    assert not list(tmp_path.glob("*.part"))


def test_download_refuses_to_overwrite_without_the_flag(tmp_path):
    (tmp_path / "out.bin").write_bytes(b"old")
    contents = {"id": "c", "type": "file", "name": "out.bin", "link": "http://x/out.bin"}
    client = _download_client(b"new", contents)
    with pytest.raises(FileExistsError):
        client.download("c", tmp_path)
    assert (tmp_path / "out.bin").read_bytes() == b"old"


def test_download_rejects_multiple_files_without_file_id(tmp_path):
    contents = {
        "id": "c",
        "type": "folder",
        "children": {
            "1": {"id": "1", "type": "file", "name": "a", "link": "http://x/a"},
            "2": {"id": "2", "type": "file", "name": "b", "link": "http://x/b"},
        },
    }
    client = _download_client(b"x", contents)
    with pytest.raises(gf.GofileError, match="Multiple files"):
        client.download("c", tmp_path)


def test_download_leaves_nothing_behind_on_checksum_failure(tmp_path):
    body = b"short-and-wrong"
    contents = {
        "id": "c",
        "type": "file",
        "name": "out.bin",
        "size": 999,
        "md5": "deadbeef",
        "link": "http://x/out.bin",
    }
    client = _download_client(body, contents)
    with pytest.raises(gf.GofileError):
        client.download("c", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_download_creates_a_missing_destination_directory(tmp_path):
    """A --dest that does not exist is a directory to create, not a file to
    write. argparse strips trailing slashes, so the old is_dir() check could
    never tell them apart and wrote a file literally named "out"."""

    body = b"payload-bytes"
    contents = {
        "id": "c",
        "type": "file",
        "name": "out.bin",
        "size": len(body),
        "md5": md5(body).hexdigest(),
        "link": "http://x/out.bin",
    }
    client = _download_client(body, contents)
    target = client.download("c", tmp_path / "out")
    assert target == tmp_path / "out" / "out.bin"
    assert target.read_bytes() == body


def test_download_filename_overrides_the_server_name(tmp_path):
    """--filename renames inside the directory; the server name is only the
    default, matching upstream's --filename flag."""

    body = b"payload-bytes"
    contents = {
        "id": "c",
        "type": "file",
        "name": "out.bin",
        "size": len(body),
        "md5": md5(body).hexdigest(),
        "link": "http://x/out.bin",
    }
    client = _download_client(body, contents)
    target = client.download("c", tmp_path, filename="custom.bin")
    assert target == tmp_path / "custom.bin"
    assert target.read_bytes() == body
    assert not (tmp_path / "out.bin").exists()


def test_download_filename_inside_a_missing_directory(tmp_path):
    """--dest and --filename compose: the directory is created first."""

    body = b"payload-bytes"
    contents = {
        "id": "c",
        "type": "file",
        "name": "out.bin",
        "size": len(body),
        "md5": md5(body).hexdigest(),
        "link": "http://x/out.bin",
    }
    client = _download_client(body, contents)
    target = client.download("c", tmp_path / "newdir", filename="custom.bin")
    assert target == tmp_path / "newdir" / "custom.bin"
    assert target.read_bytes() == body


def test_download_still_writes_to_an_existing_file_path(tmp_path):
    """Only a path that already exists as a file keeps the old write-here
    behaviour; every other shape now resolves to a directory. Pre-creating the
    file is what selects this branch."""

    dest = tmp_path / "exact.bin"
    dest.write_bytes(b"old")
    contents = {"id": "c", "type": "file", "name": "out.bin", "link": "http://x/out.bin"}
    client = _download_client(b"new", contents)
    target = client.download("c", dest, overwrite=True)
    assert target == dest
    assert dest.read_bytes() == b"new"


# --------------------------------------------------------------------------
# Legacy Downloader response helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_code,part_size,expected",
    [
        (206, 0, True),
        (206, 10, True),
        (200, 0, True),
        (200, 10, False),
        (403, 0, False),
        (404, 0, False),
        (405, 0, False),
        (500, 0, False),
    ],
)
def test_is_valid_response_matrix(status_code, part_size, expected):
    assert gf.Downloader._is_valid_response(status_code, part_size) is expected


def test_extract_file_size_prefers_content_length():
    assert gf.Downloader._extract_file_size({"Content-Length": "42"}, 0) == "42"


def test_extract_file_size_uses_content_range_on_resume():
    headers = {"Content-Range": "bytes 10-99/100"}
    assert gf.Downloader._extract_file_size(headers, 10) == "100"


def test_extract_file_size_returns_none_when_absent():
    assert gf.Downloader._extract_file_size({}, 0) is None


def test_should_skip_download_only_skips_non_empty_files(tmp_path):
    missing = tmp_path / "nope.bin"
    assert gf.Downloader._should_skip_download(missing) is False
    empty = tmp_path / "e.bin"
    empty.write_bytes(b"")
    assert gf.Downloader._should_skip_download(empty) is False
    full = tmp_path / "f.bin"
    full.write_bytes(b"data")
    assert gf.Downloader._should_skip_download(full) is True


def test_resolve_naming_collision_keeps_the_extension(tmp_path):
    existing = tmp_path / "a.bin"
    existing.write_bytes(b"x")
    name = gf.Downloader._resolve_naming_collision({}, str(tmp_path), "a.bin", False)
    assert name.endswith(".bin") and name != "a.bin"


def test_downloader_website_headers_derive_the_token_from_the_session():
    downloader = gf.Downloader.__new__(gf.Downloader)
    downloader._session = FakeSession()
    downloader._session.headers["User-Agent"] = "UA"
    downloader._session.headers["Authorization"] = "Bearer ACCT"
    headers = downloader._website_headers()
    assert headers["X-Website-Token"] == gf.generate_website_token("UA", "ACCT")
    assert headers["X-BL"] == gf.WEB_LOCALE


def test_downloader_website_headers_tolerate_a_missing_auth_header():
    downloader = gf.Downloader.__new__(gf.Downloader)
    downloader._session = FakeSession()
    downloader._session.headers["User-Agent"] = "UA"
    headers = downloader._website_headers()
    assert headers["X-Website-Token"] == gf.generate_website_token("UA", "")


# --------------------------------------------------------------------------
# Legacy Downloader download engine: shared helpers
# --------------------------------------------------------------------------

DOWNLOAD_LINK = "https://store.gofile.io/dl/abc123"


class _RaisingSession:
    """Session stub that raises queued exceptions before handing back a response."""

    def __init__(self, exceptions=(), response=None):
        self.exceptions = list(exceptions)
        self.response = response
        self.calls = []
        self.headers = dict(gf.GofileClient._DEFAULT_HEADERS)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if self.exceptions:
            raise self.exceptions.pop(0)
        return self.response


class _TimeoutAfterPrefix(FakeResponse):
    """Streams a prefix of the body, then fails the way a dropped socket does."""

    def __init__(self, prefix, **kwargs):
        super().__init__(**kwargs)
        self._prefix = prefix

    def iter_content(self, chunk_size=1):
        yield self._prefix
        raise gf.Timeout("read timed out")


def _downloader(root, session=None, number_retries=1, chunk_size=8, timeout=15.0, stop_event=None):
    """Build a Downloader with every constructor argument stated explicitly."""

    return gf.Downloader(
        str(root),      # root_dir
        False,          # interactive
        2,              # max_workers
        number_retries,
        timeout,
        chunk_size,
        threading.Event() if stop_event is None else stop_event,
        FakeSession() if session is None else session,
        "https://gofile.io/d/abc123",
        None,           # password
    )


def _file_info(root, filename="payload.bin", link=DOWNLOAD_LINK):
    return {"path": str(root), "filename": filename, "link": link}


_PERCENT_RE = re.compile(r"of \d+ (\d+(?:\.\d+)?)%")


def _capture_stdout(monkeypatch):
    """Return a buffer standing in for the module's stdout.

    The module does `from sys import ... stdout ...`, so its name is bound to the
    real stream at import time and neither capsys nor capfd can see writes to it.
    """

    buffer = io.StringIO()
    monkeypatch.setattr(gf, "stdout", buffer)
    return buffer


def _percentages(output):
    """Return every percentage rendered in a captured stdout chunk, in order."""

    found = [float(value) for value in _PERCENT_RE.findall(output)]
    assert found, f"no percentage was rendered in {output!r}"
    return found


# --------------------------------------------------------------------------
# Legacy Downloader._write_chunks
# --------------------------------------------------------------------------


def test_write_chunks_reports_true_running_totals_for_unequal_chunks(tmp_path, monkeypatch):
    """Progress used to be part_size + i * len(chunk).

    Multiplying the chunk index by the *last* chunk length reports 10, 30, 55 for
    uneven chunks instead of the real running sums 10, 13, 38, so the bar shot
    past 100% on any transfer whose last chunk was large.
    """

    downloader = _downloader(tmp_path)
    reported = []
    monkeypatch.setattr(downloader, "_update_progress", lambda *args: reported.append(args))

    chunks = [b"a" * 10, b"b" * 3, b"c" * 25]
    tmp_file = tmp_path / "payload.bin.part"
    downloader._write_chunks(iter(chunks), str(tmp_file), 0, 38.0, "payload.bin")

    assert [args[1] for args in reported] == [10, 13, 38]
    assert tmp_file.read_bytes() == b"".join(chunks)


def test_write_chunks_counts_the_already_downloaded_prefix_in_its_progress(tmp_path, monkeypatch):
    """A resumed transfer must count the bytes already on disk.

    Reporting only the bytes received in this attempt makes a resumed download
    appear to start from zero, so a nearly-complete file looks brand new.
    """

    downloader = _downloader(tmp_path)
    reported = []
    monkeypatch.setattr(downloader, "_update_progress", lambda *args: reported.append(args))

    downloader._write_chunks(iter([b"a" * 10, b"b" * 3, b"c" * 25]), str(tmp_path / "f.part"), 5, 43.0, "f.bin")

    assert [args[1] for args in reported] == [15, 18, 43]


def test_write_chunks_appends_to_an_existing_partial_instead_of_truncating_it(tmp_path):
    """Opening the partial in truncate mode would throw away the resumed prefix."""

    tmp_file = tmp_path / "payload.bin.part"
    tmp_file.write_bytes(b"HEAD")

    _downloader(tmp_path)._write_chunks(iter([b"TAIL"]), str(tmp_file), 4, 8.0, "payload.bin")

    assert tmp_file.read_bytes() == b"HEADTAIL"


def test_write_chunks_stops_writing_once_the_stop_event_is_set(tmp_path, monkeypatch):
    """Ctrl-C must halt the write loop instead of streaming the rest to disk."""

    stop_event = threading.Event()
    downloader = _downloader(tmp_path, stop_event=stop_event)
    reported = []

    def _record_then_stop(*args):
        reported.append(args)
        stop_event.set()

    monkeypatch.setattr(downloader, "_update_progress", _record_then_stop)
    tmp_file = tmp_path / "payload.bin.part"

    downloader._write_chunks(iter([b"a" * 4, b"b" * 4, b"c" * 4]), str(tmp_file), 0, 12.0, "payload.bin")

    assert tmp_file.read_bytes() == b"aaaa"
    assert len(reported) == 1


# --------------------------------------------------------------------------
# Legacy Downloader._update_progress
# --------------------------------------------------------------------------


def test_update_progress_renders_the_percentage_of_the_transferred_argument(tmp_path, monkeypatch):
    """The percentage must come from the transferred count it is handed.

    It used to be recomputed from chunk indexes, so a caller could not report a
    resumed file's true position.
    """

    out = _capture_stdout(monkeypatch)
    monkeypatch.setattr(gf, "perf_counter", lambda: 1000.0)

    _downloader(tmp_path)._update_progress("payload.bin", 50, 100, 999.0)

    rendered = out.getvalue()
    assert "Downloading payload.bin" in rendered
    assert "50 of 100" in rendered
    assert _percentages(rendered) == [50.0]


def test_update_progress_percentage_is_monotonic_and_capped_at_one_hundred(tmp_path, monkeypatch):
    """A transfer reported past its own total (bad Content-Length) must not
    render 150.0% - the bar is clamped so it never lies about overshooting."""

    out = _capture_stdout(monkeypatch)
    monkeypatch.setattr(gf, "perf_counter", lambda: 1000.0)
    downloader = _downloader(tmp_path)
    rendered = []

    for transferred in (0, 25, 50, 100, 150):
        downloader._update_progress("payload.bin", transferred, 100, 999.0)
        rendered.extend(_percentages(out.getvalue()))
        out.seek(0)
        out.truncate()

    assert rendered == sorted(rendered)
    assert max(rendered) == 100.0
    assert rendered[-1] == 100.0


@pytest.mark.parametrize("total_size", [0, 0.0])
def test_update_progress_renders_zero_percent_for_an_unknown_total(tmp_path, monkeypatch, total_size):
    """A size-less response still gets a progress line instead of blowing up."""

    out = _capture_stdout(monkeypatch)
    monkeypatch.setattr(gf, "perf_counter", lambda: 1000.0)

    _downloader(tmp_path)._update_progress("payload.bin", 10, total_size, 999.0)

    assert _percentages(out.getvalue()) == [0.0]


def test_update_progress_emits_no_progress_line_for_an_absent_total(tmp_path, monkeypatch):
    """An unknown total is unmeasurable, so nothing is printed.

    `int(None)` used to raise TypeError. The call is deliberately NOT wrapped in
    suppress(): swallowing that exception here would make the test pass whether
    or not the guard exists, so the TypeError has to escape and fail the run.
    """

    out = _capture_stdout(monkeypatch)
    monkeypatch.setattr(gf, "perf_counter", lambda: 1000.0)

    _downloader(tmp_path)._update_progress("payload.bin", 10, None, 999.0)

    assert "of " not in out.getvalue()


def test_update_progress_survives_a_zero_elapsed_time(tmp_path, monkeypatch):
    """Dividing the transfer rate by the elapsed time used to raise
    ZeroDivisionError whenever perf_counter had not ticked yet."""

    out = _capture_stdout(monkeypatch)
    monkeypatch.setattr(gf, "perf_counter", lambda: 1000.0)

    _downloader(tmp_path)._update_progress("payload.bin", 50, 100, 1000.0)

    assert _percentages(out.getvalue()) == [50.0]


@pytest.mark.parametrize(
    "transferred,elapsed,expected_rate",
    [
        (1, 1.0, "1.0B/s"),
        (1, 1 / 2048, "2.0KB/s"),
        (1, 1 / (2 * 1024 ** 2), "2.0MB/s"),
        (2, 1 / (2 * 1024 ** 3), "1.9GB/s"),
    ],
)
def test_update_progress_scales_the_rate_to_a_readable_unit(
    tmp_path, monkeypatch, transferred, elapsed, expected_rate
):
    """A multi-gigabyte transfer must be announced in GB/s at its true magnitude;
    a rate left in raw B/s (or scaled twice) is unreadable on the one line the
    user watches."""

    out = _capture_stdout(monkeypatch)
    # 1.0 keeps the dyadic elapsed values exactly representable in the subtraction,
    # and the last case rides the 1e-9 elapsed floor that caps the reachable rate.
    monkeypatch.setattr(gf, "perf_counter", lambda: 1.0)

    _downloader(tmp_path)._update_progress("payload.bin", transferred, 100, 1.0 - elapsed)

    assert out.getvalue().rstrip().endswith(expected_rate)


# --------------------------------------------------------------------------
# Legacy Downloader._perform_download
# --------------------------------------------------------------------------


def test_perform_download_never_dumps_the_session_headers_on_a_rejected_status(tmp_path, monkeypatch):
    """Regression: the failure message used to interpolate str(session.headers),
    publishing the account's `Authorization: Bearer ...` token to stdout."""

    out = _capture_stdout(monkeypatch)
    session = FakeSession([FakeResponse(status_code=403, headers={})])
    session.headers["Authorization"] = "Bearer SUPERSECRETTOKEN"
    downloader = _downloader(tmp_path, session)
    tmp_file = tmp_path / "payload.bin.part"

    result = downloader._perform_download(_file_info(tmp_path), DOWNLOAD_LINK, str(tmp_file), {}, 0)

    rendered = out.getvalue()
    assert result is None
    assert "403" in rendered
    assert "Bearer" not in rendered
    assert "SUPERSECRETTOKEN" not in rendered
    assert "Authorization" not in rendered


@pytest.mark.parametrize(
    "status_code,part_size,response_headers,accepted",
    [
        (200, 0, {"Content-Length": "4"}, True),
        (206, 0, {"Content-Length": "4"}, True),
        (200, 10, {"Content-Range": "bytes 10-13/14"}, False),
        (206, 10, {"Content-Range": "bytes 10-13/14"}, True),
        (403, 0, {"Content-Length": "4"}, False),
        (404, 0, {"Content-Length": "4"}, False),
        (405, 0, {"Content-Length": "4"}, False),
        (500, 0, {"Content-Length": "4"}, False),
    ],
)
def test_perform_download_accepts_only_ok_whole_and_partial_content_responses(
    tmp_path, status_code, part_size, response_headers, accepted
):
    """A 200 for a resumed request means the server ignored the Range header and
    is about to append the whole file to a partial download, so it must be
    rejected rather than written."""

    body = b"data"
    session = FakeSession([FakeResponse(body=body, headers=response_headers, status_code=status_code)])
    downloader = _downloader(tmp_path, session)
    tmp_file = tmp_path / "payload.bin.part"

    result = downloader._perform_download(_file_info(tmp_path), DOWNLOAD_LINK, str(tmp_file), {}, part_size)

    if accepted:
        assert result == ("4" if part_size == 0 else "14")
        assert tmp_file.read_bytes() == body
    else:
        assert result is None
        assert not tmp_file.exists()


def test_perform_download_sends_the_caller_supplied_range_header(tmp_path):
    """The resume Range built by _download_content has to reach the wire, or a
    restarted download silently re-fetches from byte zero."""

    session = FakeSession(
        [FakeResponse(body=b"tail", headers={"Content-Range": "bytes 4-7/8"}, status_code=206)]
    )
    downloader = _downloader(tmp_path, session)
    tmp_file = tmp_path / "payload.bin.part"

    downloader._perform_download(_file_info(tmp_path), DOWNLOAD_LINK, str(tmp_file), {"Range": "bytes=4-"}, 4)

    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == DOWNLOAD_LINK
    assert kwargs["headers"] == {"Range": "bytes=4-"}


@pytest.mark.parametrize(
    "part_size,response_headers,expected",
    [
        (0, {"Content-Length": "42"}, "42"),
        (10, {"Content-Range": "bytes 10-99/100"}, "100"),
        (0, {}, None),
    ],
)
def test_perform_download_derives_the_total_from_the_response_headers(
    tmp_path, part_size, response_headers, expected
):
    """An unsized response is not writable, so nothing may touch the partial file."""

    session = FakeSession(
        [FakeResponse(body=b"data", headers=response_headers, status_code=206 if part_size else 200)]
    )
    downloader = _downloader(tmp_path, session)
    tmp_file = tmp_path / "payload.bin.part"

    result = downloader._perform_download(_file_info(tmp_path), DOWNLOAD_LINK, str(tmp_file), {}, part_size)

    assert result == expected
    assert tmp_file.exists() is bool(expected)


def test_perform_download_reports_an_unknown_content_range_total_as_a_missing_size(tmp_path, monkeypatch):
    """`bytes 0-3/*` is a legal Content-Range whose total is unknown.

    "*" used to reach float() and kill the transfer with ValueError; it must
    instead take the ordinary "size unavailable" path, so the partial is left
    untouched and the user is told why nothing was written.
    """

    out = _capture_stdout(monkeypatch)
    session = FakeSession([FakeResponse(body=b"data", headers={"Content-Range": "bytes 0-3/*"}, status_code=206)])
    downloader = _downloader(tmp_path, session)
    tmp_file = tmp_path / "payload.bin.part"

    result = downloader._perform_download(
        _file_info(tmp_path), DOWNLOAD_LINK, str(tmp_file), {"Range": "bytes=0-"}, 1
    )

    assert result is None
    assert not tmp_file.exists()
    assert "Couldn't find the file size" in out.getvalue()
    assert "ValueError" not in out.getvalue()


def test_perform_download_reports_the_missing_size_to_the_user(tmp_path, monkeypatch):
    out = _capture_stdout(monkeypatch)
    session = FakeSession([FakeResponse(body=b"data", headers={}, status_code=200)])
    downloader = _downloader(tmp_path, session)

    result = downloader._perform_download(
        _file_info(tmp_path), DOWNLOAD_LINK, str(tmp_path / "payload.bin.part"), {}, 0
    )

    assert result is None
    assert "file size" in out.getvalue()


def test_perform_download_makes_no_request_once_the_stop_event_is_set(tmp_path):
    """A queued stop must not open a new connection."""

    stop_event = threading.Event()
    stop_event.set()
    session = FakeSession()
    downloader = _downloader(tmp_path, session, stop_event=stop_event)

    result = downloader._perform_download(
        _file_info(tmp_path), DOWNLOAD_LINK, str(tmp_path / "payload.bin.part"), {}, 0
    )

    assert result is None
    assert session.calls == []


def test_perform_download_fails_cleanly_when_every_retry_times_out(tmp_path, monkeypatch):
    """Losing the connection entirely must end in None, not an exception that
    tears down the whole ThreadPoolExecutor."""

    out = _capture_stdout(monkeypatch)
    session = _RaisingSession(exceptions=[gf.Timeout("boom")] * 3)
    downloader = _downloader(tmp_path, session, number_retries=3)
    tmp_file = tmp_path / "payload.bin.part"

    result = downloader._perform_download(_file_info(tmp_path), DOWNLOAD_LINK, str(tmp_file), {}, 0)

    assert result is None
    assert len(session.calls) == 3
    assert "failed to get a response" in out.getvalue()
    assert not tmp_file.exists()


# --------------------------------------------------------------------------
# Legacy Downloader._finalize_download
# --------------------------------------------------------------------------


def test_finalize_download_renames_the_partial_when_the_size_matches(tmp_path, monkeypatch):
    out = _capture_stdout(monkeypatch)
    part = tmp_path / "payload.bin.part"
    part.write_bytes(b"0123456789")

    gf.Downloader._finalize_download(_file_info(tmp_path, "payload.bin"), str(part), "10")

    final = tmp_path / "payload.bin"
    assert final.read_bytes() == b"0123456789"
    assert not part.exists()
    assert "Done!" in out.getvalue()


def test_finalize_download_keeps_the_partial_and_claims_nothing_when_the_size_differs(tmp_path, monkeypatch):
    """A truncated transfer must not be promoted to the final name: the partial
    stays on disk for the next resume and no `Done!` is printed."""

    out = _capture_stdout(monkeypatch)
    part = tmp_path / "payload.bin.part"
    part.write_bytes(b"0123456789")

    gf.Downloader._finalize_download(_file_info(tmp_path, "payload.bin"), str(part), "99")

    assert not (tmp_path / "payload.bin").exists()
    assert part.read_bytes() == b"0123456789"
    assert "Done!" not in out.getvalue()


@pytest.mark.parametrize("has_size, error", [(None, TypeError), ("", ValueError)])
def test_finalize_download_rejects_an_unusable_expected_size(tmp_path, has_size, error):
    """A size that cannot be parsed is a caller precondition violation, not a
    degraded-but-recoverable download, so int() must reject it.

    Asserting the raise is what makes this test meaningful. An earlier version
    wrapped the call in suppress(TypeError, ValueError), which could not tell
    "rejects the bad size" apart from "crashes for some unrelated reason" and so
    passed no matter what the helper did. The sole call site already guards
    with `if has_size:`, so these inputs are unreachable in production; if that
    guard is ever removed this test is the one that notices."""

    part = tmp_path / "payload.bin.part"
    part.write_bytes(b"0123456789")

    with pytest.raises(error):
        gf.Downloader._finalize_download(
            _file_info(tmp_path, "payload.bin"), str(part), has_size
        )

    assert not (tmp_path / "payload.bin").exists()
    assert part.read_bytes() == b"0123456789"


def test_finalize_download_keeps_the_partial_when_the_expected_size_does_not_match(
    tmp_path,
):
    """Zero parses fine and is simply the wrong answer here: a 10-byte partial
    cannot satisfy an expected size of 0, so it must be left for the next run."""

    part = tmp_path / "payload.bin.part"
    part.write_bytes(b"0123456789")

    gf.Downloader._finalize_download(_file_info(tmp_path, "payload.bin"), str(part), 0)

    assert not (tmp_path / "payload.bin").exists()
    assert part.read_bytes() == b"0123456789"


def test_finalize_download_moves_an_empty_partial_when_zero_is_the_expected_size(tmp_path):
    """The one falsy size that is a real answer: a legitimately empty file."""

    part = tmp_path / "payload.bin.part"
    part.write_bytes(b"")

    gf.Downloader._finalize_download(_file_info(tmp_path, "payload.bin"), str(part), "0")

    assert (tmp_path / "payload.bin").exists()
    assert not part.exists()


# --------------------------------------------------------------------------
# Legacy Downloader._get_response
# --------------------------------------------------------------------------


def test_get_response_returns_the_response_after_transient_timeouts(tmp_path):
    """Two dropped connections are normal on a flaky link; the third attempt's
    response must be handed back instead of None."""

    ok = FakeResponse(body=b"payload", status_code=200)
    session = _RaisingSession(exceptions=[gf.Timeout("boom"), gf.Timeout("boom")], response=ok)
    downloader = _downloader(tmp_path, session, number_retries=3, timeout=42.0)

    assert downloader._get_response(url=DOWNLOAD_LINK, headers={}, stream=True) is ok
    assert len(session.calls) == 3
    assert session.calls[-1][1] == DOWNLOAD_LINK
    assert session.calls[0][2]["timeout"] == 42.0


def test_get_response_gives_up_and_returns_none_when_the_retries_run_out(tmp_path):
    """After number_retries attempts the caller gets None (which _perform_download
    turns into a clean 'failed to get a response' message)."""

    session = _RaisingSession(exceptions=[gf.Timeout("boom")] * 3)
    downloader = _downloader(tmp_path, session, number_retries=3)

    assert downloader._get_response(url=DOWNLOAD_LINK) is None
    assert len(session.calls) == 3


def test_get_response_propagates_a_non_timeout_failure_without_retrying(tmp_path):
    """Only timeouts are retried: a DNS/TLS/404-class error must surface at once
    instead of burning the retry budget three times over."""

    session = _RaisingSession(exceptions=[gf.RequestException("dns failure")], response=FakeResponse())
    downloader = _downloader(tmp_path, session, number_retries=3)

    with pytest.raises(gf.RequestException):
        downloader._get_response(url=DOWNLOAD_LINK)

    assert len(session.calls) == 1


# --------------------------------------------------------------------------
# Legacy Downloader._download_content
# --------------------------------------------------------------------------


def test_download_content_resumes_with_a_range_header_starting_at_the_partial_size(tmp_path):
    """A restart must ask the server only for the bytes still missing."""

    part = tmp_path / "payload.bin.part"
    part.write_bytes(b"HEAD")
    session = FakeSession(
        [FakeResponse(body=b"TAIL", headers={"Content-Range": "bytes 4-7/8"}, status_code=206)]
    )
    downloader = _downloader(tmp_path, session, number_retries=1, chunk_size=4)

    downloader._download_content(_file_info(tmp_path, "payload.bin"))

    _, _, kwargs = session.calls[0]
    assert kwargs["headers"] == {"Range": "bytes=4-"}
    assert (tmp_path / "payload.bin").read_bytes() == b"HEADTAIL"
    assert not part.exists()


def test_download_content_sends_no_range_header_for_a_fresh_download(tmp_path):
    session = FakeSession([FakeResponse(body=b"DATA", headers={"Content-Length": "4"}, status_code=200)])
    downloader = _downloader(tmp_path, session, number_retries=1, chunk_size=4)

    downloader._download_content(_file_info(tmp_path, "payload.bin"))

    _, _, kwargs = session.calls[0]
    assert kwargs["headers"] == {}
    assert (tmp_path / "payload.bin").read_bytes() == b"DATA"


def test_download_content_skips_a_file_that_is_already_complete(tmp_path, monkeypatch):
    """A finished file must be left alone and cost no request."""

    out = _capture_stdout(monkeypatch)
    final = tmp_path / "payload.bin"
    final.write_bytes(b"already")
    session = FakeSession()
    downloader = _downloader(tmp_path, session)

    downloader._download_content(_file_info(tmp_path, "payload.bin"))

    assert session.calls == []
    assert "skipping" in out.getvalue()
    assert final.read_bytes() == b"already"


def test_download_content_resumes_after_a_timeout_partway_through_the_stream(tmp_path):
    """A read timeout escapes _get_response and lands in the retry loop, which
    re-derives the Range from whatever reached the disk."""

    session = FakeSession(
        [
            _TimeoutAfterPrefix(b"HE", body=b"HEADTAIL", headers={"Content-Length": "8"}, status_code=200),
            FakeResponse(body=b"ADTAIL", headers={"Content-Range": "bytes 2-7/8"}, status_code=206),
        ]
    )
    downloader = _downloader(tmp_path, session, number_retries=3, chunk_size=8)

    downloader._download_content(_file_info(tmp_path, "payload.bin"))

    assert len(session.calls) == 2
    assert session.calls[1][2]["headers"] == {"Range": "bytes=2-"}
    assert (tmp_path / "payload.bin").read_bytes() == b"HEADTAIL"
    assert not (tmp_path / "payload.bin.part").exists()


def test_download_content_keeps_the_partial_when_the_response_is_rejected(tmp_path):
    """A 404 must not be retried into a loop nor treated as a finished file:
    the bytes already downloaded stay put for the next run."""

    part = tmp_path / "payload.bin.part"
    part.write_bytes(b"HEAD")
    session = FakeSession([FakeResponse(body=b"", headers={}, status_code=404)])
    downloader = _downloader(tmp_path, session, number_retries=3)

    downloader._download_content(_file_info(tmp_path, "payload.bin"))

    assert not (tmp_path / "payload.bin").exists()
    assert part.read_bytes() == b"HEAD"
    assert len(session.calls) == 1


# --------------------------------------------------------------------------
# Manager token handling
# --------------------------------------------------------------------------


def test_manager_session_sends_browser_like_origin_and_referer():
    manager = gf.Manager("https://gofile.io/d/x")
    assert manager._session.headers["Origin"] == "https://gofile.io"
    assert manager._session.headers["Referer"] == "https://gofile.io/"


def test_manager_sets_a_properly_named_account_token_cookie():
    manager = gf.Manager("https://gofile.io/d/x")
    manager._set_account_access_token("TOK")
    jar = manager._session.cookies.get_dict()
    assert jar.get("accountToken") == "TOK"
    assert "Cookie" not in jar


def test_manager_account_creation_failure_raises_instead_of_keyerror():
    manager = gf.Manager("https://gofile.io/d/x")
    manager._session.post = lambda *a, **k: (_ for _ in ()).throw(
        gf.Timeout("boom")
    )
    with pytest.raises(SystemExit):
        manager._set_account_access_token(None)


# --------------------------------------------------------------------------
# main() dispatch
# --------------------------------------------------------------------------


def test_main_rejects_an_unknown_subcommand():
    with pytest.raises(SystemExit):
        gf.main(["definitely-not-a-command"])


def test_legacy_entry_rejects_a_non_url(monkeypatch):
    monkeypatch.setenv("GF_TOKEN", "TESTTOKEN")
    with pytest.raises(SystemExit):
        gf._legacy_entry(["   "])


# --------------------------------------------------------------------------
# main() dispatch: legacy vs subcommand boundary
# --------------------------------------------------------------------------


def _capture_stderr(monkeypatch):
    """Stand in for the module's stderr; die() and main() errors go there."""

    buffer = io.StringIO()
    monkeypatch.setattr(gf, "stderr", buffer)
    return buffer


def _capture_help(monkeypatch):
    """Capture argparse's help output.

    argparse resolves sys.stdout at call time, so patching the sys module
    attribute is what captures it (unlike the module's own from-sys stdout).
    """

    buffer = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buffer)
    return buffer


def _legacy_spy(monkeypatch):
    """Replace _legacy_entry with a sentinel so dispatch can be observed."""

    seen = []
    monkeypatch.setattr(gf, "_legacy_entry", lambda args: seen.append(args) or 42)
    return seen


def test_main_with_no_arguments_prints_help_and_reports_failure(monkeypatch):
    """`python gofile-downloader.py` with nothing to do must tell the user what
    the commands are and exit non-zero; a bare zero would look like success."""

    out = _capture_help(monkeypatch)

    assert gf.main([]) == 1
    printed = out.getvalue()
    assert "usage:" in printed
    assert "mirror" in printed and "download" in printed


def test_main_routes_an_unknown_word_to_the_legacy_downloader(monkeypatch):
    """The dispatch boundary itself: anything that is not one of the four
    subcommands is a share link, so a typo'd link keeps working instead of
    dying in argparse."""

    seen = _legacy_spy(monkeypatch)

    assert gf.main(["not-a-subcommand"]) == 42
    assert seen == [["not-a-subcommand"]]


def test_main_forwards_the_legacy_url_and_its_password_verbatim(monkeypatch):
    """The legacy path takes a bare URL plus an optional second positional, and
    it must arrive unrewritten - a normalized link can resolve to the wrong id."""

    seen = _legacy_spy(monkeypatch)

    gf.main(["https://gofile.io/d/abc123", "hunter2"])

    assert seen == [["https://gofile.io/d/abc123", "hunter2"]]


def test_main_routes_a_leading_dash_to_argparse_instead_of_the_legacy_path(monkeypatch):
    """A global flag with no subcommand is a help request, not a link, so it must
    never reach the legacy downloader."""

    seen = _legacy_spy(monkeypatch)
    out = _capture_help(monkeypatch)

    assert gf.main(["--token", "GLOBAL"]) == 1
    assert seen == []
    assert "usage:" in out.getvalue()


def test_main_mirror_builds_a_manager_from_the_parsed_source_and_password(monkeypatch):
    """`mirror` is the only subcommand that must not construct a GofileClient:
    it owns the legacy Manager, and its two arguments have to be marshalled
    through untouched."""

    built = []

    class _Manager:
        def __init__(self, url_or_file, password=None):
            built.append((url_or_file, password))
            self.ran = False

        def run(self):
            self.ran = True

    monkeypatch.setattr(gf, "Manager", _Manager)

    assert gf.main(["mirror", "links.txt", "--password", "PW"]) == 0
    assert built == [("links.txt", "PW")]


def test_main_upload_hands_the_parsed_file_folder_and_description_to_the_client(tmp_path, monkeypatch):
    """Upload's three CLI values have to reach upload() under the keyword names
    it expects, otherwise the file lands in the account root with no note."""

    source = tmp_path / "payload.bin"
    source.write_bytes(b"data")

    client_holder = {}

    class _Client:
        def __init__(self, token=None):
            self.calls = []
            client_holder["client"] = self

        def upload(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "fileId": "F1",
                "code": "CODE1",
                "downloadPage": "https://gofile.io/d/CODE1",
                "directLink": "https://store.gofile.io/dl/f1",
            }

    monkeypatch.setattr(gf, "GofileClient", _Client)
    out = _capture_stdout(monkeypatch)

    assert gf.main(["upload", str(source), "--folder-id", "FOLDER", "--description", "a note"]) == 0
    assert client_holder["client"].calls == [
        {"file_path": source, "folder_id": "FOLDER", "description": "a note"}
    ]
    printed = out.getvalue()
    assert "F1" in printed and "CODE1" in printed and "https://store.gofile.io/dl/f1" in printed


def test_main_download_extracts_the_content_id_from_the_share_link(tmp_path, monkeypatch):
    """download gets the *code*, not the URL: passing the whole share link to the
    API is what makes it answer error-contentId."""

    dest = tmp_path / "out"

    class _Client:
        def __init__(self, token=None):
            self.calls = []
            client_holder["client"] = self

        def download(self, **kwargs):
            self.calls.append(kwargs)
            return dest

    client_holder = {}
    monkeypatch.setattr(gf, "GofileClient", _Client)

    assert gf.main(
        ["download", "https://gofile.io/d/abc123", "--dest", str(dest), "--file-id", "F9", "--overwrite"]
    ) == 0
    kwargs = client_holder["client"].calls[0]
    assert kwargs["content_id"] == "abc123"
    assert kwargs["destination"] == dest
    assert kwargs["file_id"] == "F9"
    assert kwargs["overwrite"] is True
    assert kwargs["filename"] is None


def test_main_download_passes_filename_through(tmp_path, monkeypatch):
    """--filename must reach client.download; dropping it in main() would
    silently download under the server name despite the flag being accepted."""

    client_holder = {}

    class _Client:
        def __init__(self, token=None):
            client_holder["client"] = self

        def download(self, **kwargs):
            client_holder["calls"] = kwargs
            return tmp_path / "custom.bin"

    monkeypatch.setattr(gf, "GofileClient", _Client)

    assert gf.main(["download", "https://gofile.io/d/abc123", "--filename", "custom.bin"]) == 0
    assert client_holder["calls"]["filename"] == "custom.bin"
    assert client_holder["calls"]["destination"] == Path.cwd()


def test_main_resolve_passes_recursion_through_and_prints_the_links(monkeypatch):
    """--no-recursive has to reach resolve_direct_links; silently recursing anyway
    hands the user a list they explicitly did not ask for."""

    class _Client:
        def __init__(self, token=None):
            self.calls = []

        def resolve_direct_links(self, **kwargs):
            self.calls.append(kwargs)
            return [{"fileId": "F1", "name": "a.bin", "size": 4, "directLink": "https://store/dl/a"}]

    client_holder = {}

    class _Recording(_Client):
        def __init__(self, token=None):
            super().__init__(token)
            client_holder["client"] = self

    monkeypatch.setattr(gf, "GofileClient", _Recording)
    out = _capture_stdout(monkeypatch)

    assert gf.main(["resolve", "abc123", "--no-recursive", "--password", "PW"]) == 0
    assert client_holder["client"].calls[0] == {
        "content_id": "abc123",
        "password": "PW",
        "recursive": False,
    }
    assert "https://store/dl/a" in out.getvalue()


def test_main_resolve_json_flag_emits_machine_readable_output(monkeypatch):
    """The --json contract is that the whole payload is parseable JSON, not a
    header line followed by human rows."""

    import json as _json

    entries = [{"fileId": "F1", "name": "a.bin", "size": 4, "directLink": "https://store/dl/a"}]

    class _Client:
        def __init__(self, token=None):
            pass

        def resolve_direct_links(self, **kwargs):
            return entries

    monkeypatch.setattr(gf, "GofileClient", _Client)
    out = _capture_stdout(monkeypatch)

    assert gf.main(["resolve", "abc123", "--json"]) == 0
    assert _json.loads(out.getvalue()) == entries


@pytest.mark.parametrize(
    "raised,expected_text",
    [
        (gf.GofileError("error-token"), "error-token"),
        (FileExistsError("Destination '/x/a.bin' already exists"), "already exists"),
        (ValueError("Empty gofile link or code provided"), "Empty gofile link"),
    ],
)
def test_main_turns_a_subcommand_failure_into_a_message_and_exit_code_one(
    monkeypatch, raised, expected_text
):
    """A failed download must reach the user as one clean error line, not as a
    traceback: these are ordinary API conditions, not programming errors."""

    def _raiser(self, **kwargs):
        raise raised

    monkeypatch.setattr(gf.GofileClient, "download", _raiser)
    out = _capture_stdout(monkeypatch)
    err = _capture_stderr(monkeypatch)

    assert gf.main(["download", "abc123"]) == 1
    assert expected_text in err.getvalue()
    assert "Traceback" not in out.getvalue()


def test_main_uses_the_token_flag_for_the_client_it_builds(monkeypatch):
    """--token is the only way to override the account without touching the
    environment, so a dropped token makes every authenticated call fail."""

    seen = []

    class _Client:
        def __init__(self, token=None):
            seen.append(token)

        def resolve_direct_links(self, **kwargs):
            return [{"name": "a", "directLink": "https://store/dl/a"}]

    monkeypatch.setattr(gf, "GofileClient", _Client)
    _capture_stdout(monkeypatch)

    assert gf.main(["--token", "FLAT", "resolve", "abc123"]) == 0
    assert seen == ["FLAT"]


def test_main_resolve_with_no_entries_still_exits_zero(monkeypatch):
    """resolve's "nothing found" case is reported by resolve_direct_links as an
    error, so an empty successful list is the only path here."""

    class _Client:
        def __init__(self, token=None):
            pass

        def resolve_direct_links(self, **kwargs):
            return []

    monkeypatch.setattr(gf, "GofileClient", _Client)
    out = _capture_stdout(monkeypatch)

    assert gf.main(["resolve", "abc123"]) == 0
    assert "Resolved 0 file(s)" in out.getvalue()


# --------------------------------------------------------------------------
# Manager._parse_url_or_file
# --------------------------------------------------------------------------

_MANAGER_ENV_VARS = (
    "GF_DOWNLOAD_DIR",
    "GF_MAX_CONCURRENT_DOWNLOADS",
    "GF_MAX_RETRIES",
    "GF_TIMEOUT",
    "GF_USERAGENT",
    "GF_INTERACTIVE",
    "GF_CHUNK_SIZE",
    "GF_TOKEN",
)


def _manager_env(monkeypatch, tmp_path, **values):
    """Clear every GF_* variable, then apply the requested overrides."""

    for name in _MANAGER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GF_DOWNLOAD_DIR", str(tmp_path / "root"))
    for name, value in values.items():
        monkeypatch.setenv(name, str(value))


def _downloader_spy(monkeypatch):
    """Replace Downloader with a recorder; returns the list of constructed stubs."""

    built = []

    class _Downloader:
        def __init__(self, root_dir, interactive, max_workers, number_retries, timeout, chunk_size,
                     stop_event, session, url, password=None):
            self.root_dir = root_dir
            self.interactive = interactive
            self.max_workers = max_workers
            self.number_retries = number_retries
            self.timeout = timeout
            self.chunk_size = chunk_size
            self.stop_event = stop_event
            self.session = session
            self.url = url
            self.password = password
            self.runs = 0
            built.append(self)

        def run(self):
            self.runs += 1

    monkeypatch.setattr(gf, "Downloader", _Downloader)
    return built


def test_parse_url_or_file_builds_one_downloader_for_a_bare_url(tmp_path, monkeypatch):
    """A share link is the common case: it must yield exactly one Downloader
    aimed at that URL and rooted in the configured directory."""

    _manager_env(monkeypatch, tmp_path)
    built = _downloader_spy(monkeypatch)
    manager = gf.Manager("https://gofile.io/d/abc123")

    manager._parse_url_or_file()

    assert len(built) == 1
    assert built[0].url == "https://gofile.io/d/abc123"
    assert built[0].root_dir == str(tmp_path / "root")
    assert built[0].runs == 1


def test_parse_url_or_file_normalizes_a_schemeless_link_before_downloading(tmp_path, monkeypatch):
    """`gofile.io/d/x` is accepted by _normalize_http_url but unusable as a
    request target; the https:// prefix has to be added before the downloader
    ever sees it, or the legacy run() id split sees the wrong shape."""

    _manager_env(monkeypatch, tmp_path)
    built = _downloader_spy(monkeypatch)

    gf.Manager("gofile.io/d/abc123")._parse_url_or_file()

    assert built[0].url == "https://gofile.io/d/abc123"


def test_parse_url_or_file_refuses_a_source_that_is_neither_file_nor_url(tmp_path, monkeypatch):
    """A typo'd path must stop the run with a clear message instead of exiting
    silently having downloaded nothing."""

    err = io.StringIO()
    monkeypatch.setattr(gf, "stderr", err)

    with pytest.raises(SystemExit):
        gf.Manager(str(tmp_path / "missing.txt"))._parse_url_or_file()

    assert "is either a valid url or local url file" in err.getvalue()


def test_parse_url_or_file_keeps_good_lines_when_one_line_is_junk(tmp_path, monkeypatch):
    """One malformed line must not cost the whole batch: the rest of the file
    still has to be scheduled."""

    link_file = tmp_path / "links.txt"
    link_file.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "https://gofile.io/d/one",
                "not-a-url",
                "https://gofile.io/d/two",
            ]
        ),
        encoding="utf-8",
    )

    _manager_env(monkeypatch, tmp_path)
    built = _downloader_spy(monkeypatch)
    out = _capture_stdout(monkeypatch)

    gf.Manager(str(link_file))._parse_url_or_file()

    assert sorted(d.url for d in built) == ["https://gofile.io/d/one", "https://gofile.io/d/two"]
    assert "not-a-url is not a valid url." in out.getvalue()


def test_parse_url_or_file_attaches_each_line_password_to_its_own_downloader(tmp_path, monkeypatch):
    """`url password` on one line must not bleed into the next line: the whole
    point of the batch format is per-link credentials."""

    link_file = tmp_path / "links.txt"
    link_file.write_text(
        "https://gofile.io/d/one pw1\nhttps://gofile.io/d/two\n", encoding="utf-8"
    )

    _manager_env(monkeypatch, tmp_path)
    built = _downloader_spy(monkeypatch)

    gf.Manager(str(link_file))._parse_url_or_file()

    by_url = {d.url: d.password for d in built}
    assert by_url == {"https://gofile.io/d/one": "pw1", "https://gofile.io/d/two": None}


def test_parse_url_or_file_lets_the_manager_password_override_the_line_password(tmp_path, monkeypatch):
    """--password is documented as overriding per-line passwords, so a manager
    password must win over a line that carries one."""

    link_file = tmp_path / "links.txt"
    link_file.write_text("https://gofile.io/d/one linepw\n", encoding="utf-8")

    _manager_env(monkeypatch, tmp_path)
    built = _downloader_spy(monkeypatch)

    gf.Manager(str(link_file), password="GLOBAL")._parse_url_or_file()

    assert built[0].password == "GLOBAL"


def test_parse_url_or_file_disables_interactive_selection_for_a_batch_file(tmp_path, monkeypatch):
    """Interactive selection needs a terminal prompt per file; batch mode from a
    text file must not try, or it hangs waiting on stdin."""

    link_file = tmp_path / "links.txt"
    link_file.write_text("https://gofile.io/d/one\n", encoding="utf-8")

    _manager_env(monkeypatch, tmp_path, GF_INTERACTIVE="1")
    built = _downloader_spy(monkeypatch)

    gf.Manager(str(link_file))._parse_url_or_file()

    assert built[0].interactive is False


def test_parse_url_or_file_forwards_every_tunable_into_the_downloader(tmp_path, monkeypatch):
    """The GF_* environment is the only way to tune the legacy engine; a value
    that fails to reach the Downloader silently reverts to a default nobody
    asked for."""

    link_file = tmp_path / "links.txt"
    link_file.write_text("https://gofile.io/d/one\n", encoding="utf-8")

    _manager_env(
        monkeypatch,
        tmp_path,
        GF_MAX_CONCURRENT_DOWNLOADS="9",
        GF_MAX_RETRIES="7",
        GF_TIMEOUT="42.5",
        GF_CHUNK_SIZE="65536",
        GF_INTERACTIVE="1",
    )
    built = _downloader_spy(monkeypatch)

    gf.Manager(str(link_file))._parse_url_or_file()

    downloader = built[0]
    assert downloader.max_workers == 9
    assert downloader.number_retries == 7
    assert downloader.timeout == 42.5
    assert downloader.chunk_size == 65536


def test_parse_url_or_file_caps_batch_parallelism_at_ten(tmp_path, monkeypatch):
    """An over-large GF_MAX_CONCURRENT_DOWNLOADS must be clamped for batch mode:
    one file per line can otherwise hammer the API with hundreds of concurrent
    requests and get the account throttled."""

    link_file = tmp_path / "links.txt"
    link_file.write_text("https://gofile.io/d/one\n", encoding="utf-8")

    _manager_env(monkeypatch, tmp_path, GF_MAX_CONCURRENT_DOWNLOADS="50")
    built = _downloader_spy(monkeypatch)
    limits = []

    real_executor = gf.ThreadPoolExecutor

    class _Executor:
        def __init__(self, max_workers):
            limits.append(max_workers)
            self._inner = real_executor(max_workers=max_workers)

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    monkeypatch.setattr(gf, "ThreadPoolExecutor", _Executor)

    gf.Manager(str(link_file))._parse_url_or_file()

    assert limits == [10]
    assert built[0].max_workers == 50


# --------------------------------------------------------------------------
# Downloader._build_content_tree_structure
# --------------------------------------------------------------------------


def _tree_session(payloads):
    """Queue one /contents payload per request the traversal will make."""

    return FakeSession([FakeResponse(payload=payload) for payload in payloads])


def _empty_folder_session():
    return _tree_session([_ok({"id": "abc", "type": "folder", "name": "abc", "children": {}})])


def _ok(data):
    return {"status": "ok", "data": data}


def test_build_content_tree_structure_sends_no_dead_website_token_query_param(tmp_path, monkeypatch):
    """Regression: this call used to append `wt=4fd6sg89d7s6`, which gofile.io
    answers with HTTP 401 - so every legacy mirror download died at the API walk
    before a single byte was fetched."""

    downloader = _downloader(tmp_path, _empty_folder_session())

    downloader._build_content_tree_structure(str(tmp_path / "abc"), "abc")

    _, url, _ = downloader._session.calls[0]
    assert "wt=" not in url


def test_build_content_tree_structure_authenticates_with_website_headers(tmp_path, monkeypatch):
    """The wt= removal only works if the X-Website-Token/X-BL pair replaces it:
    without those the same request comes back as error-token."""

    downloader = _downloader(tmp_path, _empty_folder_session())

    downloader._build_content_tree_structure(str(tmp_path / "abc"), "abc")

    _, _, kwargs = downloader._session.calls[0]
    headers = kwargs["headers"]
    assert "X-Website-Token" in headers
    assert headers["X-BL"] == gf.WEB_LOCALE


def test_build_content_tree_structure_creates_the_root_dir_and_registers_nothing_when_empty(tmp_path):
    """An empty folder still has to exist on disk, or run() then finds no
    directory and reports the download as broken rather than merely empty."""

    downloader = _downloader(tmp_path, _empty_folder_session())

    downloader._build_content_tree_structure(str(tmp_path / "abc"), "abc")

    assert (tmp_path / "abc").is_dir()
    assert downloader._files_info == {}


def test_build_content_tree_structure_registers_files_from_a_folder_listing(tmp_path):
    """Each child of a folder listing has to become a downloadable entry, keyed
    sequentially so _threaded_downloads can iterate them."""

    payload = _ok(
        {
            "id": "abc",
            "type": "folder",
            "name": "abc",
            "children": {
                "c1": {"id": "c1", "type": "file", "name": "one.bin", "link": "https://store/dl/one"},
                "c2": {"id": "c2", "type": "file", "name": "two.bin", "link": "https://store/dl/two"},
            },
        }
    )
    downloader = _downloader(tmp_path, _tree_session([payload]))

    downloader._build_content_tree_structure(str(tmp_path / "abc"), "abc")

    assert sorted(info["filename"] for info in downloader._files_info.values()) == ["one.bin", "two.bin"]
    assert {info["link"] for info in downloader._files_info.values()} == {
        "https://store/dl/one",
        "https://store/dl/two",
    }


def test_build_content_tree_structure_recurses_into_nested_folders(tmp_path):
    """A children dict keyed by id is the real gofile shape; a nested folder
    must be walked on the shared pathing_count so collisions stay resolvable."""

    root = _ok(
        {
            "id": "abc",
            "type": "folder",
            "name": "abc",
            "children": {
                "sub": {"id": "sub", "type": "folder", "name": "sub"},
                "top": {"id": "top", "type": "file", "name": "top.bin", "link": "https://store/dl/top"},
            },
        }
    )
    nested = _ok(
        {
            "id": "sub",
            "type": "folder",
            "name": "sub",
            "children": {
                "deep": {"id": "deep", "type": "file", "name": "deep.bin", "link": "https://store/dl/deep"}
            },
        }
    )
    downloader = _downloader(tmp_path, _tree_session([root, nested]))

    downloader._build_content_tree_structure(str(tmp_path / "abc"), "abc")

    assert (tmp_path / "abc" / "sub" / "deep.bin").parent.is_dir()
    by_name = {info["filename"]: info for info in downloader._files_info.values()}
    assert by_name["deep.bin"]["path"] == str(tmp_path / "abc" / "sub")
    assert by_name["top.bin"]["path"] == str(tmp_path / "abc")


def test_build_content_tree_structure_creates_the_directory_for_a_single_file_content(tmp_path):
    """Regression: the non-folder branch used to skip _create_dirs, so the later
    _register_file/rename against a directory that did not exist raised
    FileNotFoundError for every single-file link."""

    payload = _ok(
        {"id": "solo", "type": "file", "name": "solo.bin", "link": "https://store/dl/solo"}
    )
    downloader = _downloader(tmp_path, _tree_session([payload]))

    downloader._build_content_tree_structure(str(tmp_path / "solo"), "solo")

    assert (tmp_path / "solo").is_dir()
    assert [info["filename"] for info in downloader._files_info.values()] == ["solo.bin"]


def test_build_content_tree_structure_puts_the_hashed_password_on_the_request(tmp_path):
    """A protected folder is only reachable if the sha256 password travels in
    the query string; omitting it makes the API answer passwordNotFound."""

    payload = _ok(
        {"id": "abc", "type": "folder", "name": "abc", "password": "x", "passwordStatus": "passwordOk", "children": {}}
    )
    downloader = _downloader(tmp_path, _tree_session([payload]))

    downloader._build_content_tree_structure(str(tmp_path / "abc"), "abc", "e3b0c44298fc1c14")

    _, url, _ = downloader._session.calls[0]
    assert "password=e3b0c44298fc1c14" in url


def test_build_content_tree_structure_reports_a_locked_link_instead_of_walking_it(tmp_path, monkeypatch):
    """passwordStatus != passwordOk means the walk must stop and say so; treating
    the locked payload as a folder produces an empty download presented as a
    successful one."""

    payload = _ok(
        {"id": "abc", "type": "folder", "name": "abc", "password": "x", "passwordStatus": "passwordRequired", "children": {}}
    )
    downloader = _downloader(tmp_path, _tree_session([payload]))
    out = _capture_stdout(monkeypatch)

    downloader._build_content_tree_structure(str(tmp_path / "abc"), "abc", "deadbeef")

    assert "Password protected link" in out.getvalue()
    assert downloader._files_info == {}


def test_build_content_tree_structure_aborts_on_a_non_ok_status(tmp_path, monkeypatch):
    """An error-token or error-server payload must not be read as a listing:
    indexing data["type"] on an error dict crashes the whole mirror."""

    downloader = _downloader(
        tmp_path, FakeSession([FakeResponse(payload={"status": "error-token"})])
    )
    out = _capture_stdout(monkeypatch)

    downloader._build_content_tree_structure(str(tmp_path / "abc"), "abc")

    assert "Failed to fetch data response" in out.getvalue()
    assert downloader._files_info == {}
    assert not (tmp_path / "abc").exists()


# --------------------------------------------------------------------------
# GofileClient.upload
# --------------------------------------------------------------------------

_UPLOAD_RESULT = {
    "fileId": "F1",
    "code": "CODE1",
    "downloadPage": "https://gofile.io/d/CODE1",
    "directLink": "https://store.gofile.io/dl/f1",
}


def _upload_client(tmp_path, monkeypatch, server="fixed01"):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload-bytes")
    client = gf.GofileClient(token="TOKEN")
    client.session = FakeSession(
        [FakeResponse(payload={"status": "ok", "data": dict(_UPLOAD_RESULT)})]
    )
    client.get_server = lambda: server
    return client, payload


def _install_fake_toolbelt(monkeypatch):
    """Stand in for requests_toolbelt's monitor; returns the recorded callbacks."""

    progress = []

    class _Encoder:
        def __init__(self, fields):
            self.fields = fields
            self.len = 1024

    class _Monitor:
        def __init__(self, encoder, callback):
            self.encoder = encoder
            self.callback = callback
            self.content_type = "multipart/form-data; boundary=FAKE"
            progress.append(self)

    monkeypatch.setattr(gf, "MultipartEncoder", _Encoder)
    monkeypatch.setattr(gf, "MultipartEncoderMonitor", _Monitor)
    return progress


def test_upload_posts_to_the_resolved_server_upload_endpoint(tmp_path, monkeypatch):
    """The upload host comes from /getServer, not from api.gofile.io; a hardcoded
    host gets a 404 that surfaces as an opaque error to the user."""

    client, payload = _upload_client(tmp_path, monkeypatch, server="fixed01")

    result = client.upload(payload)

    method, url, _ = client.session.calls[0]
    assert (method, url) == ("POST", "https://fixed01.gofile.io/uploadFile")
    assert result == _UPLOAD_RESULT


def test_upload_carries_the_account_token_in_the_payload(tmp_path, monkeypatch):
    """Without the account token gofile.io rejects the upload with
    error-token, so the token has to ride along in the form fields."""

    client, payload = _upload_client(tmp_path, monkeypatch)
    monkeypatch.setattr(gf, "MultipartEncoder", None)
    monkeypatch.setattr(gf, "MultipartEncoderMonitor", None)

    client.upload(payload)

    _, _, kwargs = client.session.calls[0]
    assert kwargs["data"]["token"] == "TOKEN"


def test_upload_sends_folder_id_and_description_only_when_supplied(tmp_path, monkeypatch):
    """An empty description or folder must not be sent as "" / null: gofile.io
    stores the empty string and the file ends up titled with a blank note."""

    client, payload = _upload_client(tmp_path, monkeypatch)
    monkeypatch.setattr(gf, "MultipartEncoder", None)
    monkeypatch.setattr(gf, "MultipartEncoderMonitor", None)

    client.upload(payload)

    _, _, kwargs = client.session.calls[0]
    assert kwargs["data"] == {"token": "TOKEN"}


def test_upload_includes_folder_and_description_when_given(tmp_path, monkeypatch):
    """The folder target and description are the only way to file an upload, so
    dropping either lands the file at the account root with no metadata."""

    client, payload = _upload_client(tmp_path, monkeypatch)
    monkeypatch.setattr(gf, "MultipartEncoder", None)
    monkeypatch.setattr(gf, "MultipartEncoderMonitor", None)

    client.upload(payload, folder_id="FOLDER", description="a note")

    _, _, kwargs = client.session.calls[0]
    assert kwargs["data"]["folderId"] == "FOLDER"
    assert kwargs["data"]["description"] == "a note"
    assert kwargs["files"]["file"][0] == "payload.bin"


def test_upload_without_a_token_sends_no_token_field(tmp_path, monkeypatch):
    """A guest-less client must not post token=None as a literal string, which
    gofile.io reads as an invalid credential rather than as absent."""

    client, payload = _upload_client(tmp_path, monkeypatch)
    client.token = None
    monkeypatch.setattr(gf, "MultipartEncoder", None)
    monkeypatch.setattr(gf, "MultipartEncoderMonitor", None)

    client.upload(payload)

    _, _, kwargs = client.session.calls[0]
    assert "token" not in kwargs["data"]


def test_upload_uses_the_multipart_monitor_when_toolbelt_is_available(tmp_path, monkeypatch):
    """The monitor path must send its own boundary Content-Type; posting the
    monitor with a hand-written header makes requests compute a second,
    different boundary and the server rejects the body."""

    client, payload = _upload_client(tmp_path, monkeypatch)
    progress = _install_fake_toolbelt(monkeypatch)

    client.upload(payload, description="a note")

    _, _, kwargs = client.session.calls[0]
    assert kwargs["headers"]["Content-Type"] == "multipart/form-data; boundary=FAKE"
    assert progress  # the encoder/monitor pair was actually constructed
    encoder = progress[0].encoder
    assert encoder.fields["token"] == "TOKEN"
    assert encoder.fields["description"] == "a note"
    assert encoder.fields["file"][0] == "payload.bin"


def test_upload_progress_callback_reports_the_percentage(tmp_path, monkeypatch):
    """A progress bar that never advances makes a large upload look hung; the
    callback has to render against the encoder's total length."""

    class _StubMonitor:
        # requests_toolbelt calls callback(monitor) with one arg; the source reads bytes_read.
        def __init__(self, bytes_read):
            self.bytes_read = bytes_read

    client, payload = _upload_client(tmp_path, monkeypatch)
    _install_fake_toolbelt(monkeypatch)
    monkeypatch.setattr(gf, "perf_counter", lambda: 1000.0)
    out = _capture_stdout(monkeypatch)

    client.upload(payload)

    _, _, kwargs = client.session.calls[0]
    monitor = kwargs["data"]
    monitor.callback(_StubMonitor(512))
    monitor.callback(_StubMonitor(1024))

    rendered = out.getvalue()
    assert "Uploading payload.bin" in rendered
    assert "50.0% | 512/1024 bytes" in rendered
    assert "100.0% | 1024/1024 bytes" in rendered


def test_upload_falls_back_to_requests_files_without_toolbelt(tmp_path, monkeypatch):
    """requests_toolbelt is an optional import; without it the upload must still
    go out as a plain multipart POST instead of silently uploading nothing."""

    client, payload = _upload_client(tmp_path, monkeypatch)
    monkeypatch.setattr(gf, "MultipartEncoder", None)
    monkeypatch.setattr(gf, "MultipartEncoderMonitor", None)

    result = client.upload(payload, folder_id="FOLDER", description="a note")

    _, url, kwargs = client.session.calls[0]
    assert url == "https://fixed01.gofile.io/uploadFile"
    assert "files" in kwargs
    assert kwargs["data"] == {"token": "TOKEN", "folderId": "FOLDER", "description": "a note"}
    assert kwargs["files"]["file"][0] == "payload.bin"
    assert result == _UPLOAD_RESULT


def test_upload_raises_on_a_rejected_response(tmp_path, monkeypatch):
    """A 4xx from the upload endpoint has to become a GofileError so main() can
    print one line instead of the user seeing a bare HTTP status."""

    client, payload = _upload_client(tmp_path, monkeypatch)
    client.session.responses = [FakeResponse(payload={"status": "error-token"})]
    monkeypatch.setattr(gf, "MultipartEncoder", None)
    monkeypatch.setattr(gf, "MultipartEncoderMonitor", None)

    with pytest.raises(gf.GofileError, match="error-token"):
        client.upload(payload)


def test_upload_reports_an_http_failure_rather_than_returning_nothing(tmp_path, monkeypatch):
    client, payload = _upload_client(tmp_path, monkeypatch)
    client.session.responses = [FakeResponse(status_code=413, payload={"status": "ok"})]
    monkeypatch.setattr(gf, "MultipartEncoder", None)
    monkeypatch.setattr(gf, "MultipartEncoderMonitor", None)

    with pytest.raises(gf.GofileError, match="HTTP 413"):
        client.upload(payload)


# --------------------------------------------------------------------------
# GofileClient.resolve_direct_links
# --------------------------------------------------------------------------


def _resolve_client(nodes):
    client = gf.GofileClient(token="TOKEN")
    client.session = FakeSession()
    client.asked = []

    def _get_content(content_id, password=None):
        client.asked.append((content_id, password))
        return nodes[content_id]

    client.get_content = _get_content
    return client


def test_resolve_direct_links_returns_one_entry_per_file(tmp_path):
    """A flat folder must yield exactly its files; dropping or duplicating one
    makes the user re-run to find what they missed."""

    nodes = {
        "root": {
            "id": "root",
            "type": "folder",
            "children": {
                "a": {"id": "a", "type": "file", "name": "a.bin", "size": 1, "link": "https://store/dl/a"},
                "b": {"id": "b", "type": "file", "name": "b.bin", "size": 2, "link": "https://store/dl/b"},
            },
        }
    }
    client = _resolve_client(nodes)

    entries = client.resolve_direct_links("root")

    assert [e["name"] for e in entries] == ["a.bin", "b.bin"]
    assert {e["directLink"] for e in entries} == {"https://store/dl/a", "https://store/dl/b"}


def test_resolve_direct_links_walks_nested_folders_with_the_right_parent_id(tmp_path):
    """parentContentId is what download() uses to select a file inside a folder;
    a nested file stamped with the root id cannot be fetched on its own."""

    nodes = {
        "root": {
            "id": "root",
            "type": "folder",
            "children": {
                "sub": {"id": "sub", "type": "folder", "name": "sub"},
                "top": {"id": "top", "type": "file", "name": "top.bin", "link": "https://store/dl/top"},
            },
        },
        "sub": {
            "id": "sub",
            "type": "folder",
            "name": "sub",
            "children": {
                "deep": {"id": "deep", "type": "file", "name": "deep.bin", "link": "https://store/dl/deep"}
            },
        },
    }
    client = _resolve_client(nodes)

    entries = client.resolve_direct_links("root")

    by_name = {e["name"]: e for e in entries}
    assert by_name["top.bin"]["parentContentId"] == "root"
    assert by_name["deep.bin"]["parentContentId"] == "sub"
    assert [content_id for content_id, _ in client.asked] == ["root", "sub"]


def test_resolve_direct_links_stops_at_the_top_level_when_not_recursive(tmp_path):
    """--no-recursive promises a flat listing; descending anyway returns links
    for files the user explicitly excluded."""

    nodes = {
        "root": {
            "id": "root",
            "type": "folder",
            "children": {"sub": {"id": "sub", "type": "folder", "name": "sub"}},
        },
        "sub": {
            "id": "sub",
            "type": "folder",
            "children": {
                "deep": {"id": "deep", "type": "file", "name": "deep.bin", "link": "https://store/dl/deep"}
            },
        },
    }
    client = _resolve_client(nodes)

    with pytest.raises(gf.GofileError, match="No file entries found"):
        client.resolve_direct_links("root", recursive=False)

    assert [content_id for content_id, _ in client.asked] == ["root"]


def test_resolve_direct_links_visits_each_folder_once(tmp_path):
    """A folder referenced twice in a listing must not be fetched twice, or a
    deep tree multiplies API calls until the account is throttled."""

    nodes = {
        "root": {
            "id": "root",
            "type": "folder",
            "children": {
                "sub": {"id": "sub", "type": "folder", "name": "sub"},
                "again": {"id": "sub", "type": "folder", "name": "sub"},
            },
        },
        "sub": {
            "id": "sub",
            "type": "folder",
            "children": {
                "deep": {"id": "deep", "type": "file", "name": "deep.bin", "link": "https://store/dl/deep"}
            },
        },
    }
    client = _resolve_client(nodes)

    entries = client.resolve_direct_links("root")

    assert [e["name"] for e in entries] == ["deep.bin"]
    assert [content_id for content_id, _ in client.asked] == ["root", "sub"]


def test_resolve_direct_links_passes_the_password_through_to_every_request(tmp_path):
    """A protected tree needs the password on each folder request; sending it
    only for the root leaves the nested walk unauthorized."""

    nodes = {
        "root": {
            "id": "root",
            "type": "folder",
            "children": {"sub": {"id": "sub", "type": "folder", "name": "sub"}},
        },
        "sub": {
            "id": "sub",
            "type": "folder",
            "children": {
                "deep": {"id": "deep", "type": "file", "name": "deep.bin", "link": "https://store/dl/deep"}
            },
        },
    }
    client = _resolve_client(nodes)

    client.resolve_direct_links("root", password="pw")

    assert client.asked == [("root", "pw"), ("sub", "pw")]


def test_resolve_direct_links_handles_a_single_file_content_id(tmp_path):
    """Resolving a file link (not a folder) must return that file rather than
    raising the no-entries error."""

    nodes = {"solo": {"id": "solo", "type": "file", "name": "solo.bin", "link": "https://store/dl/solo"}}
    client = _resolve_client(nodes)

    entries = client.resolve_direct_links("solo")

    assert [e["name"] for e in entries] == ["solo.bin"]


def test_resolve_direct_links_reports_an_empty_folder_as_an_error(tmp_path):
    """A folder with no files is a real "nothing to do" condition; returning an
    empty list makes callers print a success with zero rows."""

    nodes = {"root": {"id": "root", "type": "folder", "children": {}}}
    client = _resolve_client(nodes)

    with pytest.raises(gf.GofileError, match="No file entries found in content"):
        client.resolve_direct_links("root")


def test_resolve_direct_links_skips_children_that_have_no_direct_link(tmp_path):
    """A child without a link cannot be downloaded; reporting it as a usable
    entry hands out a None link the caller then has to filter."""

    nodes = {
        "root": {
            "id": "root",
            "type": "folder",
            "children": {
                "a": {"id": "a", "type": "file", "name": "a.bin", "link": "https://store/dl/a"},
                "b": {"id": "b", "type": "file", "name": "b.bin"},
            },
        }
    }
    client = _resolve_client(nodes)

    entries = client.resolve_direct_links("root")

    assert [e["name"] for e in entries] == ["a.bin"]


# --------------------------------------------------------------------------
# Downloader.run orchestration
# --------------------------------------------------------------------------


def _run_downloader(root, url, *, interactive=False, password=None, session=None):
    """Build a Downloader wired for run(), with the collaborators stubbed out."""

    downloader = _downloader(root, session=session)
    downloader._url = url
    downloader._interactive = interactive
    downloader._password = password
    return downloader


def test_run_refuses_a_url_without_a_d_segment(tmp_path, monkeypatch):
    """`/w/abc` is not a content link; walking it would query the wrong id."""

    buffer = _capture_stdout(monkeypatch)
    downloader = _run_downloader(tmp_path, "https://gofile.io/w/abc123")
    downloader._build_content_tree_structure = lambda *a, **k: pytest.fail("must not walk the tree")

    downloader.run()

    assert "doesn't have an id in it" in buffer.getvalue()


def test_run_reports_a_url_too_short_to_carry_an_id(tmp_path, monkeypatch):
    buffer = _capture_stdout(monkeypatch)
    downloader = _run_downloader(tmp_path, "abc123")
    downloader._build_content_tree_structure = lambda *a, **k: pytest.fail("must not walk the tree")

    downloader.run()

    assert "doesn't seem a valid url" in buffer.getvalue()


def test_run_removes_the_content_directory_when_nothing_was_found(tmp_path, monkeypatch):
    """An empty folder leaves behind a bare content id directory; it must be cleaned."""

    buffer = _capture_stdout(monkeypatch)
    content_dir = tmp_path / "abc123"
    content_dir.mkdir()

    downloader = _run_downloader(tmp_path, "https://gofile.io/d/abc123")
    downloader._build_content_tree_structure = lambda *a, **k: None

    downloader.run()

    assert "Empty directory for url" in buffer.getvalue()
    assert not content_dir.exists()


def test_run_hashes_the_password_before_it_reaches_the_api(tmp_path):
    """The wire format is a sha256 digest, so a raw password silently 401s."""

    downloader = _run_downloader(tmp_path, "https://gofile.io/d/abc123", password="sleepygoose")
    seen = {}

    def spy(parent_dir, content_id, password):
        seen["password"] = password

    downloader._build_content_tree_structure = spy

    downloader.run()

    assert seen["password"] == sha256(b"sleepygoose").hexdigest()


def test_run_passes_no_password_when_none_was_given(tmp_path):
    downloader = _run_downloader(tmp_path, "https://gofile.io/d/abc123")
    seen = {}
    downloader._build_content_tree_structure = lambda *a, **k: seen.update(password=k.get("password", a[2] if len(a) > 2 else None))

    downloader.run()

    assert seen["password"] is None


def test_run_skips_the_interactive_selection_when_it_is_disabled(tmp_path):
    downloader = _run_downloader(tmp_path, "https://gofile.io/d/abc123", interactive=False)
    downloader._build_content_tree_structure = lambda *a, **k: None
    downloader._do_interactive = lambda *a: pytest.fail("must not prompt in non-interactive mode")

    called = []
    downloader._threaded_downloads = lambda: called.append(True)

    downloader.run()

    assert called == [True]


def test_run_asks_for_a_selection_when_interactive(tmp_path):
    downloader = _run_downloader(tmp_path, "https://gofile.io/d/abc123", interactive=True)
    downloader._build_content_tree_structure = lambda *a, **k: None
    seen = []
    downloader._do_interactive = lambda content_dir: seen.append(content_dir)
    downloader._threaded_downloads = lambda: None

    downloader.run()

    assert seen == [str(tmp_path / "abc123")]


# --------------------------------------------------------------------------
# Downloader._threaded_downloads
# --------------------------------------------------------------------------


def test_threaded_downloads_surfaces_a_worker_failure_instead_of_swallowing_it(tmp_path, monkeypatch):
    """The futures were never awaited, so every worker error vanished silently."""

    buffer = _capture_stdout(monkeypatch)
    downloader = _downloader(tmp_path)
    downloader._files_info = {"0": _file_info(tmp_path)}
    downloader._download_content = lambda info: (_ for _ in ()).throw(RuntimeError("boom"))

    downloader._threaded_downloads()

    assert "Failed to download a file: boom" in buffer.getvalue()


def test_threaded_downloads_submits_nothing_once_the_stop_event_is_set(tmp_path):
    downloader = _downloader(tmp_path, stop_event=threading.Event())
    downloader._stop_event.set()
    downloader._files_info = {"0": _file_info(tmp_path)}
    downloader._download_content = lambda info: pytest.fail("must not submit after a stop")

    downloader._threaded_downloads()


def test_threaded_downloads_downloads_every_registered_file(tmp_path):
    downloader = _downloader(tmp_path)
    downloader._files_info = {
        "0": _file_info(tmp_path, "a.bin"),
        "1": _file_info(tmp_path, "b.bin"),
    }
    seen = []
    downloader._download_content = lambda info: seen.append(info["filename"])

    downloader._threaded_downloads()

    assert sorted(seen) == ["a.bin", "b.bin"]


# --------------------------------------------------------------------------
# Downloader._print_list_files
# --------------------------------------------------------------------------


def test_print_list_files_lists_every_registered_index(tmp_path, monkeypatch):
    buffer = _capture_stdout(monkeypatch)
    downloader = _downloader(tmp_path)
    downloader._files_info = {
        "0": _file_info(tmp_path, "a.bin"),
        "1": _file_info(tmp_path, "bb.bin"),
    }

    downloader._print_list_files()

    out = buffer.getvalue()
    assert "[0] ->" in out and "a.bin" in out
    assert "[1] ->" in out and "bb.bin" in out
    assert "------" in out


def test_print_list_files_truncates_a_path_longer_than_100_characters(tmp_path, monkeypatch):
    buffer = _capture_stdout(monkeypatch)
    downloader = _downloader(tmp_path)
    deep = tmp_path / ("d" * 60) / ("e" * 60)
    downloader._files_info = {"0": {"path": str(deep), "filename": "f.bin", "link": DOWNLOAD_LINK}}

    downloader._print_list_files()

    out = buffer.getvalue()
    assert "..." in out
    assert str(deep) not in out


# --------------------------------------------------------------------------
# Downloader._do_interactive
# --------------------------------------------------------------------------


def _interactive_downloader(root):
    downloader = _downloader(root)
    downloader._files_info = {
        "0": _file_info(root, "a.bin"),
        "1": _file_info(root, "b.bin"),
        "2": _file_info(root, "c.bin"),
    }
    return downloader


def test_do_interactive_keeps_every_file_when_the_answer_is_empty(tmp_path, monkeypatch):
    downloader = _interactive_downloader(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: "")

    downloader._do_interactive(str(tmp_path))

    assert sorted(downloader._files_info) == ["0", "1", "2"]


def test_do_interactive_keeps_only_the_chosen_indexes(tmp_path, monkeypatch):
    downloader = _interactive_downloader(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: "0 2")

    downloader._do_interactive(str(tmp_path))

    assert sorted(downloader._files_info) == ["0", "2"]


def test_do_interactive_drops_everything_when_only_unknown_indexes_are_typed(tmp_path, monkeypatch):
    """An answer of pure garbage must not silently download the whole folder."""

    buffer = _capture_stdout(monkeypatch)
    content_dir = tmp_path / "abc123"
    content_dir.mkdir()
    downloader = _interactive_downloader(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: "9 42")

    downloader._do_interactive(str(content_dir))

    assert "Nothing done." in buffer.getvalue()
    assert downloader._files_info == {}
    assert not content_dir.exists()


def test_do_interactive_ignores_whitespace_separators(tmp_path, monkeypatch):
    downloader = _interactive_downloader(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *a: "  1\t2  ")

    downloader._do_interactive(str(tmp_path))

    assert sorted(downloader._files_info) == ["1", "2"]


# --------------------------------------------------------------------------
# Downloader._resolve_naming_collision
# --------------------------------------------------------------------------


def test_resolve_naming_collision_returns_the_bare_path_the_first_time(tmp_path):
    counter = {}

    resolved = gf.Downloader._resolve_naming_collision(counter, str(tmp_path), "a.bin")

    assert resolved == str(tmp_path / "a.bin")
    assert counter == {str(tmp_path / "a.bin"): 0}


def test_resolve_naming_collision_inserts_the_count_before_a_file_extension(tmp_path):
    counter = {}

    first = gf.Downloader._resolve_naming_collision(counter, str(tmp_path), "a.bin")
    second = gf.Downloader._resolve_naming_collision(counter, str(tmp_path), "a.bin")

    assert first == str(tmp_path / "a.bin")
    assert second == str(tmp_path / "a(1).bin")


def test_resolve_naming_collision_appends_the_count_to_a_directory_name(tmp_path):
    counter = {}

    gf.Downloader._resolve_naming_collision(counter, str(tmp_path), "sub", is_dir=True)
    second = gf.Downloader._resolve_naming_collision(counter, str(tmp_path), "sub", is_dir=True)

    assert second == str(tmp_path / "sub(1)")


def test_resolve_naming_collision_keeps_counting_past_the_first_collision(tmp_path):
    counter = {}

    for expected in ("a.bin", "a(1).bin", "a(2).bin", "a(3).bin"):
        assert gf.Downloader._resolve_naming_collision(counter, str(tmp_path), "a.bin") == str(
            tmp_path / expected
        )


def test_resolve_naming_collision_keeps_a_dotless_name_intact(tmp_path):
    counter = {}

    gf.Downloader._resolve_naming_collision(counter, str(tmp_path), "README")
    second = gf.Downloader._resolve_naming_collision(counter, str(tmp_path), "README")

    assert second == str(tmp_path / "README(1)")


# --------------------------------------------------------------------------
# Downloader._create_dirs / _remove_dir
# --------------------------------------------------------------------------


def test_create_dirs_makes_a_missing_nested_path(tmp_path):
    target = tmp_path / "one" / "two" / "three"

    gf.Downloader._create_dirs(str(target))

    assert target.is_dir()


def test_create_dirs_is_idempotent(tmp_path):
    target = tmp_path / "one"
    target.mkdir()

    gf.Downloader._create_dirs(str(target))

    assert target.is_dir()


def test_remove_dir_deletes_an_empty_directory(tmp_path):
    target = tmp_path / "empty"
    target.mkdir()

    gf.Downloader._remove_dir(str(target))

    assert not target.exists()


def test_remove_dir_swallows_the_error_for_a_directory_still_holding_files(tmp_path):
    """rmdir on a populated directory raises; the leftover must not abort the run."""

    target = tmp_path / "full"
    target.mkdir()
    (target / "keep.bin").write_bytes(b"x")

    gf.Downloader._remove_dir(str(target))

    assert target.is_dir()


def test_remove_dir_swallows_the_error_for_a_path_that_does_not_exist(tmp_path):
    gf.Downloader._remove_dir(str(tmp_path / "never-existed"))


# --------------------------------------------------------------------------
# Manager._stop / Manager._handle_sigint
# --------------------------------------------------------------------------


def test_stop_reports_and_sets_the_stop_event(monkeypatch):
    buffer = _capture_stdout(monkeypatch)
    manager = gf.Manager("https://gofile.io/d/x")

    manager._stop()

    assert "Stopping, please wait..." in buffer.getvalue()
    assert manager._stop_event.is_set()


def test_handle_sigint_stops_once_and_then_ignores_further_interrupts(monkeypatch):
    """SIGINT is ignored after the first one so in-flight chunks can finish."""

    buffer = _capture_stdout(monkeypatch)
    installed = []
    monkeypatch.setattr(gf, "signal", lambda signum, handler: installed.append((signum, handler)))

    manager = gf.Manager("https://gofile.io/d/x")
    manager._handle_sigint(2, None)

    assert manager._stop_event.is_set()
    assert installed == [(gf.SIGINT, gf.SIG_IGN)]
    assert buffer.getvalue().count("Stopping, please wait...") == 1

    buffer.truncate(0)
    buffer.seek(0)
    manager._handle_sigint(2, None)

    assert installed == [(gf.SIGINT, gf.SIG_IGN)]
    assert buffer.getvalue() == ""


def test_run_downloads_nothing_after_an_interactive_all_garbage_answer(tmp_path, monkeypatch):
    """Regression: "Nothing done." used to be a lie. The early return left
    _files_info populated, so run() went on to download every declined file into
    the directory _do_interactive had just deleted."""

    session = FakeSession()
    downloader = _run_downloader(
        tmp_path, "https://gofile.io/d/abc123", interactive=True, session=session
    )
    downloader._build_content_tree_structure = lambda *a, **k: downloader._files_info.update(
        {"0": _file_info(tmp_path, "a.bin")}
    )
    monkeypatch.setattr("builtins.input", lambda *a: "9 42")

    downloader.run()

    assert session.calls == []
    assert downloader._files_info == {}
