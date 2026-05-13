# ingest.py - PDF to Open WebUI Knowledge Base

Converts PDFs to Markdown and uploads them to an Open WebUI knowledge base.
Fully resumable - safe to stop and restart at any time.

## Directory structure

```
/opt/knowledge/
    raw/          <- drop PDFs here
    markdown/     <- converted .md files (auto-created)
    processed/    <- originals moved here after successful upload
    failed/       <- originals moved here if conversion fails
    progress.db   <- tracks state (delete to start fresh)
    ingest.py
```

## Dependencies

```bash
python3 -m venv /opt/knowledge/.venv
/opt/knowledge/.venv/bin/pip install markitdown[pdf] requests
```

## Configuration

Edit these values at the top of `ingest.py`:

| Variable | Default | Description |
|---|---|---|
| `OPENWEBUI_URL` | `http://127.0.0.1:3000` | Open WebUI address |
| `API_KEY` | - | Set via env var (see below) |
| `KNOWLEDGE_NAME` | `Knowledgebase` | Name of the knowledge base |
| `CONVERT_WORKERS` | `4` | Parallel conversion threads |
| `BATCH_SIZE` | `20` | Files per upload batch |
| `MAX_FILE_MB` | `50` | Skip files larger than this |

## API key

Generate in Open WebUI: avatar -> Settings -> Account -> API Keys -> Create.

Pass it as an environment variable - do not hardcode it in the script:

```bash
export OPENWEBUI_API_KEY="sk-..."
```

## Usage

```bash
# Full run - convert then upload
OPENWEBUI_API_KEY="sk-..." /opt/knowledge/.venv/bin/python3 ingest.py

# Convert only (no upload)
/opt/knowledge/.venv/bin/python3 ingest.py --convert-only --workers 8

# Upload only (already converted)
OPENWEBUI_API_KEY="sk-..." /opt/knowledge/.venv/bin/python3 ingest.py --upload-only

# Check progress
/opt/knowledge/.venv/bin/python3 ingest.py --status

# Retry all failed files
/opt/knowledge/.venv/bin/python3 ingest.py --retry-failed

# Reset a specific file
/opt/knowledge/.venv/bin/python3 ingest.py --reset "filename.pdf"

# Reset everything (or just delete progress.db)
rm /opt/knowledge/progress.db
```

## Monitoring progress

```bash
watch -n 5 'sqlite3 /opt/knowledge/progress.db "SELECT status, COUNT(*) FROM files GROUP BY status"'
```

## Notes

- PDFs must be text-based (digitally created), not scanned images
- Scanned PDFs will fail with an "empty output" error - run OCR on them first
- Re-running is always safe - already converted/uploaded files are skipped
- Logs are written to `ingest.log` in the same directory
