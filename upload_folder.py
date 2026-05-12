#!/usr/bin/env python3
"""
CrossPoint Reader - Folder Upload Script
Uploads an entire local folder (recursively) to the CrossPoint Reader SD card.

Uses:
  - POST /mkdir  to create directories
  - WebSocket :81 for fast binary file uploads

Usage:
    python3 scripts/upload_folder.py <local_folder> [--host <host>] [--dest <sd_path>]

Examples:
    python3 scripts/upload_folder.py ~/Books
    python3 scripts/upload_folder.py ~/Books --host 192.168.1.42
    python3 scripts/upload_folder.py ~/Books --dest /MyBooks --host crosspoint.local
    python3 scripts/upload_folder.py ~/Books --all             # upload ALL file types
    python3 scripts/upload_folder.py ~/Books --convert-pdf     # convert PDFs → EPUB before upload
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
try:
    import requests
except ImportError:
    print("ERROR: 'requests' is not installed. Run: pip3 install requests")
    sys.exit(1)

try:
    import websocket  # websocket-client
except ImportError:
    print("ERROR: 'websocket-client' is not installed. Run: pip3 install websocket-client")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_HOST = "crosspoint.local"
DEFAULT_DEST = "/"
CHUNK_SIZE = 4 * 1024   # 4 KB – matches the ESP32-C3 server's internal write buffer
INTER_CHUNK_DELAY = 0.0 # seconds between chunks (increase to 0.01 if still dropping)
WS_TIMEOUT = 30         # seconds to wait for server response
HTTP_TIMEOUT = 10       # seconds for mkdir requests
RETRY_COUNT = 3         # retries per file
RETRY_DELAY = 3.0       # seconds between retries (give device time to recover)
INTER_FILE_DELAY = 1.0  # seconds between successful uploads (let device breathe)
PDF_WORKERS = os.cpu_count() or 4  # parallel Calibre processes for PDF conversion

# CrossPoint supported formats (EPUB + TXT only)
SUPPORTED_EXTENSIONS = {".epub", ".txt"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def sd_join(parent: str, name: str) -> str:
    """Join SD card path components, always using forward slashes."""
    parent = parent.rstrip("/")
    return f"{parent}/{name}" if parent else f"/{name}"


def is_hidden(path: Path) -> bool:
    """Return True if any component of the path starts with a dot."""
    return any(part.startswith(".") for part in path.parts)


# ---------------------------------------------------------------------------
# PDF → EPUB conversion via Calibre
# ---------------------------------------------------------------------------

def find_ebook_convert() -> str | None:
    """Locate Calibre's ebook-convert binary."""
    # Common macOS Calibre install location
    candidates = [
        "/Applications/calibre.app/Contents/MacOS/ebook-convert",
        shutil.which("ebook-convert"),
    ]
    for c in candidates:
        if c and Path(c).is_file():
            return c
    return None


def convert_pdf_to_epub(pdf_path: Path, out_dir: Path, ebook_convert: str) -> Path | None:
    """
    Convert a PDF to EPUB using Calibre's ebook-convert.
    Returns the path to the generated EPUB, or None on failure.
    """
    epub_path = out_dir / (pdf_path.stem + ".epub")
    try:
        result = subprocess.run(
            [ebook_convert, str(pdf_path), str(epub_path)],
            capture_output=True,
            text=True,
            timeout=120,  # large PDFs can take a while
        )
        if result.returncode == 0 and epub_path.exists():
            return epub_path
        print(f"\n  [convert] FAILED: {result.stderr.strip()[-200:]}")
        return None
    except subprocess.TimeoutExpired:
        print("\n  [convert] Timeout (>120s)")
        return None
    except Exception as exc:
        print(f"\n  [convert] Error: {exc}")
        return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def mkdir_remote(host: str, path: str, name: str) -> bool:
    """Create a directory on the device via POST /mkdir."""
    url = f"http://{host}/mkdir"
    try:
        resp = requests.post(url, data={"path": path, "name": name}, timeout=HTTP_TIMEOUT)
        if resp.status_code == 200:
            return True
        if "already exists" in resp.text:
            return True  # Already there – fine
        print(f"  [mkdir] ERROR {resp.status_code}: {resp.text.strip()}")
        return False
    except requests.RequestException as exc:
        print(f"  [mkdir] NETWORK ERROR: {exc}")
        return False


