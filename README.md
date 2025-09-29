# gofile-downloader

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

A versatile command-line client for [gofile.io](https://gofile.io) that can mirror folders, resolve direct download links, and upload files using the public API.

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Authentication](#authentication)
- [Quick Start](#quick-start)
- [Usage](#usage)
- [Differences from Upstream](#differences-from-upstream-ltsdwgofile-downloader)
- [Environment Variables](#environment-variables)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Mirror / Bulk Download**: High-performance, multi-threaded downloader for entire folders or batches of links (legacy behavior preserved).
- **Single-File Download**: Quickly grab a specific file by content code or link, with optional password support and progress bars.
- **Resolve Direct Links**: Convert any gofile folder link into direct file URLs (optionally as JSON).
- **Upload**: Send files to gofile.io with optional descriptions, folder IDs, and progress bars.
- **Token Helpers**: Automatically pick up your API token from `--token`, the `GOFILE_TOKEN` environment variable, or an `api.txt` file.
- **Progress Indicators**: Real-time progress bars for downloads and uploads, showing percentage, transfer rate, and ETA.

## Requirements

- Python 3.10 or newer
- Dependencies listed in `requirements.txt`

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/soficis/gofile-downloader.git
   cd gofile-downloader
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

## Authentication

To access private content or upload files, obtain an API token from your [gofile.io account](https://gofile.io/myProfile).

The token is checked in this order:

1. `--token` command-line flag
2. `GOFILE_TOKEN` environment variable
3. First non-empty line in `api.txt` (next to the script or in the working directory)

Example `api.txt`:

```text
your_token_here
```

## Quick Start

### Download a Folder (Mirror)

```bash
python gofile-downloader.py mirror https://gofile.io/d/CONTENT_ID
```

### Resolve Direct Links

```bash
python gofile-downloader.py resolve https://gofile.io/d/CONTENT_ID --json
```

### Upload a File

```bash
python gofile-downloader.py upload ./myfile.zip --description "My upload"
```

### Download a Single File

```bash
python gofile-downloader.py download https://gofile.io/d/CONTENT_ID --dest ./downloads/
```

## Usage

Run `python gofile-downloader.py --help` for the full reference.

### Commands Overview

| Command  | Purpose                                      | Example |
|----------|----------------------------------------------|---------|
| `mirror` | Multi-threaded folder mirroring (legacy)     | `python gofile-downloader.py mirror <url>` |
| `download` | Single-file download with progress         | `python gofile-downloader.py download <url> --dest <dir>` |
| `resolve` | Extract direct download URLs                | `python gofile-downloader.py resolve <url> --json` |
| `upload` | Upload file to gofile.io with progress      | `python gofile-downloader.py upload <file> --description <desc>` |

### Detailed Usage

#### Mirror (Multi-threaded Downloader)

Downloads entire folders recursively, supporting passwords and batch files.

```bash
python gofile-downloader.py mirror https://gofile.io/d/CONTENT_ID --password PASSWORD
```

- Accepts a single link or a text file with one link per line.
- Passwords can be global (`--password`) or per-line in the file (link + space + password).
- Configurable via environment variables (see below).

#### Download (Single File)

Fetches one file from a folder, with progress bar.

```bash
python gofile-downloader.py download https://gofile.io/d/CONTENT_ID --dest ./downloads/ --file-id FILE_ID --overwrite
```

- `--file-id`: Specify which file if multiple exist.
- `--dest`: Target directory or file path.
- `--password`: For protected content.
- `--overwrite`: Overwrite existing files.

#### Resolve (Direct Links)

Outputs direct URLs for all files in a folder.

```bash
python gofile-downloader.py resolve https://gofile.io/d/CONTENT_ID --json --no-recursive
```

- `--json`: Output as JSON array.
- `--no-recursive`: Stop at the first folder level.
- `--password`: For protected content.

#### Upload

Uploads a file to gofile.io, with progress bar.

```bash
python gofile-downloader.py upload ./file.pdf --folder-id FOLDER_ID --description "Description"
```

- `--folder-id`: Upload into a specific folder.
- `--description`: Add a description to the file.

### Legacy Usage

The original single-command behavior is preserved:

```bash
python gofile-downloader.py https://gofile.io/d/CONTENT_ID [password]
```

This mirrors the folder as before.

## Differences from Upstream (`ltsdw/gofile-downloader`)

This fork enhances [ltsdw/gofile-downloader](https://github.com/ltsdw/gofile-downloader) with modern CLI ergonomics:

- **Subcommand Structure**: Organized into `mirror`, `download`, `resolve`, and `upload` for clarity.
- **Direct-Link Resolution**: Dedicated command for extracting URLs, unlike upstream's mirroring focus.
- **Single-File Operations**: New download command with progress and overwrite handling.
- **Upload Functionality**: Added file upload with progress, folder targeting, and descriptions.
- **Token Management**: Streamlined token discovery from multiple sources.
- **API Modernization**: Updated to use current gofile API endpoints (`/contents/{id}`) for reliability.
- **Progress Bars**: Added real-time indicators for downloads and uploads.
- **Documentation**: Comprehensive README with examples, tables, and comparisons.

## Environment Variables

These fine-tune the legacy `mirror` command:

| Variable                  | Description                          | Windows Example                          | Unix Example                              |
|---------------------------|--------------------------------------|------------------------------------------|------------------------------------------|
| `GF_DOWNLOAD_DIR`        | Target directory (must exist)        | `set GF_DOWNLOAD_DIR="C:\path\to\dir"`   | `GF_DOWNLOAD_DIR="/path/to/dir"`         |
| `GF_USERAGENT`           | Custom User-Agent                    | `set GF_USERAGENT="custom agent"`        | `GF_USERAGENT="custom agent"`            |
| `GF_TOKEN`               | API token                            | `set GF_TOKEN="token"`                   | `GF_TOKEN="token"`                       |
| `GF_INTERACTIVE`         | Enable file selection (`1` to enable)| `set GF_INTERACTIVE="1"`                 | `GF_INTERACTIVE="1"`                     |
| `GF_MAX_CONCURRENT_DOWNLOADS` | Max parallel downloads          | `set GF_MAX_CONCURRENT_DOWNLOADS="5"`    | `GF_MAX_CONCURRENT_DOWNLOADS="5"`        |
| `GF_MAX_RETRIES`         | Retry attempts on timeout           | `set GF_MAX_RETRIES="5"`                 | `GF_MAX_RETRIES="5"`                     |
| `GF_TIMEOUT`             | Connection timeout (seconds)        | `set GF_TIMEOUT="15.0"`                  | `GF_TIMEOUT="15.0"`                      |
| `GF_CHUNK_SIZE`          | Download chunk size (bytes)         | `set GF_CHUNK_SIZE="2097152"`            | `GF_CHUNK_SIZE="2097152"`                |

## Contributing

Contributions are welcome! Please open issues or pull requests on GitHub.

## License

This project is licensed under the GNU General Public License v3.0 - see the [LICENSE](LICENSE) file for details.
