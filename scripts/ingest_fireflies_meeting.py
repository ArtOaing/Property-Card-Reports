#!/usr/bin/env python3
"""
Update an existing Fireflies meeting row in The Acre Hub's Fireflies Meetings database
with the full transcript text.

Architecture:
  Fireflies meeting ends
    → Fireflies-Notion integration auto-creates a row with metadata (title, date, etc.)
    → Make.com sees meeting completed, fires repository_dispatch with the meeting ID
    → THIS SCRIPT fetches the full transcript from Fireflies, finds the matching row,
      and fills in the Full Transcript field
    → Notion Agent fires on Full Transcript fill, dispatches per-Transaction notes

Why update instead of create: the Fireflies-Notion integration is already creating one
row per meeting with all the metadata. Creating a second row would cause duplicates.
Our job is just to populate the one field the integration can't fill (the transcript).

Stdlib only — no pip installs.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------- Config ----------

NOTION_VERSION = "2022-06-28"
NOTION_BASE = "https://api.notion.com/v1"
FIREFLIES_BASE = "https://api.fireflies.ai/graphql"

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
FIREFLIES_API_KEY = os.environ["FIREFLIES_API_KEY"]
INBOX_DATABASE_ID = os.environ["FIREFLIES_INBOX_DATABASE_ID"]
MEETING_ID = os.environ.get("MEETING_ID", "").strip()

# Property names on the Notion inbox database. Override via env if your column names differ.
PROP_TITLE      = os.environ.get("PROP_TITLE",      "Title")
PROP_TRANSCRIPT = os.environ.get("PROP_TRANSCRIPT", "Full Transcript")
PROP_MEETING_ID = os.environ.get("PROP_MEETING_ID", "Fireflies ID")
PROP_DATE       = os.environ.get("PROP_DATE",       "Date")

# The Fireflies-Notion integration adds " (Fireflies)" to the end of every meeting title.
# We construct the expected Notion title by appending this to the raw Fireflies title.
NOTION_TITLE_SUFFIX = " (Fireflies)"

# When matching by title + date, allow this much clock drift between Fireflies meeting
# time and the timestamp the Notion integration recorded.
DATE_MATCH_WINDOW_MIN = 15

# Notion has a hard limit of 2000 characters per rich_text element. We split longer
# transcripts into multiple chunks and pass them as a list of rich_text elements.
NOTION_RICH_TEXT_CHUNK = 2000


# ---------- HTTP helpers ----------

def http_request(method, url, headers, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} on {method} {url}: {body_text}", file=sys.stderr)
        raise
    except URLError as e:
        print(f"Network error on {method} {url}: {e}", file=sys.stderr)
        raise


# ---------- Fireflies ----------

def fireflies_query(query, variables=None):
    headers = {
        "Authorization": f"Bearer {FIREFLIES_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {"query": query, "variables": variables or {}}
    resp = http_request("POST", FIREFLIES_BASE, headers, body)
    if "errors" in resp:
        raise RuntimeError(f"Fireflies GraphQL errors: {json.dumps(resp['errors'])}")
    return resp.get("data", {})


def fetch_meeting(meeting_id):
    """Fetch a Fireflies transcript including sentences, title, date, etc."""
    query = """
    query Transcript($id: String!) {
      transcript(id: $id) {
        id
        title
        date
        duration
        transcript_url
        sentences { text speaker_name }
      }
    }
    """
    data = fireflies_query(query, {"id": meeting_id})
    t = data.get("transcript")
    if not t:
        raise RuntimeError(f"Fireflies returned no transcript for id={meeting_id}")
    return t


def assemble_transcript(transcript):
    """Join all sentences into one block of text, with speaker labels on speaker change."""
    sentences = transcript.get("sentences") or []
    if not sentences:
        return ""
    lines, last_speaker = [], None
    for s in sentences:
        speaker = (s.get("speaker_name") or "").strip()
        text = (s.get("text") or "").strip()
        if not text:
            continue
        if speaker and speaker != last_speaker:
            lines.append(f"\n{speaker}: {text}")
            last_speaker = speaker
        else:
            lines.append(text)
    return " ".join(lines).strip()


def fireflies_date_to_dt(ts):
    """Fireflies returns date as a unix epoch in milliseconds. Convert to datetime."""
    if ts is None:
        return None
    try:
        ts = int(ts)
        if ts > 10**12:  # ms
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return None


# ---------- Notion ----------

def notion_request(method, path, body=None):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    return http_request(method, f"{NOTION_BASE}{path}", headers, body)


def find_row_by_meeting_id(meeting_id):
    """If we've previously stamped a row with this Fireflies ID, find it directly."""
    body = {
        "filter": {"property": PROP_MEETING_ID, "rich_text": {"equals": meeting_id}},
        "page_size": 1,
    }
    try:
        resp = notion_request("POST", f"/databases/{INBOX_DATABASE_ID}/query", body)
        results = resp.get("results", [])
        return results[0] if results else None
    except HTTPError as e:
        print(f"  WARNING: Fireflies-ID query failed ({e})", file=sys.stderr)
        return None


