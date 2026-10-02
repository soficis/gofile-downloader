#! /usr/bin/env python3
# noqa: SIZE_OK - intentional single-file distributable: the documented install is "curl one file and run it", so a module split would break every existing copy.

import argparse
import json
from os import getcwd, getenv, listdir, makedirs, name, path, rmdir
from pathlib import Path
from sys import argv, exit, stdout, stderr
from typing import Any, Dict, Iterator, List, NoReturn, Optional, TextIO
from types import FrameType
from urllib.parse import parse_qs, urlparse, ParseResult
from itertools import count
from requests import Session, Response, Timeout, RequestException
try:
    from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
except Exception:
    MultipartEncoder = None
    MultipartEncoderMonitor = None
from requests.structures import CaseInsensitiveDict
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from hashlib import md5, sha256
from shutil import move
from signal import signal, SIGINT, SIG_IGN
from time import perf_counter, time
from math import ceil


NEW_LINE: str = "\n" if name != "nt" else "\r\n"
API_BASE: str = "https://api.gofile.io"
TOKEN_FILENAME: str = "api.txt"
# WEB_LOCALE is folded into both the X-BL header and the website-token salt; changing one without the other silently breaks auth.
WEB_LOCALE: str = "en-US"
WEBSITE_TOKEN_SALT: str = "12af056dacea0b"
# The website token is derived from the User-Agent actually sent, so the API rejects a default that looks like tooling.
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def has_ansi_support() -> bool:
    """Return True when the terminal is able to render ANSI escapes."""

    import os
    import sys

    if not sys.stdout.isatty():
        return False

    if os.name == "nt":
        # Not sure, but I think the console on win10+ have default ansi support
        return sys.getwindowsversion().major >= 10

    return True


# I hope these 100 character are enough for fallback,
# anyone using win7 still?
TERMINAL_CLEAR_LINE: str = f"\r{' ' * 100} \r" if not has_ansi_support() else "\033[2K\r"


def _print(msg: str, error: bool = False) -> None:
    """Write msg to stdout, or to stderr when error is set."""

    output: TextIO = stderr if error else stdout
    output.write(msg)
    output.flush()


def _print_progress(prefix: str, transferred: int, total: Optional[int], start_time: float) -> None:
    """Print a single-line progress indicator with rate and percentage.

    This uses carriage return to update the same terminal line.
    """

    elapsed = perf_counter() - start_time
    rate = transferred / elapsed if elapsed > 0 else 0.0

    if total and total > 0:
        percent = transferred / total * 100
        eta = (total - transferred) / rate if rate > 0 else None
        eta_text = f" ETA {ceil(eta)}s" if eta is not None else ""
        size_text = f"{transferred}/{total} bytes"
        progress_text = f"{percent:5.1f}%"
    else:
        size_text = f"{transferred} bytes"
        progress_text = "   -  "
        eta_text = ""

    unit = "B/s"
    display_rate = rate
    if rate >= 1024 ** 3:
        display_rate = rate / (1024 ** 3)
        unit = "GB/s"
    elif rate >= 1024 ** 2:
        display_rate = rate / (1024 ** 2)
        unit = "MB/s"
    elif rate >= 1024:
        display_rate = rate / 1024
        unit = "KB/s"

    _print(f"\r{prefix} | {progress_text} | {size_text} | {display_rate:.1f}{unit}{eta_text}")


def die(msg: str) -> NoReturn:
    """Print msg to stderr and exit with a non-zero status."""

    _print(f"{msg}{NEW_LINE}", True)
    exit(-1)


def generate_website_token(user_agent: str, account_token: str) -> str:
    """Compute the X-Website-Token the web app sends.

    It is bound to the User-Agent actually transmitted and rotates every four hours,
    so a token minted for a different agent or slot is rejected as error-token.
    """

    time_slot = int(time()) // 14400
    raw = f"{user_agent}::{WEB_LOCALE}::{account_token}::{time_slot}::{WEBSITE_TOKEN_SALT}"
    return sha256(raw.encode()).hexdigest()


class GofileError(RuntimeError):
    """Raised when gofile.io responds with an error."""


