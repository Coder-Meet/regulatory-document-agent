#!/usr/bin/env python3
"""
Regulatory Document Ingestion Agent
===================================

A single, modular, locally-run agent that:

  1. EMAIL INGESTION (IMAP)   -> reads an unread Gmail, regex-parses a Matter
                                 Number (M + 5 digits) and a Document Type.
  2. WEB AUTOMATION (Playwright) -> looks up the matter on the Nova Scotia UARB
                                 FileMaker WebDirect site, scrapes metadata and
                                 per-tab document counts, then downloads up to
                                 10 files of the requested type.
  3. FILE PROCESSING          -> zips the downloads into `attachments.zip`.
  4. AI SUMMARY (OpenRouter)  -> drafts a fixed-structure summary email with a
                                 lightweight LLM via the OpenAI SDK.
  5. EMAIL RESPONSE (SMTP)    -> replies to the original sender with the summary
                                 and the ZIP attached, then marks the email read.

Run:  python agent.py
See README.md for the required .env configuration.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
import re
import smtplib
import ssl
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Optional

from dotenv import load_dotenv

# --------------------------------------------------------------------------- #
# Configuration & logging
# --------------------------------------------------------------------------- #

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("agent")

UARB_URL = "https://uarb.novascotia.ca/fmi/webd/UARB15"

# Canonical document types and the prefix used on the website's tabs.
DOC_TYPES = ["Exhibits", "Key Documents", "Other Documents", "Transcripts", "Recordings"]

MAX_DOWNLOADS = 10


def _env(name: str, default: Optional[str] = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return val or ""


@dataclass
class Config:
    imap_host: str = _env("IMAP_HOST", "imap.gmail.com")
    imap_port: int = int(_env("IMAP_PORT", "993"))
    smtp_host: str = _env("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(_env("SMTP_PORT", "465"))
    email_address: str = _env("EMAIL_ADDRESS", required=True)
    email_password: str = _env("EMAIL_APP_PASSWORD", required=True)
    openrouter_api_key: str = _env("OPENROUTER_API_KEY", required=True)
    openrouter_base_url: str = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    openrouter_model: str = _env("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    headless: bool = _env("HEADLESS", "true").lower() != "false"
    mailbox: str = _env("MAILBOX", "INBOX")
    # Only inspect the N most recent unread emails (keeps things fast on a busy inbox).
    max_emails_to_scan: int = int(_env("MAX_EMAILS_TO_SCAN", "25"))
    # Dedicated address requests must be sent to (e.g. a Gmail "+alias"). When set,
    # ONLY emails addressed to this are treated as document requests, so the agent
    # ignores your regular mail. Leave blank to process any parseable email.
    agent_address: str = _env("AGENT_ADDRESS", "").strip()
    # How often to re-check the inbox when running in --loop mode (seconds).
    poll_interval: int = int(_env("POLL_INTERVAL_SECONDS", "60"))
    # Email size budget (MB). Gmail rejects messages over ~25 MB *after* base64
    # encoding, so the on-disk ZIP is capped lower (see handle_request) and only
    # includes as many downloaded files as fit.
    max_attachment_mb: int = int(_env("MAX_ATTACHMENT_MB", "25"))


# --------------------------------------------------------------------------- #
# Data containers
# --------------------------------------------------------------------------- #


@dataclass
class EmailRequest:
    uid: bytes
    sender: str
    subject: str
    message_id: str
    matter_number: str
    doc_type: str


@dataclass
class MatterData:
    matter_number: str = ""
    title_description: str = ""      # title without the trailing dollar amount
    amount: str = ""                 # e.g. "$69,275,000"
    matter_type: str = ""            # e.g. "Water"
    category: str = ""               # e.g. "Capital Expenditure Approvals"
    initial_filing: str = ""         # human-readable date (initial filing)
    final_filing: str = ""           # human-readable date (final filing)
    status: str = ""
    counts: dict = field(default_factory=dict)   # {doc_type: int}
    downloaded_files: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 1. EMAIL INGESTION
# --------------------------------------------------------------------------- #


def _decode(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _extract_body(msg: email.message.Message) -> str:
    """Return the best-effort plain-text body of an email message."""
    if msg.is_multipart():
        # Prefer text/plain, fall back to text/html (stripped).
        plain, html = "", ""
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/plain":
                plain += text
            elif ctype == "text/html":
                html += text
        body = plain or re.sub(r"<[^>]+>", " ", html)
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace") if payload else ""
        except Exception:
            body = msg.get_payload() or ""
    return body


def parse_request_text(body: str) -> tuple[Optional[str], Optional[str]]:
    """Deterministically extract (matter_number, doc_type) from email text."""
    matter_match = re.search(r"\bM\d{5}\b", body, re.IGNORECASE)
    matter_number = matter_match.group(0).upper() if matter_match else None

    # Match the most specific phrases first so "Key/Other Documents" win over
    # a bare "Documents", and the canonical casing is always returned.
    ordered = ["Key Documents", "Other Documents", "Exhibits", "Transcripts", "Recordings"]
    doc_type = None
    for candidate in ordered:
        if re.search(rf"\b{re.escape(candidate)}\b", body, re.IGNORECASE):
            doc_type = candidate
            break

    return matter_number, doc_type


def _recipients(msg: email.message.Message) -> str:
    """Lower-cased blob of every address an email was delivered/addressed to."""
    fields = []
    for header in ("To", "Cc", "Delivered-To", "X-Original-To", "X-Forwarded-To"):
        for value in msg.get_all(header, []):
            fields.append(_decode(value))
    return " ".join(fields).lower()


def find_requests(cfg: Config) -> list:
    """Return parseable unread document requests (newest first).

    If ``cfg.agent_address`` is set, only emails addressed to that address are
    considered requests, so the agent ignores your regular mail.
    """
    log.info("Connecting to IMAP %s:%s", cfg.imap_host, cfg.imap_port)
    imap = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    requests: list = []
    try:
        imap.login(cfg.email_address, cfg.email_password)
        imap.select(cfg.mailbox)

        # Narrow the server-side search to unread mail addressed to the agent.
        if cfg.agent_address:
            try:
                status, data = imap.search(
                    None, "UNSEEN", "TO", f'"{cfg.agent_address}"'
                )
            except Exception:
                status, data = imap.search(None, "UNSEEN")
        else:
            status, data = imap.search(None, "UNSEEN")

        if status != "OK":
            log.warning("IMAP search failed: %s", status)
            return requests
        uids = data[0].split()
        if not uids:
            log.info("No matching unread emails.")
            return requests

        recent = list(reversed(uids))[: max(1, cfg.max_emails_to_scan)]
        log.info("Found %d candidate unread email(s); inspecting the %d most recent.",
                 len(uids), len(recent))

        for uid in recent:
            # BODY.PEEK avoids implicitly setting the \Seen flag.
            status, msg_data = imap.fetch(uid, "(BODY.PEEK[])")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode(msg.get("Subject"))
            sender = parseaddr(msg.get("From"))[1]

            # Defence in depth: confirm the agent address really is a recipient.
            if cfg.agent_address and cfg.agent_address.lower() not in _recipients(msg):
                continue

            body = _extract_body(msg)
            matter_number, doc_type = parse_request_text(f"{subject}\n{body}")
            if matter_number and doc_type:
                log.info("Request: matter=%s, type=%s, from=%s, subject=%r",
                         matter_number, doc_type, sender, subject)
                requests.append(EmailRequest(
                    uid=uid,
                    sender=sender,
                    subject=subject,
                    message_id=msg.get("Message-ID", ""),
                    matter_number=matter_number,
                    doc_type=doc_type,
                ))
            else:
                log.info("Ignoring (no matter/type): subject=%r", subject)
        return requests
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def mark_email_read(cfg: Config, uid: bytes) -> None:
    """Re-open IMAP and flag the message as \\Seen."""
    try:
        imap = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
        imap.login(cfg.email_address, cfg.email_password)
        imap.select(cfg.mailbox)
        imap.store(uid, "+FLAGS", "\\Seen")
        imap.logout()
        log.info("Marked original email as read.")
    except Exception as exc:
        log.warning("Could not mark email as read: %s", exc)


# --------------------------------------------------------------------------- #
# 2. WEB AUTOMATION & SCRAPING  (Playwright)
# --------------------------------------------------------------------------- #


def _format_date(raw: str) -> str:
    """Convert MM/DD/YYYY -> 'Month D, YYYY'; pass through anything else."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%B %-d, %Y")
        except ValueError:
            continue
    return raw


