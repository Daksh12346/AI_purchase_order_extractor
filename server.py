import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import base64
from datetime import datetime, timedelta
from typing import Optional, List, Set
import os
import json

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="AI Purchase Order Extractor System")

# CORS setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROCESSED_FILE = "processed_po.json"
MAX_FETCH_EMAILS = 1000000000
MAX_FILE_SIZE_MB = 25


def get_processed_pos() -> Set[str]:
    if os.path.exists(PROCESSED_FILE):
        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("po_numbers", []))
        except Exception:
            return set()
    return set()


def save_processed_pos(new_pos: List[str]):
    existing = get_processed_pos()
    for po in new_pos:
        if po and str(po).strip():
            existing.add(str(po).strip().upper())
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump({"po_numbers": list(existing)}, f, indent=2)


class GmailFetchRequest(BaseModel):
    email: str
    password: str
    filter_type: str = "PO_ONLY"
    date_from: Optional[str] = ""
    date_to: Optional[str] = ""
    query: Optional[str] = ""
    force_reprocess: Optional[bool] = False


class MarkProcessedRequest(BaseModel):
    po_numbers: List[str]


def decode_mime_words(s: str) -> str:
    if not s:
        return ""
    decoded_fragments = decode_header(s)
    output = []
    for fragment, encoding in decoded_fragments:
        if isinstance(fragment, bytes):
            try:
                output.append(fragment.decode(encoding or "utf-8", errors="ignore"))
            except Exception:
                output.append(fragment.decode("latin1", errors="ignore"))
        else:
            output.append(str(fragment))
    return "".join(output)


def sync_fetch_po_pdfs(req: GmailFetchRequest) -> dict:
    user_email = req.email.strip()
    app_password = req.password.strip().replace(" ", "")

    if not user_email or not app_password:
        raise HTTPException(
            status_code=400, detail="Gmail address and 16-character App Password are required."
        )

    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(user_email, app_password)
        mail.select("inbox")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Gmail Login Failed: {str(e)}")

    try:
        gmail_query_parts = ["filename:pdf", "has:attachment"]

        if req.date_from and req.date_from.strip():
            d_from = req.date_from.strip().replace("-", "/")
            gmail_query_parts.append(f"after:{d_from}")

        if req.date_to and req.date_to.strip():
            try:
                dt_to = datetime.strptime(req.date_to.strip(), "%Y-%m-%d") + timedelta(days=1)
                d_to_str = dt_to.strftime("%Y/%m/%d")
                gmail_query_parts.append(f"before:{d_to_str}")
            except ValueError:
                pass

        if req.query and req.query.strip():
            raw_q = req.query.strip().replace('"', '').replace("'", "")
            gmail_query_parts.append(f'({raw_q})')

        full_raw_query = " ".join(gmail_query_parts)
        status, message_numbers = mail.uid("search", None, 'X-GM-RAW', f'"{full_raw_query}"')

        if status != "OK" or not message_numbers[0]:
            return {"files": [], "message": "No emails found matching criteria."}

        email_uids = message_numbers[0].split()
        recent_uids = email_uids[-MAX_FETCH_EMAILS:]
        recent_uids.reverse()

        extracted_files = []

        for uid in recent_uids:
            res, msg_data = mail.uid("fetch", uid, "(RFC822)")
            if res != "OK":
                continue

            for response_part in msg_data:
                if not isinstance(response_part, tuple):
                    continue

                msg = email.message_from_bytes(response_part[1])

                email_date_str = ""
                raw_date_hdr = msg.get("Date")
                if raw_date_hdr:
                    try:
                        parsed_dt = parsedate_to_datetime(raw_date_hdr)
                        email_date_str = parsed_dt.strftime("%Y-%m-%d")
                    except Exception:
                        email_date_str = ""

                for part in msg.walk():
                    if (
                        part.get_content_maintype() == "multipart"
                        or part.get("Content-Disposition") is None
                    ):
                        continue

                    filename = part.get_filename()
                    if not filename:
                        continue

                    filename = decode_mime_words(filename).strip()

                    if filename.lower().endswith(".pdf"):
                        payload = part.get_payload(decode=True)
                        if payload:
                            if len(payload) > MAX_FILE_SIZE_MB * 1024 * 1024:
                                continue

                            b64_str = base64.b64encode(payload).decode("utf-8")
                            extracted_files.append({
                                "filename": filename,
                                "base64": b64_str,
                                "email_date": email_date_str,
                            })

        saved_pos = [] if req.force_reprocess else list(get_processed_pos())

        return {
            "files": extracted_files,
            "total_found": len(extracted_files),
            "previously_processed_pos": saved_pos,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading Gmail: {str(e)}")
    finally:
        if mail:
            try:
                mail.logout()
            except Exception:
                pass


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    for target in ["index.html", "odd.html"]:
        if os.path.exists(target):
            with open(target, "r", encoding="utf-8") as f:
                return f.read()
    return "<h1>HTML file not found in current directory!</h1>"


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/processed-pos")
def get_existing_pos():
    return {"processed": list(get_processed_pos())}


@app.post("/clear-history")
def clear_history():
    if os.path.exists(PROCESSED_FILE):
        os.remove(PROCESSED_FILE)
    return {"status": "cleared"}


@app.post("/mark-processed")
def mark_pos_as_processed(req: MarkProcessedRequest):
    save_processed_pos(req.po_numbers)
    return {"status": "success", "total_saved": len(req.po_numbers)}


@app.post("/fetch-po-pdfs")
async def fetch_po_pdfs(req: GmailFetchRequest):
    return await run_in_threadpool(sync_fetch_po_pdfs, req)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=False)