def find_row_by_title_and_date(notion_title, meeting_dt):
    """Find a Notion row matching the expected title + date window.

    Returns the best match: a row with the matching title whose Date falls within
    +/-DATE_MATCH_WINDOW_MIN of the meeting time. If multiple match, prefer the one
    where Full Transcript is empty (most likely the row Fireflies just created).
    """
    if not meeting_dt:
        print(f"  WARNING: meeting_dt is None — can't do date-windowed match", file=sys.stderr)
        return None

    after = (meeting_dt - timedelta(minutes=DATE_MATCH_WINDOW_MIN)).isoformat()
    before = (meeting_dt + timedelta(minutes=DATE_MATCH_WINDOW_MIN)).isoformat()
    body = {
        "filter": {
            "and": [
                {"property": PROP_TITLE, "title": {"equals": notion_title}},
                {"property": PROP_DATE, "date": {"on_or_after": after}},
                {"property": PROP_DATE, "date": {"on_or_before": before}},
            ]
        },
        "page_size": 10,
    }
    try:
        resp = notion_request("POST", f"/databases/{INBOX_DATABASE_ID}/query", body)
        results = resp.get("results", [])
    except HTTPError as e:
        print(f"  ERROR: title+date query failed ({e})", file=sys.stderr)
        return None

    if not results:
        return None

    # Prefer rows where Full Transcript is empty — that's the row that needs filling.
    def transcript_is_empty(page):
        prop = page.get("properties", {}).get(PROP_TRANSCRIPT, {})
        rt = prop.get("rich_text") or []
        return all(not (r.get("plain_text") or "").strip() for r in rt)

    empty_first = sorted(results, key=lambda p: (0 if transcript_is_empty(p) else 1))
    return empty_first[0]


def get_existing_transcript_text(page):
    """Return the current Full Transcript content (joined plain_text) for an existing row."""
    prop = page.get("properties", {}).get(PROP_TRANSCRIPT, {})
    rt = prop.get("rich_text") or []
    return "".join((r.get("plain_text") or "") for r in rt).strip()


def chunk_text(text, size=NOTION_RICH_TEXT_CHUNK):
    if not text:
        return [""]
    return [text[i:i + size] for i in range(0, len(text), size)]


def rich_text_array(text):
    return [{"type": "text", "text": {"content": chunk}} for chunk in chunk_text(text)]


def update_row(page_id, full_text, meeting_id):
    """Patch the Full Transcript and Fireflies ID properties on an existing row."""
    body = {
        "properties": {
            PROP_TRANSCRIPT: {"rich_text": rich_text_array(full_text)},
            PROP_MEETING_ID: {"rich_text": [{"type": "text", "text": {"content": meeting_id}}]},
        }
    }
    return notion_request("PATCH", f"/pages/{page_id}", body)


# ---------- Main ----------

def main():
    if not MEETING_ID:
        print("ERROR: MEETING_ID env var is empty.", file=sys.stderr)
        sys.exit(1)

    print(f"Ingesting Fireflies meeting {MEETING_ID}", file=sys.stderr)

    # Fast path: have we stamped this Fireflies ID into a row before?
    by_id = find_row_by_meeting_id(MEETING_ID)
    if by_id:
        existing_text = get_existing_transcript_text(by_id)
        if existing_text:
            print(f"  Row {by_id['id']} already has Full Transcript ({len(existing_text)} chars) — skipping.", file=sys.stderr)
            return
        # Row exists but transcript empty → fall through to fetch and update

    # Fetch transcript from Fireflies
    transcript = fetch_meeting(MEETING_ID)
    raw_title = (transcript.get("title") or "").strip()
    notion_title = raw_title + NOTION_TITLE_SUFFIX
    meeting_dt = fireflies_date_to_dt(transcript.get("date"))
    full_text = assemble_transcript(transcript)
    print(f"  Fireflies title: {raw_title!r}", file=sys.stderr)
    print(f"  Looking for Notion row titled: {notion_title!r}", file=sys.stderr)
    print(f"  Meeting time: {meeting_dt.isoformat() if meeting_dt else '<unknown>'}", file=sys.stderr)
    print(f"  Sentences: {len(transcript.get('sentences') or [])}, transcript chars: {len(full_text)}", file=sys.stderr)

    # If we already found the row by Fireflies ID, use that. Otherwise search by title+date.
    target = by_id or find_row_by_title_and_date(notion_title, meeting_dt)
    if not target:
        print(f"  ERROR: no matching Notion row found for {notion_title!r} within +/-{DATE_MATCH_WINDOW_MIN}min", file=sys.stderr)
        print(f"  This usually means the Fireflies-Notion integration hasn't created the row yet.", file=sys.stderr)
        print(f"  Make sure the Fireflies integration runs first, or wait a minute and re-trigger this workflow.", file=sys.stderr)
        sys.exit(2)

    # Idempotency: if the matched row already has a transcript, skip
    existing_text = get_existing_transcript_text(target)
    if existing_text:
        print(f"  Matched row {target['id']} already has Full Transcript ({len(existing_text)} chars) — skipping.", file=sys.stderr)
        # Still stamp the Fireflies ID so future runs can find it directly
        try:
            notion_request("PATCH", f"/pages/{target['id']}", {
                "properties": {PROP_MEETING_ID: {"rich_text": [{"type": "text", "text": {"content": MEETING_ID}}]}}
            })
            print(f"  (stamped Fireflies ID for future lookups)", file=sys.stderr)
        except Exception as e:
            print(f"  (couldn't stamp Fireflies ID: {e})", file=sys.stderr)
        return

    if not full_text:
        print(f"  WARNING: Fireflies returned an empty transcript — not updating Notion.", file=sys.stderr)
        sys.exit(3)

    update_row(target["id"], full_text, MEETING_ID)
    print(f"  Updated Notion row {target['id']} with {len(full_text)} chars of transcript — agent should fire shortly.", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