def _scrape_header(page) -> dict:
    """Scrape the matter header (labels + values) using DOM geometry.

    FileMaker WebDirect renders fields as positioned <div class="text"> value
    boxes with separate <span class="fm-text-character"> labels, so we read the
    raw positions and reconstruct the record by column.
    """
    return page.evaluate(
        """() => {
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const values = [];
            // FileMaker value cells carry the single class "text"; column/row
            // labels are "v-label ... text", so an exact single-class match
            // isolates the actual field values.
            document.querySelectorAll('div.text').forEach(el => {
                if (el.classList.length !== 1 || !el.classList.contains('text')) return;
                const t = norm(el.innerText);
                const r = el.getBoundingClientRect();
                if (t && r.width > 0 && r.height > 0 && r.y < 320)
                    values.push({ text: t, x: Math.round(r.x), y: Math.round(r.y) });
            });
            // Title - Description is a span that includes the dollar amount.
            let title = '';
            document.querySelectorAll('span.fm-text-character, .iwps_text_box').forEach(el => {
                const t = norm(el.innerText);
                const r = el.getBoundingClientRect();
                if (r.y < 320 && /\\$\\s?[\\d,]/.test(t) && t.length > title.length)
                    title = t;
            });
            return { values, title };
        }"""
    )


def _interpret_header(raw: dict) -> dict:
    """Turn the raw geometric scrape into named fields using column ordering."""
    values = [v for v in raw.get("values", []) if v["text"]]
    out = {
        "matter_number": "", "title_description": "", "amount": "",
        "matter_type": "", "category": "", "initial_filing": "",
        "final_filing": "", "status": "",
    }

    # Matter number + status share the left-most column.
    matter = next((v for v in values if re.fullmatch(r"M\d{5}", v["text"], re.IGNORECASE)), None)
    if matter:
        out["matter_number"] = matter["text"].upper()
        same_col = [v for v in values
                    if abs(v["x"] - matter["x"]) < 80 and v["y"] > matter["y"]]
        if same_col:
            out["status"] = sorted(same_col, key=lambda v: v["y"])[0]["text"]

    # Dates: earliest = initial filing, latest = final filing.
    date_re = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
    dated = []
    for v in values:
        if date_re.fullmatch(v["text"]):
            try:
                dated.append((datetime.strptime(v["text"], "%m/%d/%Y"), v["text"]))
            except ValueError:
                pass
    if dated:
        dated.sort(key=lambda d: d[0])
        out["initial_filing"] = _format_date(dated[0][1])
        out["final_filing"] = _format_date(dated[-1][1])

    # Title / amount.
    title = raw.get("title", "")
    amount_match = re.search(r"\$\s?[\d,]+(?:\.\d+)?", title)
    if amount_match:
        out["amount"] = amount_match.group(0).replace(" ", "")
        out["title_description"] = title[: amount_match.start()].strip().rstrip("-").strip()
    else:
        out["title_description"] = title.strip()

    # Type + Category live in the middle column (right of matter, left of dates),
    # excluding the title and date/matter/status cells already consumed.
    consumed = {out["matter_number"], out["status"], title}
    matter_x = matter["x"] if matter else 0
    min_date_x = min((v["x"] for v in values if date_re.fullmatch(v["text"])), default=10_000)
    mids = [
        v for v in values
        if matter_x + 120 < v["x"] < min_date_x - 30
        and v["text"] not in consumed
        and not date_re.fullmatch(v["text"])
    ]
    mids.sort(key=lambda v: v["y"])
    if len(mids) >= 1:
        out["matter_type"] = mids[0]["text"]
    if len(mids) >= 2:
        out["category"] = mids[1]["text"]

    return out


