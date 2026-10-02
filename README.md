# gofile-downloader

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

This fork is a multi-command gofile.io CLI: it keeps upstream-compatible folder mirroring, while adding focused single-file download, direct-link resolution, and upload commands with stronger download verification.

## Table of Contents

- [Fork vs Upstream](#fork-vs-upstream)
- [Authentication](#authentication)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Modern Commands](#modern-commands)
- [Legacy Compatibility](#legacy-compatibility)
- [Environment Variables](#environment-variables)
- [Contributing](#contributing)
- [License](#license)

## Fork vs Upstream

| Area | Upstream `ltsdw/gofile-downloader` | This fork |
|---|---|---|
| CLI shape | One positional mirroring command | `mirror`, `download`, `resolve`, and `upload` subcommands |
| Single-file download | Not a separate workflow | Dedicated `download` command with `--file-id`, `--dest`, `--filename`, and `--overwrite` |
| Direct links | Mirroring-oriented | Dedicated `resolve` command, including `--json` and `--no-recursive` |
| Uploads | Not included | `upload` command with `--folder-id` and `--description` |
| Token input | `GF_TOKEN` for the legacy path | Modern commands use `--token`, `GOFILE_TOKEN`, or `api.txt`; legacy `mirror` still uses `GF_TOKEN` |
| Account bootstrap | Guest account flow in legacy path | Guest account bootstrap retained for both modern and legacy paths |
| Download verification | Legacy resume-oriented behavior | Modern downloads check expected size and MD5 when available; failed partial files are removed |
| Destination directories | Legacy `GF_DOWNLOAD_DIR` follows the upstream documented contract as a pre-existing directory | Modern `--dest` creates a missing directory; an existing file path is still written exactly |
| Upload progress dependency | `requests` only | `requests` is enough for basic operation; `requests-toolbelt` adds streaming upload progress |

## Authentication

For modern commands, the token is checked in this order:

1. `--token` command-line flag
2. `GOFILE_TOKEN` environment variable
3. First non-empty line in `api.txt`, either beside the script or in the working directory

Example `api.txt`:

```text
your_token_here
```

If no token is supplied, the client still bootstraps a guest account automatically.

Legacy `mirror` uses `GF_TOKEN` when present and otherwise follows the same guest-account bootstrap behavior.

## Requirements

- Python 3.10 or newer
- `requests` for basic operation
- `requests-toolbelt` only for streaming upload progress

## Installation

Minimal operation:

```bash
pip install requests
```

Full recommended installation, including upload progress support:

```bash
pip install -r requirements.txt
```

Clone the repository first if you are working from source:

```bash
git clone https://github.com/soficis/gofile-downloader.git
cd gofile-downloader
```

## Quick Start

```bash
python gofile-downloader.py download https://gofile.io/d/CONTENT_ID --dest ./downloads/
```

```bash
python gofile-downloader.py resolve https://gofile.io/d/CONTENT_ID --json
```

```bash
python gofile-downloader.py upload ./myfile.zip --description "My upload"
```

```bash
python gofile-downloader.py mirror https://gofile.io/d/CONTENT_ID
```

Run `python gofile-downloader.py --help` for the complete CLI reference.

## Modern Commands

### Download

Fetch one file with progress and verification.

```bash
python gofile-downloader.py download https://gofile.io/d/CONTENT_ID --dest ./downloads/ --file-id FILE_ID --overwrite
```

- `--file-id`: choose one file when the content contains several files.
- `--dest`: destination directory, created when missing; an existing file path is written exactly.
- `--filename`: output name inside the destination directory, overriding the server-provided name.
- `--password`: password for protected content.
- `--overwrite`: replace an existing destination file.

### Resolve

List direct file URLs contained in gofile content.

```bash
python gofile-downloader.py resolve https://gofile.io/d/CONTENT_ID --json --no-recursive
```

- `--json`: emit the result as a JSON array.
- `--no-recursive`: do not descend into nested folders.
- `--password`: password for protected content.

### Upload

Send a file to gofile.io.

```bash
python gofile-downloader.py upload ./file.pdf --folder-id FOLDER_ID --description "Description"
```

- `--folder-id`: upload into an existing folder.
- `--description`: attach a description to the uploaded file.
- Upload progress is streamed when `requests-toolbelt` is installed; otherwise upload uses a non-progress fallback.

## Legacy Compatibility

The legacy multi-threaded mirroring engine is preserved for existing upstream-style workflows.

Both of these use the legacy engine:

```bash
python gofile-downloader.py mirror https://gofile.io/d/CONTENT_ID --password PASSWORD
```

```bash
python gofile-downloader.py https://gofile.io/d/CONTENT_ID [password]
```

Legacy behavior notes:

- Accepts either one link or a text file containing one link per line.
- Batch text files may include per-line passwords after each link.
- Interactive file selection is controlled with `GF_INTERACTIVE=1`.
- Batch-file mirroring disables interactive selection.
- Legacy downloads retain resume-oriented partial-file behavior.

## Environment Variables

These mainly tune legacy `mirror` behavior:

| Variable | Description | Windows Example | Unix Example |
|---|---|---|---|
| `GF_DOWNLOAD_DIR` | Legacy target directory; upstream documents it as pre-existing | `set GF_DOWNLOAD_DIR="C:\path\to\dir"` | `GF_DOWNLOAD_DIR="/path/to/dir"` |
| `GF_USERAGENT` | Custom User-Agent | `set GF_USERAGENT="custom agent"` | `GF_USERAGENT="custom agent"` |
| `GF_TOKEN` | Legacy API token | `set GF_TOKEN="token"` | `GF_TOKEN="token"` |
| `GF_INTERACTIVE` | Enable legacy file selection (`1` enables it) | `set GF_INTERACTIVE="1"` | `GF_INTERACTIVE="1"` |
| `GF_MAX_CONCURRENT_DOWNLOADS` | Maximum parallel legacy downloads | `set GF_MAX_CONCURRENT_DOWNLOADS="5"` | `GF_MAX_CONCURRENT_DOWNLOADS="5"` |
| `GF_MAX_RETRIES` | Retry attempts after timeouts | `set GF_MAX_RETRIES="5"` | `GF_MAX_RETRIES="5"` |
| `GF_TIMEOUT` | Connection timeout, in seconds | `set GF_TIMEOUT="15.0"` | `GF_TIMEOUT="15.0"` |
| `GF_CHUNK_SIZE` | Legacy download chunk size, in bytes | `set GF_CHUNK_SIZE="2097152"` | `GF_CHUNK_SIZE="2097152"` |

Modern `GofileClient` commands use `GOFILE_TOKEN`, not the `GF_*` legacy variables, except where explicitly documented.

## Contributing

Contributions are welcome! Please open issues or pull requests on GitHub.

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.
