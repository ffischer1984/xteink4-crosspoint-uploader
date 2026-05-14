# xteink4-crosspoint-uploader

Python script to recursively upload a local folder of pdf/epub-files to a [Xteink 4](https://www.xteink.com/products/xteink-x4) / [CrossPoint Reader](https://github.com/crosspoint-reader/crosspoint-reader)-device over Wi-Fi.
The Conversion of pdf to epub uses multithreading otherwise it takes to long. Maybe you have to playaround with this line ```PDF_WORKERS = os.cpu_count() or 4  # parallel Calibre processes for PDF conversion```in the uploay_folder.py

The script:
- creates directories on the device via HTTP (`POST /mkdir`)
- uploads files via WebSocket (`ws://<host>:81/`)
- can optionally convert PDFs to EPUB before upload (Calibre)

## Compatibility note

This uploader should also work with other [xteink devices](https://www.xteink.com/) and may also work with the xteink firmware.
However, this has not been tested yet.

## 1) Setup (starting with `.venv`)

Run from the project root directory:

```zsh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Optional for PDF conversion (`--convert-pdf`):

```zsh
brew install --cask calibre
```

## 2) Prerequisites

- CrossPoint Reader is reachable on the same network
- Hostname or IP is known (for example `crosspoint.local` or `192.168.x.x`)
- Target path on the SD card exists or may be created

## 3) Usage

Basic syntax:

```zsh
python upload_folder.py <local_folder> [--host <host>] [--dest <sd_path>] [--all] [--convert-pdf]
```

Examples:

```zsh
# Default: upload only .epub and .txt to /
python upload_folder.py ~/Books

# With explicit IP
python upload_folder.py ~/Books --host 192.168.1.42

# Upload into a subfolder on the SD card
python upload_folder.py ~/Books --dest /MyBooks --host crosspoint.local

# Upload all file types
python upload_folder.py ~/Books --all

# Convert PDFs to EPUB before upload
python upload_folder.py ~/Books --convert-pdf
```

## 4) Important options

- `--host`: reader hostname or IP (default: `crosspoint.local`)
- `--dest`: target directory on the SD card (default: `/`)
- `--all`: upload all file types (otherwise only `.epub` and `.txt`)
- `--convert-pdf`: convert PDF -> EPUB using Calibre before upload

## 5) Output and errors

The script shows:
- connection info for the reader (`/api/status`)
- progress per file
- final stats (`Uploaded`, `Converted`, `Skipped`, `Failed`)

If `Failed > 0`, the script exits with code `1`.

Successful example output (`--convert-pdf`):

```text
CrossPoint Folder Uploader
  Device  : http://crosspoint.local/
  Source  : ./books
  Target  : /MyBooks
  Filter  : .epub, .txt only  (use --all to override)
  PDF conv: enabled  (/Applications/calibre.app/Contents/MacOS/ebook-convert)

  Connected  version=1.2.0  freeHeap=214352 B

  Temp dir: /var/folders/.../crosspoint_pdf_abcd1234

  Phase 1/3 - Converting 2 PDF(s) to EPUB  (up to 8 parallel)...

  [1/2] Fiction/dune.pdf  (4.9 MB) -> OK  (2.1 MB)
  [2/2] Fiction/foundation.pdf  (4.2 MB) -> OK  (1.8 MB)

  Phase 2/3 - Creating directories...
  [mkdir] /MyBooks
  [mkdir] /MyBooks/Fiction

  Phase 3/3 - Uploading 3 file(s)...

  [1/3] dune.epub  (2.1 MB)  ->  /MyBooks/Fiction/dune.epub
    [####################] 100%  2.1 MB/2.1 MB
  [2/3] foundation.epub  (1.8 MB)  ->  /MyBooks/Fiction/foundation.epub
    [####################] 100%  1.8 MB/1.8 MB
  [3/3] notes.txt  (12.4 KB)  ->  /MyBooks/notes.txt
    [####################] 100%  12.4 KB/12.4 KB

==================================================
Done in 12.8s
  Uploaded  : 3 file(s)
  Converted : 2 PDF(s) -> EPUB
  Skipped   : 0 file(s)  (wrong format / hidden)
```

## 6) Troubleshooting

- `Could not reach device`:
  - verify host/IP
  - ensure reader and computer are on the same Wi-Fi
  - open reader in browser: `http://<host>/`

- `websocket-client` or `requests` missing:
  - activate the venv and reinstall:

```zsh
source .venv/bin/activate
pip install -r requirements.txt
```

- PDF conversion not available:
  - install Calibre (`brew install --cask calibre`)
  - rerun with `--convert-pdf`

## 7) Deactivate venv

```zsh
deactivate
```
## Sponsor me
# Donations
[![paypal](https://www.paypalobjects.com/en_US/DK/i/btn/btn_donateCC_LG.gif)](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=EN22Z95HKGD74&source=url) 
[![buymeacoffee](https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png)](https://buymeacoffee.com/CWkjUYH)