def _scrape_counts(page) -> dict:
    """Read 'Exhibits - 13', 'Other Documents - 42', ... tab counts."""
    texts = page.evaluate(
        """() => {
            const set = new Set();
            document.querySelectorAll('*').forEach(el => {
                if (el.children.length === 0) {
                    const t = (el.innerText || '').trim();
                    if (/^(Exhibits|Key Documents|Other Documents|Transcripts|Recordings)\\s*-\\s*\\d+$/.test(t))
                        set.add(t);
                }
            });
            return [...set];
        }"""
    )
    counts = {dt: 0 for dt in DOC_TYPES}
    for t in texts:
        m = re.match(r"(.+?)\s*-\s*(\d+)$", t)
        if m and m.group(1).strip() in counts:
            counts[m.group(1).strip()] = int(m.group(2))
    return counts


def _matter_loaded(page, timeout_ms: int = 15_000) -> bool:
    """Wait until the matter detail page (its document tabs) has rendered."""
    try:
        page.get_by_role(
            "button",
            name=re.compile(r"^(Exhibits|Key Documents|Other Documents|Transcripts|Recordings)\s*-\s*\d+"),
        ).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _click_matter_search(page, box) -> bool:
    """Click the 'Search' button nearest (and to the right of) the matter field."""
    target = page.evaluate(
        """(fb) => {
            const cands = [...document.querySelectorAll('.fm-button-container')]
                .filter(e => (e.innerText || '').trim() === 'Search');
            let best = null, bestD = 1e9;
            for (const e of cands) {
                const r = e.getBoundingClientRect();
                if (r.x < fb.x) continue;                 // must be to the right of the field
                const d = Math.abs((r.y + r.height / 2) - (fb.y + fb.height / 2));
                if (d < bestD) { bestD = d; best = { x: Math.round(r.x + r.width / 2),
                                                      y: Math.round(r.y + r.height / 2) }; }
            }
            return best;
        }""",
        box,
    )
    if not target:
        log.error("Could not locate the matter 'Search' button.")
        return False
    page.mouse.click(target["x"], target["y"])
    return True


