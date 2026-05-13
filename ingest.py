#!/usr/bin/env python3
"""
ingest.py - Bulk PDF → Markdown → Open WebUI knowledge base ingestion.

Usage:
    # First run: convert + upload all PDFs in raw/
    python3 ingest.py

    # Convert only (no upload)
    python3 ingest.py --convert-only

    # Upload only (already converted)
    python3 ingest.py --upload-only

    # Reset progress for a specific file
    python3 ingest.py --reset path/to/file.pdf

    # Show status
    python3 ingest.py --status

Requirements:
    pip install markitdown requests
"""

import os
import sys
import time
import json
import sqlite3
import shutil
import hashlib
import argparse
import logging
from pathlib import Path
from datetime import datetime
import requests

# -- Config --------------------------------------------------------------------
BASE_DIR        = Path(__file__).parent
RAW_DIR         = BASE_DIR / "raw"
MD_DIR          = BASE_DIR / "markdown"
PROCESSED_DIR   = BASE_DIR / "processed"
FAILED_DIR      = BASE_DIR / "failed"
DB_PATH         = BASE_DIR / "progress.db"
LOG_FILE        = BASE_DIR / "ingest.log"

OPENWEBUI_URL   = "http://127.0.0.1:3000"
API_KEY         = os.getenv("OPENWEBUI_API_KEY", "YOUR_API_KEY_HERE")
KNOWLEDGE_NAME  = "FSO Wissensdatenbank"
KNOWLEDGE_DESC  = "Interne Dokumentation und Verfahrensanweisungen"

# Tuning
BATCH_SIZE      = 20      # files per upload batch
CONVERT_WORKERS = 4       # parallel conversion threads
UPLOAD_DELAY    = 0.5     # seconds between uploads (avoid overwhelming API)
MAX_FILE_MB     = 50      # skip files larger than this

# -- Logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)


# -- Database ------------------------------------------------------------------
def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path        TEXT PRIMARY KEY,
            hash        TEXT,
            status      TEXT DEFAULT 'pending',
            md_path     TEXT,
            doc_id      TEXT,
            error       TEXT,
            converted   TEXT,
            uploaded    TEXT
        );
        CREATE TABLE IF NOT EXISTS meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    conn.commit()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def file_hash(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes()[:65536])  # first 64KB is enough
    return h.hexdigest()


def scan_files(conn):
    """Discover new PDFs in raw/ and register them."""
    known = {r["path"] for r in conn.execute("SELECT path FROM files")}
    new_count = 0
    for pdf in sorted(RAW_DIR.rglob("*.pdf")):
        rel = str(pdf.relative_to(BASE_DIR))
        if rel not in known:
            size_mb = pdf.stat().st_size / 1024 / 1024
            if size_mb > MAX_FILE_MB:
                log.warning(f"Skipping large file ({size_mb:.1f} MB): {pdf.name}")
                conn.execute(
                    "INSERT OR IGNORE INTO files (path, status, error) VALUES (?,?,?)",
                    (rel, "skipped", f"File too large: {size_mb:.1f} MB")
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO files (path, hash) VALUES (?,?)",
                    (rel, file_hash(pdf))
                )
            new_count += 1
    conn.commit()
    return new_count


def get_status(conn) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) as n FROM files GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# -- Conversion ----------------------------------------------------------------
def convert_pdf(pdf_path: Path, md_path: Path) -> str:
    """Convert PDF to Markdown. Returns markdown text."""
    try:
        from markitdown import MarkItDown
        md = MarkItDown()
        result = md.convert(str(pdf_path))
        text = result.text_content or ""
    except ImportError:
        raise RuntimeError("markitdown not installed: pip install markitdown")
    except Exception as e:
        raise RuntimeError(f"markitdown failed: {e}")

    if not text.strip():
        raise RuntimeError("Empty output - PDF may be scanned/image-only")

    # Clean up common PDF extraction noise
    lines = []
    prev_blank = False
    for line in text.splitlines():
        line = line.rstrip()
        # Skip lines that are just page numbers or headers
        if line.strip().isdigit():
            continue
        # Collapse multiple blank lines
        if not line.strip():
            if not prev_blank:
                lines.append("")
            prev_blank = True
        else:
            lines.append(line)
            prev_blank = False

    cleaned = "\n".join(lines).strip()

    # Add filename as title if no heading present
    if not cleaned.startswith("#"):
        title = pdf_path.stem.replace("_", " ").replace("-", " ")
        cleaned = f"# {title}\n\n{cleaned}"

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(cleaned, encoding="utf-8")
    return cleaned