class GofileClient:
    """Minimal client to interact with gofile.io's public API."""

    _DEFAULT_HEADERS: CaseInsensitiveDict[str] = CaseInsensitiveDict(
        {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Origin": "https://gofile.io",
            "Referer": "https://gofile.io/",
            "User-Agent": DEFAULT_USER_AGENT,
        }
    )

    def __init__(self, token: Optional[str] = None) -> None:
        self.session: Session = Session()
        self.session.headers.update(self._DEFAULT_HEADERS)
        self.token: Optional[str] = token or getenv("GOFILE_TOKEN") or load_token_from_file()
        self._attach_token(self.token)

    @property
    def user_agent(self) -> str:
        """The User-Agent actually set on the session, which the website token is bound to."""

        return self.session.headers.get("User-Agent", DEFAULT_USER_AGENT)

    def _website_headers(self, account_token: Optional[str] = None) -> Dict[str, str]:
        return {
            "X-Website-Token": generate_website_token(self.user_agent, account_token or ""),
            "X-BL": WEB_LOCALE,
        }

    def ensure_account(self) -> str:
        """Create a guest account so authenticated-only endpoints stop returning error-token."""

        if self.token:
            return self.token
        response = self.session.post(
            f"{API_BASE}/accounts",
            headers=self._website_headers(),
            timeout=30,
        )
        response.raise_for_status()
        self.token = self._ensure_ok(response.json())["token"]
        self._attach_token(self.token)
        return self.token

    def _attach_token(self, token: Optional[str]) -> None:
        if not token:
            return
        # Public API accepts the token either via cookie or Bearer header.
        self.session.cookies.set("accountToken", token)
        self.session.headers.update({"Authorization": f"Bearer {token}"})

    def _ensure_ok(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if payload.get("status") != "ok":
            raise GofileError(payload.get("status", "error"))
        return payload["data"]

    def get_server(self) -> str:
        response = self.session.get(f"{API_BASE}/getServer", timeout=30)
        response.raise_for_status()
        return self._ensure_ok(response.json())["server"]

    def upload(
        self,
        file_path: Path,
        folder_id: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Dict[str, Any]:
        server = self.get_server()
        url = f"https://{server}.gofile.io/uploadFile"
        payload: Dict[str, Any] = {}
        if self.token:
            payload["token"] = self.token
        if folder_id:
            payload["folderId"] = folder_id
        if description:
            payload["description"] = description

        # Prefer requests-toolbelt MultipartEncoderMonitor for progress-aware multipart upload
        if MultipartEncoder and MultipartEncoderMonitor:
            fields = {"file": (file_path.name, file_path.open("rb"), "application/octet-stream")}
            for k, v in payload.items():
                fields[k] = str(v)

            encoder = MultipartEncoder(fields=fields)
            start = perf_counter()

            def _callback(monitor) -> None:
                _print_progress(f"Uploading {file_path.name}", monitor.bytes_read, encoder.len, start)

            monitor = MultipartEncoderMonitor(encoder, _callback)
            headers = {"Content-Type": monitor.content_type}
            response = self.session.post(url, data=monitor, headers=headers, timeout=120)
            _print("\n")
            response.raise_for_status()
            return self._ensure_ok(response.json())

        # Fallback: simple upload without multipart progress
        with file_path.open("rb") as fh:
            response = self.session.post(url, files={"file": (file_path.name, fh)}, data=payload, timeout=120)
        response.raise_for_status()
        return self._ensure_ok(response.json())

    def get_content(self, content_id: str, password: Optional[str] = None) -> Dict[str, Any]:
        account_token = self.ensure_account()
        url = f"{API_BASE}/contents/{content_id}"
        params: Dict[str, Any] = {
            "cache": "true",
            "sortField": "createTime",
            "sortDirection": "1",
        }
        if password:
            params["password"] = sha256(password.encode()).hexdigest()
        response = self.session.get(
            url,
            params=params,
            headers=self._website_headers(account_token),
            timeout=30,
        )
        response.raise_for_status()
        return self._ensure_ok(response.json())

    @staticmethod
    def _extract_child_id(entry: Dict[str, Any]) -> Optional[str]:
        for key in ("id", "contentId", "folderId"):
            value = entry.get(key)
            if value:
                return value
        return None

    @staticmethod
    def _build_file_descriptor(
        entry: Dict[str, Any], parent_id: str
    ) -> Optional[Dict[str, Any]]:
        direct_link = entry.get("link") or entry.get("linkDirect")
        if not direct_link:
            return None

        file_id = entry.get("fileId") or entry.get("id")
        name = entry.get("name") or entry.get("fileName") or entry.get("displayName")
        return {
            "fileId": file_id,
            "name": name,
            "size": entry.get("size"),
            "md5": entry.get("md5"),
            "directLink": direct_link,
            "parentContentId": parent_id,
        }

    def _file_entries_for_node(
        self, node: Dict[str, Any], parent_id: str
    ) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []

        if node.get("type") == "file":
            descriptor = self._build_file_descriptor(node, parent_id)
            if descriptor:
                entries.append(descriptor)

        for child in node.get("children", {}).values():
            if child.get("type") != "file":
                continue
            descriptor = self._build_file_descriptor(child, parent_id)
            if descriptor:
                entries.append(descriptor)

        return entries

    def resolve_direct_links(
        self,
        content_id: str,
        password: Optional[str] = None,
        recursive: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return a list of direct file links for the given content id."""

        results: List[Dict[str, Any]] = []
        visited: set[str] = set()

        stack: List[str] = [content_id]

        while stack:
            current_id = stack.pop()
            if current_id in visited:
                continue
            visited.add(current_id)

            node = self.get_content(content_id=current_id, password=password)
            results.extend(self._file_entries_for_node(node, current_id))

            if not recursive or node.get("type") != "folder":
                continue

            for child in node.get("children", {}).values():
                if child.get("type") != "folder":
                    continue
                child_id = self._extract_child_id(child)
                if child_id and child_id not in visited:
                    stack.append(child_id)

        if not results:
            raise GofileError("No file entries found in content")
        return results

    def download(
        self,
        content_id: str,
        destination: Path,
        file_id: Optional[str] = None,
        password: Optional[str] = None,
        overwrite: bool = False,
        filename: Optional[str] = None,
    ) -> Path:
        payload = self.get_content(content_id=content_id, password=password)
        parent_reference = payload.get("id") or content_id

        files = self._file_entries_for_node(payload, parent_reference)
        if not files:
            raise GofileError("No files found in requested content")

        selected: Optional[Dict[str, Any]] = None

        if file_id:
            for entry in files:
                if entry.get("fileId") == file_id:
                    selected = entry
                    break
            if not selected:
                raise GofileError(f"File id '{file_id}' not found in content")
        else:
            if len(files) > 1:
                raise GofileError(
                    "Multiple files available; provide --file-id to pick one"
                )
            selected = files[0]

        direct_link = selected.get("directLink")
        if not direct_link:
            raise GofileError("Direct download link missing in response")

        server_filename = selected.get("name") or selected.get("fileId") or f"{content_id}.bin"

        # A missing destination is a directory to create: argparse strips trailing
        # slashes, so no heuristic can tell "out" the file from "out" the folder.
        # Only an existing file keeps the write-to-this-exact-path behaviour.
        if filename:
            directory = (
                destination
                if destination.is_dir() or not destination.exists()
                else destination.parent
            )
            target = directory / filename
        elif destination.is_dir() or not destination.exists():
            target = destination / server_filename
        else:
            target = destination

        if target.exists() and not overwrite:
            raise FileExistsError(f"Destination '{target}' already exists")

        expected_size = selected.get("size")
        expected_md5 = selected.get("md5")
        # gofile.io answers an unauthenticated fetch with HTTP 200 and a short HTML
        # body, so the status code alone cannot prove the payload arrived intact.
        partial = target.with_name(target.name + ".part")
        partial.parent.mkdir(parents=True, exist_ok=True)

        try:
            with self.session.get(direct_link, stream=True, timeout=120) as response:
                response.raise_for_status()
                try:
                    total_size = int(response.headers.get("Content-Length") or 0) or None
                except (TypeError, ValueError):
                    total_size = None

                transferred = 0
                start = perf_counter()
                digest = md5()
                with partial.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            fh.write(chunk)
                            digest.update(chunk)
                            transferred += len(chunk)
                            _print_progress(f"Downloading {target.name}", transferred, total_size, start)
            _print("\n")

            self._verify_payload(partial, transferred, expected_size, expected_md5, digest.hexdigest())
            partial.replace(target)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        return target

    @staticmethod
    def _verify_payload(
        partial: Path,
        transferred: int,
        expected_size: Optional[int],
        expected_md5: Optional[str],
        actual_md5: str,
    ) -> None:
        if isinstance(expected_size, int) and transferred != expected_size:
            raise GofileError(
                f"Incomplete download for '{partial.name}': got {transferred} bytes, "
                f"expected {expected_size}"
            )
        if expected_md5 and actual_md5.lower() != str(expected_md5).lower():
            raise GofileError(
                f"Checksum mismatch for '{partial.name}': got {actual_md5}, expected {expected_md5}"
            )


def extract_content_id(value: str) -> str:
    """Return the content id/code extracted from a possible gofile link."""

    if not value:
        raise ValueError("Empty gofile link or code provided")

    parsed = urlparse(value)
    if not parsed.scheme:
        return value

    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts:
        if path_parts[0].lower() == "d" and len(path_parts) >= 2:
            return path_parts[1]
        return path_parts[-1]

    query = parse_qs(parsed.query)
    for key in ("c", "contentId", "contentid", "id"):
        values = query.get(key)
        if values:
            return values[0]

    raise ValueError("Unable to determine content id from link")


def load_token_from_file() -> Optional[str]:
    """Attempt to load an API token from an `api.txt` file."""

    seen: set[Path] = set()
    candidates: List[Path] = []

    try:
        module_dir = Path(__file__).resolve().parent
        candidates.append(module_dir / TOKEN_FILENAME)
    except (NameError, OSError):
        pass

    candidates.append(Path.cwd() / TOKEN_FILENAME)

    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            content = candidate.read_text(encoding="utf-8")
        except OSError:
            continue

        for line in content.splitlines():
            token = line.strip()
            if token:
                return token
    return None


class Downloader:
    def __init__(
        self,
        root_dir: str,
        interactive: bool,
        max_workers: int,
        number_retries,
        timeout: float,
        chunk_size: int,
        stop_event: Event,
        session: Session,
        url: str,
        password: str | None = None,
    ) -> None:
        """
        Downloader class to concurrently manage, download and write files to disk.
        This one does the heavy lifting, the actual working of downloading.

        :root_dir: Directory where files will be saved (defaults to current directory).
        :interactive: Whether download will be interactive or not
                      (it's disabled by default while batch downloading from a text file)
        :max_workers: Maximum number of concurrent workers (tasks).
        :number_retries: The maximum number of connections retries for POST and GET requests.
        :timeout: Maximum number of time to wait until give up on trying to establish a connection.
        :chunk_size: Maximum chunk byte size.
        :stop_event: An Event object to handle the request to stop the program and exit gracefully.
        :session: Session object to handle headers, cookies and allowing reuse of resources and TCP connections.
        :url: The content url to download.
        :password: The content password if it's protected.
        """

        # Dictionary to hold information about file and its directories structure
        # {"index": {"path": "", "filename": "", "link": ""}}
        # where the largest index is the top most file
        self._files_info: dict[str, dict[str, str]] = {}

        self._max_workers: int = max_workers
        self._number_retries: int = number_retries
        self._timeout: float = timeout
        self._interactive: bool = interactive
        self._chunk_size: int = chunk_size
        self._password: str | None = password
        self._session: Session = session
        self._stop_event: Event = stop_event
        self._root_dir: str = root_dir
        self._url: str = url


    def run(self) -> None:
        """
        run

        Requests to start downloading files.

        :return:
        """

        try:
            if not self._url.split("/")[-2] == "d":
                _print(f"The url probably doesn't have an id in it: {self._url}.{NEW_LINE}")
                return

            content_id: str = self._url.split("/")[-1]
        except IndexError:
            _print(f"{self._url} doesn't seem a valid url.{NEW_LINE}")
            return

        _password: str | None = sha256(self._password.encode()).hexdigest() if self._password else None

        content_dir: str = path.join(self._root_dir, content_id)
        self._build_content_tree_structure(content_dir, content_id, _password)

        if path.exists(content_dir) and not listdir(content_dir) and not self._files_info:
            _print(f"Empty directory for url: {self._url}, nothing done.{NEW_LINE}")
            self._remove_dir(content_dir)
            return

        if self._interactive:
            self._do_interactive(content_dir)

        self._threaded_downloads()


    def _get_response(self, **kwargs: Any) -> Response | None:
        """
        Auxiliary function for the requests.session.get.

        :param kwargs: arguments for the requests.session.get function.
        :return: requests.Response or None on requests.Timeout.
        """

        for _ in range(self._number_retries):
            try:
                return self._session.get(timeout=self._timeout, **kwargs)
            except Timeout:
                continue


    def _threaded_downloads(self) -> None:
        """Parallelize the downloads."""

        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            futures = [
                executor.submit(self._download_content, item)
                for item in self._files_info.values()
                if not self._stop_event.is_set()
            ]

            for future in futures:
                try:
                    future.result()
                except Exception as error:
                    _print(
                        f"{TERMINAL_CLEAR_LINE}"
                        f"Failed to download a file: {error}{NEW_LINE}"
                    )


    @staticmethod
    def _create_dirs(dirname: str) -> None:
        """
        Creates a directory and its subdirectories recursively if they don't exist.

        :param dirname: name of the directory to be created.
        """

        makedirs(dirname, exist_ok = True)


    @staticmethod
    def _remove_dir(dirname: str) -> None:
        """
        Removes a directory if it's empty ignoring any throw.

        :param dirname: name of the directory to be removed.
        """

        try:
            rmdir(dirname)
        except OSError:
            pass


    def _download_content(self, file_info: dict[str, str]) -> None:
        """
        Requests the contents of the file and writes it.

        :param file_info: a dictionary with information about a file to be downloaded.
        """

        filepath: str = path.join(file_info["path"], file_info["filename"])

        if self._should_skip_download(filepath):
            return

        tmp_file: str =  f"{filepath}.part"
        url: str = file_info["link"]

        for _ in range(self._number_retries):
            try:
                part_size: int = 0
                headers: dict[str, str] = {}
                if path.isfile(tmp_file):
                    part_size = path.getsize(tmp_file)
                    headers = {"Range": f"bytes={part_size}-"}

                has_size: str | None = self._perform_download(
                    file_info,
                    url,
                    tmp_file,
                    headers,
                    part_size
                )
            except Timeout:
                continue
            else:
                if has_size:
                    self._finalize_download(file_info, tmp_file, has_size)
                break


    @staticmethod
    def _should_skip_download(filepath: str) -> bool:
        """
        _should_skip_download

        Checks if a file already exists and has non-zero size.

        :param filepath: filepath.
        :return: True if download should be skipped, False otherwise.
        """

        if path.exists(filepath) and path.getsize(filepath) > 0:
            _print(f"{filepath} already exist, skipping.{NEW_LINE}")
            return True
        return False


    def _perform_download(
        self,
        file_info: dict[str, str],
        url: str,
        tmp_file: str,
        headers: dict[str, str],
        part_size: int,
    ) -> str | None:
        """
        Executes the HTTP GET request, processes file chunks, and tracks progress.

        :param file_info: a dictionary containing file details.
        :param url: the file download URL.
        :param tmp_file: temporary file path for partial downloads.
        :param headers: request headers.
        :param part_size: the current partial file size.
        :return: the total file size (if available).
        """

        if self._stop_event.is_set():
            return

        response: Response | None = self._get_response(url=url, headers=headers, stream=True)

        if not response:
            _print(
                f"{TERMINAL_CLEAR_LINE}Couldn't download the file, failed to get a response from {url}.{NEW_LINE}"
            )
            return None

        with response:
            status_code: int = response.status_code

            if not self._is_valid_response(status_code, part_size):
                _print(
                    f"{TERMINAL_CLEAR_LINE}"
                    f"Couldn't download the file from {url}.{NEW_LINE}"
                    f"Status code: {status_code}{NEW_LINE}"
                )
                return None

            has_size: str | None = self._extract_file_size(response.headers, part_size)

            if not has_size:
                _print(
                    f"{TERMINAL_CLEAR_LINE}"
                    f"Couldn't find the file size from {url}.{NEW_LINE}"
                    f"Status code: {status_code}{NEW_LINE}"
                )
                return None

            self._write_chunks(
                response.iter_content(chunk_size=self._chunk_size),
                tmp_file,
                part_size,
                float(has_size),
                file_info["filename"]
            )

            return has_size


    @staticmethod
    def _is_valid_response(status_code: int, part_size: int) -> bool:
        """
        Validates HTTP status code based on partial download state.

        :param status_code: the HTTP status code.
        :param part_size: the current partial file size.
        :return: True if status code is acceptable, False otherwise.
        """

        if status_code in (403, 404, 405, 500):
            return False
        if part_size == 0:
            return status_code in (200, 206)
        if part_size > 0:
            return status_code == 206
        return False


    @staticmethod
    def _extract_file_size(headers: CaseInsensitiveDict[str], part_size: int) -> str | None:
        """
        Retrieves the file size from HTTP headers.

        :param headers: the HTTP response headers.
        :param part_size: the current partial file size.
        :return: the total file size as a string, or None if unavailable.
        """

        if part_size == 0:
            return headers.get("Content-Length")

        content_range: str | None = headers.get("Content-Range")

        if not content_range:
            return None

        total: str = content_range.split("/")[-1]

        # "*" is the legal HTTP marker for an unknown total. It must read as
        # unavailable rather than as a size, or the caller's float() raises.
        return None if total == "*" else total


    def _write_chunks(
        self,
        chunks: Iterator[Any],
        tmp_file: str,
        part_size: int,
        total_size: float,
        filename: str
    ) -> None:
        """
        Iterates over download chunks and writes them to disk, updating progress.

        :param chunks: a generator of byte chunks.
        :param tmp_file: temporary file path.
        :param part_size: number of bytes already downloaded.
        :param total_size: total file size in bytes.
        :param filename: the file's name.
        """

        start_time: float = perf_counter()
        written: int = 0

        with open(tmp_file, "ab") as f:
            for chunk in chunks:
                if self._stop_event.is_set():
                    return

                f.write(chunk)
                written += len(chunk)
                self._update_progress(filename, part_size + written, total_size, start_time)


    def _update_progress(
        self,
        filename: str,
        transferred: int,
        total_size: float | None,
        start_time: float
    ) -> None:
        """
        Calculates and displays download progress and transfer rate.

        :param filename: the name of the file being downloaded.
        :param transferred: bytes on disk for this file, including any resumed prefix.
        :param total_size: total file size, or None when the server did not say.
        :param start_time: download start time.
        """

        elapsed: float = max(perf_counter() - start_time, 1e-9)

        if total_size is None:
            return

        progress: float = min(transferred / total_size * 100, 100.0) if total_size else 0.0
        rate: float = transferred / elapsed

        unit: str
        if rate < 1024:
            unit = "B/s"
        elif rate < (1024 ** 2):
            rate /= 1024
            unit = "KB/s"
        elif rate < (1024 ** 3):
            rate /= (1024 ** 2)
            unit = "MB/s"
        else:
            rate /= (1024 ** 3)
            unit = "GB/s"

        _print(
            f"{TERMINAL_CLEAR_LINE}"
            f"Downloading {filename}: {transferred} "
            f"of {int(total_size)} {round(progress, 1)}% {round(rate, 1)}{unit}"
        )


    @staticmethod
    def _finalize_download(file_info: dict[str, str], tmp_file: str, has_size: str) -> None:
        """
        Verifies the final file size and moves the temporary file to its destination.

        :param file_info: a dictionary containing file details.
        :param tmp_file: temporary file path.
        :param has_size: expected file size.
        """

        final_size = path.getsize(tmp_file)
        if final_size == int(has_size):
            _print(
                f"{TERMINAL_CLEAR_LINE}"
                f"Downloading {file_info['filename']}: {final_size} "
                f"of {has_size} Done!{NEW_LINE}"
            )
            move(tmp_file, path.join(file_info["path"], file_info["filename"]))


    def _register_file(self, file_index: count, filepath: str, file_url: str) -> None:
        """
        Registers file information into the internal files info dictionary
        (with sequential index, path, filename and download url).

        :param file_index: an itertools.count object used to sequentially index discovered files.
                           Acts as a mutable counter local to the parsing thread context.
                           Should not be modified outside this function.
        :param filepath: absolute or relative path to the file on the local filesystem.
        :param file_url: remote URL link for downloading the file.
        :return:
        """

        self._files_info[str(next(file_index))] = {
            "path": path.dirname(filepath),
            "filename": path.basename(filepath),
            "link": file_url
        }


    @staticmethod
    def _resolve_naming_collision(
        pathing_count: dict[str, int],
        absolute_parent_dir: str,
        child_name: str,
        is_dir: bool = False,
    ) -> str:
        """
        Ensures unique file or directory paths by checking and updating a naming collision
        tracker. If a collision is detected, appends a numeric suffix to the name to
        avoid overwriting existing paths.

        :param pathing_count: dictionary used to track the number of naming collisions
                              for each path encountered during traversal.
        :param absolute_parent_dir: absolute path to the parent directory where the child
                                    (file or directory) will be created.
        :param child_name: original name of the file or directory.
        :param is_dir: boolean flag indicating whether the child is a directory, defaults to False.
        :return: a unique filepath string with a numeric suffix appended if needed.
        """

        filepath: str = path.join(absolute_parent_dir, child_name)

        if filepath in pathing_count:
            pathing_count[filepath] += 1
        else:
            pathing_count[filepath] = 0

        if pathing_count[filepath] > 0 and is_dir:
            return f"{filepath}({pathing_count[filepath]})"

        if pathing_count[filepath] > 0:
            extension: str
            root, extension = path.splitext(filepath)

            return f"{root}({pathing_count[filepath]}){extension}"

        return filepath


    def _website_headers(self) -> dict[str, str]:
        """
        Build the X-Website-Token headers from the session's own User-Agent and the
        account token, mirroring what the web app computes client-side.

        :return: header mapping to merge into the /contents request.
        """

        user_agent = self._session.headers.get("User-Agent", DEFAULT_USER_AGENT)
        auth = self._session.headers.get("Authorization", "")
        account_token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""

        return {
            "X-Website-Token": generate_website_token(user_agent, account_token),
            "X-BL": WEB_LOCALE,
        }

    def _build_content_tree_structure(
        self,
        parent_dir: str,
        content_id: str,
        password: str | None = None,
        pathing_count: dict[str, int] | None = None,
        file_index: count = count(start=0, step=1)
    ) -> None:
        """
        Recursively traverses a remote content structure and builds a corresponding
        local directory tree (handling naming collisions), while registering files url.

        :param parent_dir: absolute path to the parent directory where the current content
                           directory or file should be created.
        :param content_id: content identifier.
        :param password: optional password to access protected content.
        :param pathing_count: pointer-like dictionary used internally to track naming collisions
                              for file and directory paths. Should not be modified outside this function.
        :param file_index: an itertools.count object used to sequentially index discovered files.
                           Acts as a mutable counter local to the parsing thread context.
                           Should not be modified outside this function.
        :return:
        """

        url: str = f"{API_BASE}/contents/{content_id}?cache=true&sortField=createTime&sortDirection=1"

        if not pathing_count:
            pathing_count = {}

        if password:
            url = f"{url}&password={password}"

        response: Response | None = self._get_response(url=url, headers=self._website_headers())
        json_response: dict[str, Any] = response.json() if response else {}

        if not json_response or json_response["status"] != "ok":
            _print(f"Failed to fetch data response from the {url}.{NEW_LINE}")
            return

        data: dict[str, Any] = json_response["data"]

        if "password" in data and "passwordStatus" in data and data["passwordStatus"] != "passwordOk":
            _print(f"Password protected link. Please provide the password.{NEW_LINE}")
            return

        if data["type"] != "folder":
            self._create_dirs(parent_dir)
            filepath: str = self._resolve_naming_collision(pathing_count, parent_dir, data["name"])
            self._register_file(file_index, filepath, data["link"])
            return

        absolute_path: str = self._resolve_naming_collision(pathing_count, parent_dir, data["name"])

        # If the content directory (the root directory) directory isn't named the same as the content_id,
        # use the content_id as a name for the content directory.
        #
        # Also do not use the default root directory named as "root" created by default.
        if path.basename(parent_dir) == content_id:
            absolute_path = parent_dir

        self._create_dirs(absolute_path)

        for child in data["children"].values():
            if child["type"] == "folder":
                self._build_content_tree_structure(absolute_path, child["id"], password, pathing_count, file_index)
            else:
                filepath: str = self._resolve_naming_collision(pathing_count, absolute_path, child["name"])
                self._register_file(file_index, filepath, child["link"])


    def _print_list_files(self) -> None:
        """Helper function to display a list of all files for selection."""

        MAX_FILENAME_CHARACTERS: int = 100
        width: int = max(len(f"[{v}] -> ") for v in self._files_info)

        for (k, v) in self._files_info.items():
            filepath: str = path.join(v["path"], v["filename"])
            filepath = f"...{filepath[-MAX_FILENAME_CHARACTERS:]}" \
                if len(filepath) > MAX_FILENAME_CHARACTERS \
                else filepath

            text: str = f"[{k}] -> ".ljust(width) + filepath

            _print(f"{text}{NEW_LINE}"
                   f"{'-' * len(text)}"
                   f"{NEW_LINE}"
            )


    def _do_interactive(self, content_dir: str) -> None:
        """
        Performs interactive file selection for download.

        :param content_dir: Content root directory.
        """

        self._print_list_files()

        # Ensure only valid index strings are stored.
        input_list: set[str] = set(input(
            f"Files to download (Ex: 1 3 7) | or leave empty to download them all"
            f"{NEW_LINE}"
            f":: "
        ).split())
        valid_indexes = set(self._files_info)
        input_list = valid_indexes if not input_list \
                     else input_list & valid_indexes

        if not input_list:
            _print(f"Nothing done.{NEW_LINE}")
            self._files_info.clear()
            self._remove_dir(content_dir)
            return

        for key in valid_indexes - input_list:
            del self._files_info[key]


class Manager:
    def __init__(self, url_or_file: str, password: str | None = None) -> None:
        """
        Manager class to handle individual download tasks.

        :url_or_file: This may be an existent text file or url.
        :password: Password if the content is protected.
        """

        root_dir: str | None = getenv("GF_DOWNLOAD_DIR")

        self._max_workers: int = int(getenv("GF_MAX_CONCURRENT_DOWNLOADS", 5))
        self._number_retries: int = int(getenv("GF_MAX_RETRIES", 5))
        # Connection and read timeout, defaults to 15 seconds
        self._timeout: float = float(getenv("GF_TIMEOUT", 15.0))
        self._user_agent: str | None = getenv("GF_USERAGENT")
        self._interactive: bool = getenv("GF_INTERACTIVE") == "1"
        # The number of bytes it should read into memory
        self._chunk_size: int = int(getenv("GF_CHUNK_SIZE", 2097152))

        self._password: str | None = password
        self._url_or_file: str = url_or_file

        self._session: Session = Session()
        self._stop_event: Event = Event()
        self._root_dir: str = root_dir if root_dir else getcwd()

        self._session.headers.update({
            "Accept-Encoding": "gzip",
            "User-Agent": self._user_agent if self._user_agent else DEFAULT_USER_AGENT,
            "Connection": "keep-alive",
            "Accept": "*/*",
            "Origin": "https://gofile.io",
            "Referer": "https://gofile.io/",
        })


    @staticmethod
    def _normalize_http_url(value: str) -> Optional[str]:
        """
        Normalize value as an url if it's a possible url-like string, otherwise returning None.

        :value: possible url-like string.
        :return: a normalized url or None if it can't be normalized as one.
        """

        value = value.strip()
        parsed: ParseResult = urlparse(value)

        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return value

        if not parsed.scheme and "." in parsed.path:
            candidate: str = f"https://{value}"
            parsed = urlparse(candidate)

            if parsed.netloc:
                return candidate

        return None


    def _parse_url_or_file(self) -> None:
        """
        Parses a file or a url for possible links.
        """

        source: str = self._url_or_file.strip()
        filepath: Path = Path(source).expanduser()
        url: Optional[str] = self._normalize_http_url(source)
        is_link_file: bool = filepath.is_file()

        if not is_link_file and not url:
            die(f"{source} is either a valid url or local url file.")

        if not is_link_file and url:
            downloader: Downloader = Downloader(
                self._root_dir,
                self._interactive,
                self._max_workers,
                self._number_retries,
                self._timeout,
                self._chunk_size,
                self._stop_event,
                self._session,
                url,
                self._password
            )

            downloader.run()

            return

        with open(filepath, "r") as f:
            lines: list[str] = f.readlines()

        # I think it's better to limit this one here, the api may get angry if we starve it.
        # We may make this a tunable in the future, but for now let it be hardcoded.
        max_workers: int = self._max_workers if self._max_workers <= 10 else 10

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for line in lines:
                if self._stop_event.is_set():
                    return

                line_splitted: list[str] = line.split(" ")
                source = line_splitted[0].strip()
                url = self._normalize_http_url(source)

                if not url:
                    _print(f"{source} is not a valid url.")
                    continue

                if self._password:
                    password: str | None = self._password
                elif len(line_splitted) > 1:
                    password = line_splitted[1].strip()
                else:
                    password = self._password

                downloader: Downloader = Downloader(
                    self._root_dir,
                    False, # Disable interactive download when downloading a batch from text file.
                    self._max_workers,
                    self._number_retries,
                    self._timeout,
                    self._chunk_size,
                    self._stop_event,
                    self._session,
                    url,
                    password
                )

                executor.submit(downloader.run)


    def run(self) -> None:
        """
        This method starts the download process after the creation of the Downloader object.
        """

        signal(SIGINT, self._handle_sigint)
        _print(f"Starting, please wait...{NEW_LINE}")
        self._set_account_access_token(getenv("GF_TOKEN"))
        self._parse_url_or_file()


    def _set_account_access_token(self, token: str | None = None) -> None:
        """
        Get a new access token for the account created or use the token provided for an already existent account.

        :param token: token to be used accross connections if available.
        """

        if token:
            self._session.cookies.set("accountToken", token)
            self._session.headers.update({"Authorization": f"Bearer {token}"})
            return

        response: dict[Any, Any] = {}
        user_agent = self._session.headers.get("User-Agent", DEFAULT_USER_AGENT)
        auth_headers = {
            "X-Website-Token": generate_website_token(user_agent, ""),
            "X-BL": WEB_LOCALE,
        }

        for _ in range(self._number_retries):
            try:
                response = self._session.post(
                    f"{API_BASE}/accounts",
                    headers=auth_headers,
                    timeout=self._timeout
                ).json()
            except Timeout:
                continue
            else:
                break

        if not response or response.get("status") != "ok":
            die("Account creation failed!")

        self._session.cookies.set("accountToken", response['data']['token'])
        self._session.headers.update({"Authorization": f"Bearer {response['data']['token']}"})


    def _stop(self) -> None:
        """
        Stops all work from continuing.
        """

        _print(f"{TERMINAL_CLEAR_LINE}Stopping, please wait...{NEW_LINE}")
        self._stop_event.set()


    def _handle_sigint(self, _: int, __: FrameType | None) -> None:
        """
        Signal handler triggered when a SIGINT (when pressing CTRL-C) is received.
        Issues the stop event so that the running tasks can close gracefully,
        ignoring tasks that didn't start yet.

        :param signum:  Signal number received (for this callback usually SIGINT).
        :param frame:   FrameType object representing the current stack frame
                        where the received signal was caught.
        """

        if not self._stop_event.is_set():
            self._stop()
            signal(SIGINT, SIG_IGN)


def parse_cli_args(args: Optional[List[str]] = None) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
    parser = argparse.ArgumentParser(
        description="Download, upload, and resolve gofile.io content.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--token",
        help="Gofile API token (falls back to GOFILE_TOKEN env var or api.txt file)",
    )

    subparsers = parser.add_subparsers(dest="command")

    mirror_parser = subparsers.add_parser(
        "mirror",
        help="Use the advanced multi-threaded downloader (legacy behaviour)",
    )
    mirror_parser.add_argument(
        "source",
        help="Gofile folder/content link or path to a text file with links",
    )
    mirror_parser.add_argument(
        "--password",
        help="Password for locked content (overrides per-line passwords in files)",
    )

    upload_parser = subparsers.add_parser("upload", help="Upload a file to gofile.io")
    upload_parser.add_argument("file", type=Path, help="Path to the file to upload")
    upload_parser.add_argument(
        "--folder-id",
        dest="folder_id",
        help="Optional folder id to upload into",
    )
    upload_parser.add_argument(
        "--description",
        help="Optional description to attach to the file",
    )

    download_parser = subparsers.add_parser(
        "download", help="Download a single file (non-recursive)"
    )
    download_parser.add_argument(
        "source",
        help="Gofile content code or share link",
    )
    download_parser.add_argument(
        "--file-id",
        help="Specific file id inside the content (if multiple files exist)",
    )
    download_parser.add_argument(
        "--dest",
        type=Path,
        default=Path.cwd(),
        help="Destination directory (created when missing), or an existing file path",
    )
    download_parser.add_argument(
        "--filename",
        help="Output file name inside the destination directory (overrides the server name)",
    )
    download_parser.add_argument(
        "--password",
        help="Password for locked content, if required",
    )
    download_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite destination file if it already exists",
    )

    resolve_parser = subparsers.add_parser(
        "resolve", help="Resolve a gofile folder link or code to direct file links"
    )
    resolve_parser.add_argument(
        "source",
        help="Gofile folder/content link or code",
    )
    resolve_parser.add_argument(
        "--password",
        help="Password for locked content, if required",
    )
    resolve_parser.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        help="Do not traverse nested folders",
    )
    resolve_parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    resolve_parser.set_defaults(recursive=True)

    for subparser in (mirror_parser, upload_parser, download_parser, resolve_parser):
        subparser.add_argument(
            "--token",
            default=argparse.SUPPRESS,
            help="Gofile API token (overrides the global --token)",
        )

    return parser, parser.parse_args(args)


def _legacy_entry(args: List[str]) -> int:
    if not args:
        die(
            "Usage:\n"
            "python gofile-downloader.py https://gofile.io/d/contentid\n"
            "python gofile-downloader.py https://gofile.io/d/contentid password"
        )

    url_or_file: str = args[0]
    password: Optional[str] = args[1] if len(args) > 1 else None
    manager = Manager(url_or_file=url_or_file, password=password)
    manager.run()
    return 0


def main(argv_: Optional[List[str]] = None) -> int:
    if argv_ is None:
        argv_ = argv[1:]

    commands = {"upload", "download", "resolve", "mirror"}

    if argv_ and not argv_[0].startswith("-") and argv_[0] not in commands:
        return _legacy_entry(argv_)

    parser, args = parse_cli_args(argv_)

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "mirror":
        manager = Manager(url_or_file=args.source, password=args.password)
        manager.run()
        return 0

    client = GofileClient(token=args.token)

    try:
        if args.command == "upload":
            data = client.upload(
                file_path=args.file,
                folder_id=args.folder_id,
                description=args.description,
            )
            _print("Upload complete:" + NEW_LINE)
            _print(f"  File ID: {data.get('fileId')}{NEW_LINE}")
            _print(f"  Code: {data.get('code')}{NEW_LINE}")
            _print(f"  Download page: {data.get('downloadPage')}{NEW_LINE}")
            if "directLink" in data:
                _print(f"  Direct link: {data['directLink']}{NEW_LINE}")
            return 0

        if args.command == "download":
            content_id = extract_content_id(args.source)
            destination = client.download(
                content_id=content_id,
                destination=args.dest,
                file_id=args.file_id,
                password=args.password,
                overwrite=args.overwrite,
                filename=args.filename,
            )
            _print(f"Downloaded to {destination}{NEW_LINE}")
            return 0

        if args.command == "resolve":
            content_id = extract_content_id(args.source)
            entries = client.resolve_direct_links(
                content_id=content_id,
                password=args.password,
                recursive=args.recursive,
            )
            if args.json:
                _print(json.dumps(entries, indent=2) + NEW_LINE)
            else:
                _print(f"Resolved {len(entries)} file(s):{NEW_LINE}")
                for entry in entries:
                    size = entry.get("size")
                    size_text = f" ({size} bytes)" if size is not None else ""
                    name = entry.get("name") or entry.get("fileId")
                    _print(f"- {name}{size_text}: {entry['directLink']}{NEW_LINE}")
            return 0

    except FileExistsError as exc:
        _print(f"{exc}{NEW_LINE}", True)
        return 1
    except ValueError as exc:
        _print(f"Error: {exc}{NEW_LINE}", True)
        return 1
    except (RequestException, GofileError) as exc:
        _print(f"Error: {exc}{NEW_LINE}", True)
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    exit(main())