def _open_matter(page, matter_number: str) -> bool:
    """Type the matter number into 'Go Directly to Matter' and search.

    FileMaker WebDirect syncs the typed value to the server asynchronously, so we
    pause after typing to let the value commit before submitting, then verify the
    detail page actually rendered (retrying once if it did not).
    """
    field = page.locator(".iwps_edit_box.fm-textarea-prompt").first
    field.wait_for(state="visible", timeout=30_000)
    box = field.bounding_box()
    field.click()
    page.wait_for_timeout(500)
    page.keyboard.type(matter_number, delay=40)
    # Let the value sync to the FileMaker server before submitting.
    page.wait_for_timeout(2_000)

    if not _click_matter_search(page, box):
        return False
    if _matter_loaded(page):
        return True

    # Retry once: re-type, commit with Enter, and re-submit.
    log.warning("Matter page did not load; retrying search for %s.", matter_number)
    try:
        field.click()
        page.keyboard.type(matter_number, delay=40)
        page.wait_for_timeout(2_000)
        page.keyboard.press("Enter")
        page.wait_for_timeout(1_500)
        _click_matter_search(page, box)
    except Exception as exc:
        log.warning("Retry interaction failed: %s", exc)
    return _matter_loaded(page)


def _open_tab(page, doc_type: str) -> bool:
    """Click the tab whose label starts with the requested document type."""
    try:
        tab = page.get_by_role("button", name=re.compile(rf"^{re.escape(doc_type)}\s*-\s*\d+"))
        tab.first.click(timeout=10_000)
        page.wait_for_timeout(4_000)
        return True
    except Exception as exc:
        log.error("Could not open tab %r: %s", doc_type, exc)
        return False


def _download_files(page, dest_dir: str, limit: int) -> list:
    """Click up to `limit` 'GO GET IT' buttons and save each downloaded file."""
    saved = []
    buttons = page.get_by_role("button", name="GO GET IT")
    try:
        available = buttons.count()
    except Exception:
        available = 0
    n = min(limit, available)
    log.info("%d 'GO GET IT' buttons present; attempting %d download(s).", available, n)

    for i in range(n):
        try:
            page.get_by_role("button", name="GO GET IT").nth(i).click(timeout=10_000)
            # The modal exposes a .fm-download-button labelled with the filename.
            dl_button = page.locator(".fm-download-button").first
            dl_button.wait_for(state="visible", timeout=10_000)
            with page.expect_download(timeout=20_000) as dl_info:
                dl_button.click()
            download = dl_info.value
            filename = download.suggested_filename or f"document_{i + 1}"
            path = os.path.join(dest_dir, filename)
            download.save_as(path)
            saved.append(path)
            log.info("  [%d/%d] downloaded %s", i + 1, n, filename)
        except Exception as exc:
            log.warning("  [%d/%d] download failed: %s", i + 1, n, exc)
        finally:
            # Dismiss the modal so the next row is clickable.
            try:
                page.get_by_role("button", name="Close").first.click(timeout=5_000)
                page.wait_for_timeout(800)
            except Exception:
                pass
    return saved