def run_conversion(conn, workers: int = CONVERT_WORKERS):
    """Convert all pending PDFs to Markdown."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pending = conn.execute(
        "SELECT path FROM files WHERE status='pending'"
    ).fetchall()

    if not pending:
        log.info("No files pending conversion.")
        return

    log.info(f"Converting {len(pending)} files with {workers} workers...")

    def _convert(row):
        rel = row["path"]
        pdf_path = BASE_DIR / rel
        md_rel = "markdown" / Path(rel).relative_to("raw").with_suffix(".md")
        md_path = BASE_DIR / md_rel

        try:
            convert_pdf(pdf_path, md_path)
            return rel, str(md_rel), None
        except Exception as e:
            return rel, None, str(e)

    done = failed = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_convert, row): row for row in pending}
        for future in as_completed(futures):
            rel, md_rel, error = future.result()
            if error:
                log.warning(f"FAILED {Path(rel).name}: {error}")
                conn.execute(
                    "UPDATE files SET status='failed', error=? WHERE path=?",
                    (error, rel)
                )
                # Move to failed/
                src = BASE_DIR / rel
                dst = FAILED_DIR / Path(rel).relative_to("raw")
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
                failed += 1
            else:
                conn.execute(
                    "UPDATE files SET status='converted', md_path=?, converted=? WHERE path=?",
                    (md_rel, datetime.utcnow().isoformat(), rel)
                )
                done += 1

            if (done + failed) % 100 == 0:
                conn.commit()
                log.info(f"  Progress: {done} converted, {failed} failed")

    conn.commit()
    log.info(f"Conversion complete: {done} succeeded, {failed} failed")


# -- Open WebUI API ------------------------------------------------------------
class OpenWebUIClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict:
        r = requests.get(f"{self.base}{path}", headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, **kwargs) -> dict:
        r = requests.post(f"{self.base}{path}", headers=self.headers,
                          timeout=60, **kwargs)
        r.raise_for_status()
        return r.json()

    def _delete(self, path: str) -> dict:
        r = requests.delete(f"{self.base}{path}", headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def get_or_create_knowledge(self, name: str, description: str) -> str:
        """Return knowledge base ID, creating it if needed."""
        try:
            items = self._get("/api/v1/knowledge/")
            for item in items:
                if item.get("name") == name:
                    log.info(f"Found existing knowledge base: {name} ({item['id']})")
                    return item["id"]
        except Exception:
            pass

        result = self._post("/api/v1/knowledge/create", json={
            "name": name,
            "description": description,
        })
        kb_id = result["id"]
        log.info(f"Created knowledge base: {name} ({kb_id})")
        return kb_id

    def upload_file(self, md_path: Path) -> str:
        """Upload a markdown file, return file ID."""
        headers = {"Authorization": f"Bearer {API_KEY}"}
        with open(md_path, "rb") as f:
            r = requests.post(
                f"{self.base}/api/v1/files/",
                headers=headers,
                files={"file": (md_path.name, f, "text/markdown")},
                timeout=60,
            )
        r.raise_for_status()
        return r.json()["id"]

    def add_file_to_knowledge(self, kb_id: str, file_id: str):
        self._post(f"/api/v1/knowledge/{kb_id}/file/add",
                   json={"file_id": file_id})


# -- Upload --------------------------------------------------------------------
def run_upload(conn, client: OpenWebUIClient):
    kb_id = client.get_or_create_knowledge(KNOWLEDGE_NAME, KNOWLEDGE_DESC)
    conn.execute(
        "INSERT OR REPLACE INTO meta VALUES ('kb_id', ?)", (kb_id,)
    )
    conn.commit()

    pending = conn.execute(
        "SELECT path, md_path FROM files WHERE status='converted'"
    ).fetchall()

    if not pending:
        log.info("No files pending upload.")
        return

    log.info(f"Uploading {len(pending)} files to knowledge base...")
    done = failed = 0

    for i, row in enumerate(pending):
        md_path = BASE_DIR / row["md_path"]
        if not md_path.exists():
            log.warning(f"Markdown file missing: {md_path}")
            continue

        try:
            file_id = client.upload_file(md_path)
            client.add_file_to_knowledge(kb_id, file_id)
            conn.execute(
                "UPDATE files SET status='uploaded', doc_id=?, uploaded=? WHERE path=?",
                (file_id, datetime.utcnow().isoformat(), row["path"])
            )
            done += 1

            # Move original PDF to processed/
            src = BASE_DIR / row["path"]
            dst = PROCESSED_DIR / Path(row["path"]).relative_to("raw")
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(src), dst)
            except Exception:
                pass

        except Exception as e:
            log.warning(f"Upload failed {Path(row['path']).name}: {e}")
            conn.execute(
                "UPDATE files SET status='upload_failed', error=? WHERE path=?",
                (str(e), row["path"])
            )
            failed += 1

        if (done + failed) % BATCH_SIZE == 0:
            conn.commit()
            log.info(f"  Uploaded: {done}, Failed: {failed}")
            time.sleep(UPLOAD_DELAY)

    conn.commit()
    log.info(f"Upload complete: {done} succeeded, {failed} failed")


# -- CLI -----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="PDF → Open WebUI ingestion")
    parser.add_argument("--convert-only", action="store_true")
    parser.add_argument("--upload-only",  action="store_true")
    parser.add_argument("--reset",        metavar="PATH", help="Reset file status to pending")
    parser.add_argument("--retry-failed", action="store_true", help="Retry failed files")
    parser.add_argument("--status",       action="store_true")
    parser.add_argument("--workers",      type=int, default=CONVERT_WORKERS)
    args = parser.parse_args()

    # Create dirs
    for d in (RAW_DIR, MD_DIR, PROCESSED_DIR, FAILED_DIR):
        d.mkdir(parents=True, exist_ok=True)

    conn = get_conn()

    if args.status:
        status = get_status(conn)
        total = sum(status.values())
        print(f"\nStatus ({total} total files):")
        for k, v in sorted(status.items()):
            print(f"  {k:20s} {v:6d}")
        print()
        return

    if args.reset:
        conn.execute(
            "UPDATE files SET status='pending', error=NULL WHERE path LIKE ?",
            (f"%{args.reset}%",)
        )
        conn.commit()
        log.info(f"Reset: {args.reset}")
        return

    if args.retry_failed:
        conn.execute(
            "UPDATE files SET status='pending', error=NULL "
            "WHERE status IN ('failed', 'upload_failed')"
        )
        conn.commit()
        log.info("Reset all failed files to pending")

    # Scan for new files
    new = scan_files(conn)
    if new:
        log.info(f"Found {new} new files")

    status = get_status(conn)
    log.info(f"Status: {dict(status)}")

    if not args.upload_only:
        run_conversion(conn, workers=args.workers)

    if not args.convert_only:
        if API_KEY == "YOUR_API_KEY_HERE":
            log.error("Set OPENWEBUI_API_KEY env var or edit API_KEY in script")
            sys.exit(1)
        client = OpenWebUIClient(OPENWEBUI_URL, API_KEY)
        run_upload(conn, client)

    log.info("Done.")


if __name__ == "__main__":
    main()
