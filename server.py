import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import base64
from datetime import datetime, timedelta
from typing import Optional, List, Set, Any, Dict
import os
import json
import re

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
import uvicorn
import requests

app = FastAPI(title="AI Purchase Order Extractor System")

# CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROCESSED_FILE = "processed_po.json"
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


class ExtractPdfRequest(BaseModel):
    api_key: str
    content_type: str  # "text" or "image"
    content: str


def sanitize_header_str(val: Any) -> str:
    if not val:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="ignore")
    return str(val)


def decode_mime_words(s: str) -> str:
    if not s:
        return ""
    try:
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
        res = "".join(output).strip()
        # Clean control characters
        return re.sub(r"[\r\n\t]+", " ", res)
    except Exception:
        return str(s)


def sync_fetch_po_pdfs(req: GmailFetchRequest) -> dict:
    user_email = req.email.strip()
    app_password = req.password.strip().replace(" ", "")

    if not user_email or not app_password:
        raise HTTPException(
            status_code=400, detail="Gmail address and 16-digit App Password are required."
        )

    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(user_email, app_password)
        mail.select("inbox")
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Gmail Login Failed: {str(e)}")

    try:
        gmail_query_parts = ["has:attachment", "filename:pdf"]

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
            raw_q = re.sub(r'["\']', '', req.query.strip())
            if raw_q:
                gmail_query_parts.append(raw_q)

        full_raw_query = " ".join(gmail_query_parts)

        status, message_numbers = mail.uid("search", None, f'X-GM-RAW "{full_raw_query}"')

        if status != "OK" or not message_numbers or not message_numbers[0]:
            # Fallback search without custom keyword if query failed
            status, message_numbers = mail.uid("search", None, 'X-GM-RAW "has:attachment filename:pdf"')

        if status != "OK" or not message_numbers or not message_numbers[0]:
            return {"files": [], "total_found": 0, "previously_processed_pos": []}

        email_uids = message_numbers[0].split()
        email_uids.reverse()

        # Limit batch size to prevent server timeout
        email_uids = email_uids[:50]

        extracted_files = []
        max_bytes = MAX_FILE_SIZE_MB * 1024 * 1024

        for uid in email_uids:
            res, msg_data = mail.uid("fetch", uid, "(RFC822)")
            if res != "OK" or not msg_data:
                continue

            for response_part in msg_data:
                if not isinstance(response_part, tuple) or len(response_part) < 2:
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
                    content_disposition = sanitize_header_str(part.get("Content-Disposition", ""))
                    content_type = sanitize_header_str(part.get_content_type()).lower()

                    filename = part.get_filename()
                    if not filename:
                        filename = part.get_param("name")

                    if not filename and "attachment" not in content_disposition.lower() and content_type != "application/pdf":
                        continue

                    if filename:
                        filename = decode_mime_words(filename)
                    else:
                        filename = f"Attachment_{len(extracted_files)+1}.pdf"

                    # Verify actual PDF extension
                    if not filename.lower().endswith(".pdf"):
                        continue

                    payload = part.get_payload(decode=True)
                    if not payload or len(payload) == 0:
                        continue

                    if len(payload) > max_bytes:
                        continue

                    # Safe base64 conversion
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
                mail.close()
            except Exception:
                pass
            try:
                mail.logout()
            except Exception:
                pass


@app.post("/extract-openai")
def extract_openai_backend(req: ExtractPdfRequest):
    """Secure, server-side OpenAI call that prevents browser CORS and timeout errors."""
    clean_key = req.api_key.strip().replace('"', '').replace("'", "")
    if not clean_key:
        raise HTTPException(status_code=400, detail="OpenAI API Key is required.")

    system_prompt = (
        "You are an ERP Purchase Order Data Extractor. Extract procurement records into valid JSON.\n"
        "RULES:\n"
        "1. Identify documents that are Purchase Orders, Indents, Supply Contracts, or Work Orders.\n"
        "2. Mark isPurchaseOrder: false ONLY if strictly an invoice, bill, or transport receipt.\n"
        "3. Extract client/buyer organization name, address, PO number, PO date, and line items.\n"
        "4. Return strict JSON format with keys: isPurchaseOrder, buyerCompanyName, buyerBillingAddress, poNumber, poDate, items."
    )

    user_contents: List[Dict[str, Any]] = []
    if req.content_type == "image":
        user_contents.append({"type": "text", "text": "Extract all PO line items from this scanned document image in JSON format."})
        user_contents.append({"type": "image_url", "image_url": {"url": req.content}})
    else:
        user_contents.append({"type": "text", "text": f"Extract all PO line items from this document text in JSON format:\n\n{req.content}"})

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {clean_key}"
    }

    body = {
        "model": "gpt-4o-mini",
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_contents}
        ]
    }

    try:
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=60)
        if resp.status_code != 200:
            err_data = resp.json().get("error", {}) if resp.text else {}
            msg = err_data.get("message", f"OpenAI HTTP {resp.status_code}")
            raise HTTPException(status_code=resp.status_code, detail=msg)
        return resp.json()
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="OpenAI request timed out. Please try again.")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Failed to communicate with OpenAI: {str(e)}")


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    for target in ["index.html", "odd.html"]:
        if os.path.exists(target):
            with open(target, "r", encoding="utf-8") as f:
                return f.read()
    return "<h1>Place index.html in the same directory as this script.</h1>"


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
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)