def scrape_matter(cfg: Config, matter_number: str, doc_type: str, dest_dir: str) -> MatterData:
    """Drive the browser end-to-end for one matter; never raises on missing UI."""
    from playwright.sync_api import sync_playwright

    data = MatterData(matter_number=matter_number)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.headless)
        context = browser.new_context(viewport={"width": 1400, "height": 1000},
                                      accept_downloads=True)
        page = context.new_page()
        try:
            log.info("Navigating to %s", UARB_URL)
            page.goto(UARB_URL, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(5_000)

            if not _open_matter(page, matter_number):
                return data

            # Open the requested tab first: on the document-list view the date
            # columns are labelled 'Date Received' and 'Date Final Submissions',
            # which is exactly what the summary template needs.
            opened = _open_tab(page, doc_type)

            # Scrape header metadata (best-effort, tolerant of missing fields).
            try:
                header = _interpret_header(_scrape_header(page))
                data.matter_number = header["matter_number"] or matter_number
                data.title_description = header["title_description"]
                data.amount = header["amount"]
                data.matter_type = header["matter_type"]
                data.category = header["category"]
                data.initial_filing = header["initial_filing"]
                data.final_filing = header["final_filing"]
                data.status = header["status"]
                log.info("Scraped: title=%r type=%r category=%r amount=%r",
                         data.title_description, data.matter_type, data.category, data.amount)
            except Exception as exc:
                log.warning("Header scrape incomplete: %s", exc)

            # Read all five tab counts.
            try:
                data.counts = _scrape_counts(page)
                log.info("Counts: %s", data.counts)
            except Exception as exc:
                log.warning("Count scrape failed: %s", exc)
                data.counts = {dt: 0 for dt in DOC_TYPES}

            # Download up to 10 of the requested type.
            if opened and data.counts.get(doc_type, 0) > 0:
                data.downloaded_files = _download_files(page, dest_dir, MAX_DOWNLOADS)
            else:
                log.info("No documents to download for %r.", doc_type)
        except Exception as exc:
            log.error("Web automation error: %s", exc)
        finally:
            context.close()
            browser.close()
    return data


# --------------------------------------------------------------------------- #
# 3. FILE PROCESSING
# --------------------------------------------------------------------------- #


def zip_files(files: list, zip_path: str, max_bytes: Optional[int] = None) -> list:
    """Compress files into a single ZIP, keeping it under `max_bytes`.

    Files are added in order; any file that would push the total over the cap is
    skipped (so the attachment always fits an email). Since PDFs barely compress,
    gating on raw size is a safe over-estimate. Returns the files actually added.
    """
    present = [f for f in files if os.path.exists(f)]

    # Choose which files to include. With a cap, pick smallest-first so the most
    # documents fit under the limit; otherwise keep the original order.
    if max_bytes is not None:
        candidates = sorted(present, key=os.path.getsize)
        chosen, total = [], 0
        for f in candidates:
            size = os.path.getsize(f)
            if total + size > max_bytes:
                continue
            chosen.append(f)
            total += size
    else:
        chosen = present

    included = [f for f in present if f in set(chosen)]  # preserve original order
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in included:
            zf.write(f, arcname=os.path.basename(f))

    if len(included) < len(present):
        log.warning("Attachment cap (%.0f MB): included %d of %d file(s).",
                    (max_bytes or 0) / 1e6, len(included), len(present))
    else:
        log.info("Wrote %s (%d file(s)).", zip_path, len(included))
    return included


# --------------------------------------------------------------------------- #
# 4. AI SUMMARY GENERATION (OpenRouter)
# --------------------------------------------------------------------------- #


def _template_summary(data: MatterData, doc_type: str, downloaded: int) -> str:
    """Deterministic fallback summary in the exact required structure."""
    c = data.counts
    transcripts_recordings = c.get("Transcripts", 0) + c.get("Recordings", 0)
    title = data.title_description or "this matter"
    amount = f" {data.amount}" if data.amount else ""
    return (
        f"{data.matter_number} is about the {title}{amount}. "
        f"It relates to {data.category or 'N/A'} within the {data.matter_type or 'N/A'} category. "
        f"The matter had an initial filing on {data.initial_filing or 'N/A'} "
        f"and a final filing on {data.final_filing or 'N/A'}. "
        f"I found {c.get('Exhibits', 0)} Exhibits, {c.get('Key Documents', 0)} Key Documents, "
        f"{c.get('Other Documents', 0)} Other Documents, and {transcripts_recordings} "
        f"Transcripts or Recordings. "
        f"I downloaded {downloaded} out of the {c.get(doc_type, 0)} {doc_type} "
        f"and am attaching them as a ZIP here."
    )


def generate_summary(cfg: Config, data: MatterData, doc_type: str, downloaded: int) -> str:
    """Ask a lightweight LLM (via OpenRouter) to fill the fixed template."""
    c = data.counts
    facts = {
        "matter_number": data.matter_number,
        "title_description": data.title_description,
        "amount": data.amount,
        "type": data.matter_type,
        "category": data.category,
        "initial_filing_date": data.initial_filing,
        "final_filing_date": data.final_filing,
        "exhibits": c.get("Exhibits", 0),
        "key_documents": c.get("Key Documents", 0),
        "other_documents": c.get("Other Documents", 0),
        "transcripts_or_recordings": c.get("Transcripts", 0) + c.get("Recordings", 0),
        "requested_document_type": doc_type,
        "total_in_requested_category": c.get(doc_type, 0),
        "number_downloaded": downloaded,
    }

    template = (
        "[Matter Number] is about the [Title Description] $[Amount]. It relates to "
        "[Category] within the [Type] category. The matter had an initial filing on "
        "[Date Recalled] and a final filing on [Date Final Submissions]. I found "
        "[X] Exhibits, [Y] Key Documents, [Z] Other Documents, and [A] Transcripts "
        "or Recordings. I downloaded [Number <= 10] out of the [Total in requested "
        "category] [Requested Document Type] and am attaching them as a ZIP here."
    )

    prompt = (
        "You are drafting the body of a reply email for a regulatory research agent.\n"
        "Use ONLY the JSON facts provided. Produce ONE paragraph that follows this "
        "EXACT structure, substituting the bracketed placeholders and removing the "
        "brackets. Do not add greetings, sign-offs, markdown, or any extra text.\n\n"
        f"STRUCTURE:\n{template}\n\n"
        "Notes: '$[Amount]' should render the amount exactly as given (it already "
        "includes the $ sign, so do not add another). '[A] Transcripts or "
        "Recordings' uses the combined transcripts_or_recordings count.\n\n"
        f"FACTS (JSON):\n{facts}"
    )

    try:
        from openai import OpenAI

        client = OpenAI(api_key=cfg.openrouter_api_key, base_url=cfg.openrouter_base_url)
        resp = client.chat.completions.create(
            model=cfg.openrouter_model,
            messages=[
                {"role": "system", "content": "You output only the requested email body text."},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text:
            log.info("LLM summary generated (%d chars).", len(text))
            return text
        raise ValueError("empty LLM response")
    except Exception as exc:
        log.warning("LLM generation failed (%s); using deterministic fallback.", exc)
        return _template_summary(data, doc_type, downloaded)


# --------------------------------------------------------------------------- #
# 5. EMAIL RESPONSE (SMTP)
# --------------------------------------------------------------------------- #


def send_reply(cfg: Config, req: EmailRequest, body: str, zip_path: str) -> None:
    """Reply to the original sender with the summary and the ZIP attached."""
    msg = EmailMessage()
    msg["From"] = cfg.email_address
    msg["To"] = req.sender
    subject = req.subject or f"Documents for {req.matter_number}"
    msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if req.message_id:
        msg["In-Reply-To"] = req.message_id
        msg["References"] = req.message_id
    msg.set_content(body)

    if zip_path and os.path.exists(zip_path):
        with open(zip_path, "rb") as fh:
            msg.add_attachment(
                fh.read(),
                maintype="application",
                subtype="zip",
                filename=os.path.basename(zip_path),
            )

    log.info("Sending reply to %s via %s:%s", req.sender, cfg.smtp_host, cfg.smtp_port)
    context = ssl.create_default_context()
    if cfg.smtp_port == 465:
        # Implicit SSL.
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=context) as server:
            server.login(cfg.email_address, cfg.email_password)
            server.send_message(msg)
    else:
        # STARTTLS (e.g. port 587).
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(cfg.email_address, cfg.email_password)
            server.send_message(msg)
    log.info("Reply sent.")


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def handle_request(cfg: Config, req: EmailRequest) -> bool:
    """Process a single request end-to-end. Returns True on a successful reply."""
    log.info("Handling request %s / %s for %s", req.matter_number, req.doc_type, req.sender)
    with tempfile.TemporaryDirectory(prefix="uarb_") as workdir:
        download_dir = os.path.join(workdir, "downloads")
        os.makedirs(download_dir, exist_ok=True)

        data = scrape_matter(cfg, req.matter_number, req.doc_type, download_dir)

        zip_path = os.path.join(workdir, "attachments.zip")
        # Base64 encoding inflates an attachment by ~37%, so cap the raw ZIP at
        # ~70% of the message budget to stay under the limit once encoded.
        raw_cap = int(cfg.max_attachment_mb * 1_000_000 * 0.70)
        attached = zip_files(data.downloaded_files, zip_path, max_bytes=raw_cap)

        # Report the number actually attached (some may be dropped to fit email).
        body = generate_summary(cfg, data, req.doc_type, len(attached))
        log.info("Email body:\n%s", body)

        try:
            send_reply(cfg, req, body, zip_path)
        except Exception as exc:
            log.error("Failed to send reply: %s", exc)
            return False

    # Only mark read once the reply was sent successfully.
    mark_email_read(cfg, req.uid)
    return True


def run_once(cfg: Config) -> int:
    """Process every pending request found in one inbox scan; return the count."""
    try:
        requests = find_requests(cfg)
    except Exception as exc:
        log.error("Could not read inbox: %s", exc)
        return 0

    if not requests:
        log.info("No new document requests.")
        return 0

    handled = 0
    # Oldest first so requests are answered in the order they arrived.
    for req in reversed(requests):
        try:
            if handle_request(cfg, req):
                handled += 1
        except Exception as exc:
            log.error("Unexpected error handling %s: %s", req.matter_number, exc)
    log.info("Processed %d request(s).", handled)
    return handled


def run_loop(cfg: Config, max_runtime: Optional[int] = None) -> int:
    """Poll the inbox, handling new requests as they arrive.

    Runs until interrupted, or (if ``max_runtime`` is set) until that many
    seconds have elapsed. The bounded mode lets a scheduler (e.g. a GitHub
    Actions cron) restart the agent without overlapping runs while still polling
    on a short interval.
    """
    import time

    interval = max(5, cfg.poll_interval)
    started = time.monotonic()
    log.info("Starting agent in loop mode (every %ds%s). Press Ctrl+C to stop.",
             interval, f", for up to {max_runtime}s" if max_runtime else "")
    if cfg.agent_address:
        log.info("Listening for requests addressed to: %s", cfg.agent_address)
    else:
        log.warning("AGENT_ADDRESS is not set: every parseable unread email will be "
                    "treated as a request. Set AGENT_ADDRESS to filter your own mail.")
    try:
        while True:
            try:
                run_once(cfg)
            except Exception as exc:
                log.error("Cycle failed: %s", exc)
            if max_runtime is not None and (time.monotonic() - started) + interval >= max_runtime:
                log.info("Reached max runtime (%ds); exiting for scheduler restart.",
                         max_runtime)
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Regulatory document ingestion agent: emails -> documents -> reply."
    )
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously, checking the inbox on an interval.")
    parser.add_argument("--interval", type=int, default=None,
                        help="Polling interval in seconds for --loop "
                             "(overrides POLL_INTERVAL_SECONDS).")
    parser.add_argument("--max-runtime", type=int, default=None,
                        help="With --loop, exit after this many seconds "
                             "(useful for scheduler-restarted runs).")
    args = parser.parse_args()

    cfg = Config()
    if args.interval is not None:
        cfg.poll_interval = args.interval

    if args.loop:
        return run_loop(cfg, max_runtime=args.max_runtime)
    run_once(cfg)
    log.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