# ---------------------------------------------------------------------------
# WebSocket upload
# ---------------------------------------------------------------------------

def upload_file_ws(host: str, local_path: Path, sd_path: str) -> bool:
    """
    Upload a single file via the CrossPoint WebSocket binary protocol.

    Protocol:
      1. Client  TEXT  "START:<filename>:<size>:<directory>"
      2. Server  TEXT  "READY"
      3. Client  BINARY chunks …
      4. Server  TEXT  "PROGRESS:<recv>:<total>" (periodic)
      5. Server  TEXT  "DONE" | "ERROR:<msg>"
    """
    file_size = local_path.stat().st_size
    sd_dir = "/".join(sd_path.split("/")[:-1]) or "/"
    filename = local_path.name

    ws_url = f"ws://{host}:81/"

    result = {"ok": False, "error": ""}
    open_event = threading.Event()   # fires when WS connection is established
    ready_event = threading.Event()  # fires when server sends READY
    done_event = threading.Event()   # fires when server sends DONE or ERROR

    def on_open(ws_app):
        open_event.set()

    def on_message(ws_app, message):
        if message == "READY":
            ready_event.set()
        elif message == "DONE":
            result["ok"] = True
            done_event.set()
        elif message.startswith("ERROR:"):
            result["error"] = message[6:]
            done_event.set()
        elif message.startswith("PROGRESS:"):
            parts = message.split(":")
            if len(parts) == 3:
                recv, total = int(parts[1]), int(parts[2])
                pct = recv * 100 // total if total else 0
                bar = "#" * (pct // 5) + "-" * (20 - pct // 5)
                print(f"\r    [{bar}] {pct:3d}%  {human_size(recv)}/{human_size(total)}", end="", flush=True)

    def on_error(ws_app, error):
        result["error"] = str(error)
        open_event.set()   # unblock if still waiting
        ready_event.set()
        done_event.set()

    ws_app = websocket.WebSocketApp(
        ws_url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
    )
    ws_thread = threading.Thread(target=ws_app.run_forever, daemon=True)
    ws_thread.start()

    # Step 0: Wait for connection to open
    if not open_event.wait(timeout=WS_TIMEOUT):
        print("\n  [ws] Timeout waiting for connection")
        ws_app.close()
        return False

    if result["error"]:
        print(f"\n  [ws] Connection error: {result['error']}")
        ws_app.close()
        return False

    # Step 1: Send START
    start_msg = f"START:{filename}:{file_size}:{sd_dir}"
    try:
        ws_app.send(start_msg)
    except Exception as exc:
        print(f"\n  [ws] Send START failed: {exc}")
        ws_app.close()
        return False

    # Step 2: Wait for READY
    if not ready_event.wait(timeout=WS_TIMEOUT):
        print("\n  [ws] Timeout waiting for READY")
        ws_app.close()
        return False

    if result["error"]:
        print(f"\n  [ws] Error: {result['error']}")
        ws_app.close()
        return False

    # Step 3: Send binary chunks
    try:
        with open(local_path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                ws_app.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)
                if INTER_CHUNK_DELAY > 0:
                    time.sleep(INTER_CHUNK_DELAY)
    except Exception as exc:
        print(f"\n  [ws] File read/send error: {exc}")
        ws_app.close()
        return False

    # Step 4: Wait for DONE / ERROR
    if not done_event.wait(timeout=WS_TIMEOUT):
        print("\n  [ws] Timeout waiting for DONE")
        ws_app.close()
        return False

    ws_app.close()
    ws_thread.join(timeout=2)

    if result["ok"]:
        print()  # newline after progress bar
        return True
    else:
        print(f"\n  [ws] Upload failed: {result['error']}")
        return False


def upload_file_with_retry(host: str, local_path: Path, sd_path: str) -> bool:
    """Upload a file with automatic retries on failure."""
    for attempt in range(1, RETRY_COUNT + 1):
        if attempt > 1:
            print(f"    Retry {attempt}/{RETRY_COUNT} after {RETRY_DELAY}s…")
            time.sleep(RETRY_DELAY)
        if upload_file_ws(host, local_path, sd_path):
            return True
    return False


# ---------------------------------------------------------------------------
# Recursive upload
# ---------------------------------------------------------------------------

def upload_tree(host: str, local_root: Path, sd_root: str, upload_all: bool, ebook_convert: str | None) -> tuple[int, int, int, int]:
    """
    Recursively upload local_root → sd_root.
    Phase 1: Convert all PDFs to EPUB (if ebook_convert is set).
    Phase 2: Create directories on device.
    Phase 3: Upload all files.
    Returns (files_ok, files_failed, files_skipped, files_converted).
    """
    ok = 0
    failed = 0
    skipped = 0
    converted = 0

    all_entries = sorted(local_root.rglob("*"))
    entries = [e for e in all_entries if not is_hidden(e.relative_to(local_root))]
    files = [e for e in entries if e.is_file()]

    # ------------------------------------------------------------------
    # Phase 1: Convert PDFs → EPUB locally (no network needed)
    # ------------------------------------------------------------------
    # upload_queue: list of (local_path, sd_path)
    upload_queue: list[tuple[Path, str]] = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="crosspoint_pdf_")) if ebook_convert else None

    if ebook_convert:
        print(f"  Temp dir: {tmp_dir}")
        print()
        pdf_files = [f for f in files if f.suffix.lower() == ".pdf"]
        other_files = [f for f in files if f.suffix.lower() != ".pdf"]
        total_pdfs = len(pdf_files)
        print(f"  Phase 1/3 – Converting {total_pdfs} PDF(s) to EPUB  "
              f"(up to {PDF_WORKERS} parallel)…")
        print()

        print_lock = threading.Lock()
        results: dict[Path, Path | None] = {}

        def convert_one(entry: Path) -> tuple[Path, Path | None]:
            epub = convert_pdf_to_epub(entry, tmp_dir, ebook_convert)
            return entry, epub

        with ThreadPoolExecutor(max_workers=PDF_WORKERS) as pool:
            futures = {pool.submit(convert_one, f): f for f in pdf_files}
            for future in as_completed(futures):
                entry, epub = future.result()
                rel = entry.relative_to(local_root)
                size_str = human_size(entry.stat().st_size)
                with print_lock:
                    idx = len(results) + 1
                    if epub is None:
                        print(f"  [{idx}/{total_pdfs}] {rel}  ({size_str}) → FAILED (skipped)")
                        skipped += 1
                    else:
                        print(f"  [{idx}/{total_pdfs}] {rel}  ({size_str}) → OK  ({human_size(epub.stat().st_size)})")
                        converted += 1
                        sd_dir = sd_join(sd_root, str(rel.parent).replace(os.sep, "/")) if str(rel.parent) != "." else sd_root
                        upload_queue.append((epub, sd_join(sd_dir, epub.name)))
                    results[entry] = epub

        # Add non-PDF files to queue
        for entry in other_files:
            rel = entry.relative_to(local_root)
            ext = entry.suffix.lower()
            if not upload_all and ext not in SUPPORTED_EXTENSIONS:
                skipped += 1
                continue
            sd_path = sd_join(sd_root, str(rel).replace(os.sep, "/"))
            upload_queue.append((entry, sd_path))
    else:
        for entry in files:
            rel = entry.relative_to(local_root)
            ext = entry.suffix.lower()
            if not upload_all and ext not in SUPPORTED_EXTENSIONS:
                skipped += 1
                continue
            sd_path = sd_join(sd_root, str(rel).replace(os.sep, "/"))
            upload_queue.append((entry, sd_path))

    print()

    # ------------------------------------------------------------------
    # Phase 2: Create directories on device
    # ------------------------------------------------------------------
    print(f"  Phase 2/3 – Creating directories…")
    dirs_created: set[str] = set()
    for entry in entries:
        if entry.is_dir():
            rel = entry.relative_to(local_root)
            cur = sd_root
            for part in rel.parts:
                parent = cur
                cur = sd_join(cur, part)
                if cur not in dirs_created:
                    print(f"  [mkdir] {cur}")
                    mkdir_remote(host, parent, part)
                    dirs_created.add(cur)
    # Also ensure dirs for converted EPUBs exist (e.g. subfolders of PDFs)
    for _, sd_path in upload_queue:
        sd_dir = "/".join(sd_path.split("/")[:-1]) or "/"
        parts = [p for p in sd_dir.split("/") if p]
        cur = ""
        for part in parts:
            parent = cur or "/"
            cur = f"{cur}/{part}"
            if cur not in dirs_created:
                mkdir_remote(host, parent, part)
                dirs_created.add(cur)
    print()

    # ------------------------------------------------------------------
    # Phase 3: Upload all files
    # ------------------------------------------------------------------
    total = len(upload_queue)
    print(f"  Phase 3/3 – Uploading {total} file(s)…")
    print()

    try:
        for i, (local_path, sd_path) in enumerate(upload_queue, 1):
            size_str = human_size(local_path.stat().st_size)
            print(f"  [{i}/{total}] {local_path.name}  ({size_str})  →  {sd_path}")
            if upload_file_with_retry(host, local_path, sd_path):
                ok += 1
                time.sleep(INTER_FILE_DELAY)
            else:
                failed += 1
    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return ok, failed, skipped, converted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Upload a local folder recursively to CrossPoint Reader via Wi-Fi."
    )
    parser.add_argument("folder", help="Local folder to upload")
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"CrossPoint hostname or IP (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--dest",
        default=DEFAULT_DEST,
        help=f"Target directory on the SD card (default: {DEFAULT_DEST})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="upload_all",
        help="Upload ALL file types (default: only .epub and .txt)",
    )
    parser.add_argument(
        "--convert-pdf",
        action="store_true",
        dest="convert_pdf",
        help="Convert PDF files to EPUB via Calibre before uploading (requires Calibre)",
    )
    args = parser.parse_args()

    local_root = Path(args.folder).expanduser().resolve()
    if not local_root.is_dir():
        print(f"ERROR: '{local_root}' is not a directory.")
        sys.exit(1)

    # Normalise dest
    sd_root = args.dest.rstrip("/") or "/"
    if not sd_root.startswith("/"):
        sd_root = "/" + sd_root

    host = args.host

    print(f"CrossPoint Folder Uploader")
    print(f"  Device  : http://{host}/")
    print(f"  Source  : {local_root}")
    print(f"  Target  : {sd_root}")
    if args.upload_all:
        print(f"  Filter  : ALL file types")
    else:
        print(f"  Filter  : {', '.join(sorted(SUPPORTED_EXTENSIONS))} only  (use --all to override)")

    # Calibre check
    ebook_convert = None
    if args.convert_pdf:
        ebook_convert = find_ebook_convert()
        if ebook_convert:
            print(f"  PDF conv: enabled  ({ebook_convert})")
        else:
            print("  PDF conv: ERROR – Calibre not found!")
            print("            Install via: brew install --cask calibre")
            print("            Then re-run with --convert-pdf")
            sys.exit(1)
    else:
        print(f"  PDF conv: disabled  (use --convert-pdf to enable)")
    print()

    # Quick reachability check
    try:
        resp = requests.get(f"http://{host}/api/status", timeout=5)
        data = resp.json()
        print(f"  Connected  version={data.get('version', '?')}  freeHeap={data.get('freeHeap', '?')} B")
    except Exception as exc:
        print(f"WARNING: Could not reach device ({exc}). Proceeding anyway…")
    print()

    start = time.time()
    files_ok, files_failed, files_skipped, files_converted = upload_tree(
        host, local_root, sd_root, args.upload_all, ebook_convert
    )
    elapsed = time.time() - start

    print()
    print("=" * 50)
    print(f"Done in {elapsed:.1f}s")
    print(f"  Uploaded  : {files_ok} file(s)")
    if files_converted:
        print(f"  Converted : {files_converted} PDF(s) → EPUB")
    print(f"  Skipped   : {files_skipped} file(s)  (wrong format / hidden)")
    if files_failed:
        print(f"  Failed    : {files_failed} file(s)")
        sys.exit(1)


if __name__ == "__main__":
    main()

