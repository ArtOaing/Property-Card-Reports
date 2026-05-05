# Notion meeting sync — setup

This dashboard pulls **Pricing Meeting** and **Portfolio Review** notes from the listings Notion database. A GitHub Action runs every 30 minutes, fetches the latest content via the Notion API, and writes `data/meetings.json` to the repo. The dashboard reads that JSON on load and renders a **Property Timeline** section in each property card.

The Notion token lives in GitHub Secrets — it never touches the browser.

---

## One-time setup

### 1. Create a Notion integration
1. Go to <https://www.notion.so/profile/integrations>
2. **New integration** → name it (e.g. *GoldSoil Dashboard Sync*) → pick the workspace where the listings database lives
3. After creation, copy the **Internal Integration Secret** (starts with `ntn_…` or `secret_…`) — you'll paste this into GitHub in step 4

Keep the **Read content** capability enabled. You don't need write access.

### 2. Connect the integration to the database
The integration only sees pages explicitly shared with it.

1. Open the listings database in Notion
2. `…` (top-right) → `Connections` → search for the integration → **Confirm**

This grants the integration read access to every page in the database.

### 3. Get the database ID
- Open the database as a full page in Notion (not as an inline view)
- Copy the URL — it looks like `https://www.notion.so/{workspace}/{32-char-id}?v=…`
- The 32-character chunk before `?v=` is the database ID. You can paste it with or without hyphens.

### 4. Add secrets and variables to the GitHub repo
In the repo: **Settings → Secrets and variables → Actions**

**Repository secret** (encrypted, only readable by Actions):
- `NOTION_TOKEN` → the integration secret from step 1

**Repository variables** (visible in the repo, not sensitive):
- `NOTION_DATABASE_ID` → the ID from step 3
- `NOTION_TRANSACTION_PROPERTY` → the **exact** name of the property on the database that holds the Transaction # (e.g. `Transaction #` — match capitalization, spaces, and the `#` symbol)

### 5. Commit the new files
Drop the bundle into the repo so it ends up at:

```
.github/workflows/sync-notion.yml
scripts/sync_notion.py
data/meetings.json          ← starts empty; the Action overwrites it
index.html                  ← updated dashboard
```

Push.

### 6. Run the Action once and verify
- **Actions** tab → **Sync Notion meeting notes** → **Run workflow** (manual trigger)
- After ~30 seconds, check `data/meetings.json` — it should contain a populated `byTransaction` map
- Open the dashboard, click any listing whose Transaction # exists in the Notion database — the **Property Timeline** section appears between the chronological timeline and Red Flags

From here on, the Action runs automatically every 30 minutes on cron.

---

## How it matches a listing to a Notion page

Salesforce field `Transaction` on the listing → matched against the Notion database property named in `NOTION_TRANSACTION_PROPERTY`. Exact string match, whitespace-trimmed.

If the dashboard shows *"No Transaction # on this listing"*, the Salesforce row doesn't have a Transaction value.

If it shows *"No Pricing Meeting or Portfolio Review notes synced yet for this transaction"*, the page exists but has no toggle blocks whose label starts with `Pricing Meeting` or `Portfolio Review`.

---

## What gets extracted

For each Notion page, the script walks the page (up to 3 levels of nesting — handles being inside a heading or column) and finds toggle blocks whose plain-text label **starts with**:
- `Pricing Meeting` (rendered as a purple badge)
- `Portfolio Review` (rendered as a teal badge)

For each meeting toggle:
- **Date** is parsed from the toggle label (e.g. `Pricing Meeting — 2026-04-28` → `2026-04-28`). Both `YYYY-MM-DD` and `M/D/YYYY` are recognized.
- **Bullets** are pulled from the toggle's body — bulleted lists, numbered lists, to-dos, sub-headings, callouts, and short paragraphs all become bullet items. Long paragraphs are reduced to their first sentence as a preview.
- A maximum of **8 bullets per meeting** are kept (the dashboard then renders the first 6). Adjust `MAX_BULLETS_PER_MEETING` in `scripts/sync_notion.py` if you want more.

The card always includes an **"Open in Notion ↗"** link that anchors to the specific meeting toggle, so the full notes are one click away.

---

## Adjusting

| Want to… | Edit |
|---|---|
| Change sync frequency | `cron` in `.github/workflows/sync-notion.yml` |
| Add a new meeting type (e.g. `DD Review`) | `MEETING_TYPES` tuple in `scripts/sync_notion.py` |
| Change bullet cap | `MAX_BULLETS_PER_MEETING` in the same file |
| Look deeper for nested toggles | `RECURSION_DEPTH` in the same file |
| Match a different Salesforce field instead of `Transaction` | `txn:` line in `parseRow()` in `index.html` |

---

## Troubleshooting

**Action fails with `401 Unauthorized`**
The `NOTION_TOKEN` secret is missing or wrong. Re-check step 4. Make sure you didn't accidentally paste the OAuth client ID instead of the internal integration secret.

**Action runs successfully but `byTransaction` is empty**
The integration isn't connected to the database. Re-check step 2. Open one page from the database and look at the bottom-right "Connections" — the integration must be listed.

**Action runs and finds pages, but `meetings` arrays are empty for every page**
The toggle labels in Notion don't start with `Pricing Meeting` or `Portfolio Review`. Either rename the toggles or update `MEETING_TYPES` in the script.

**JSON updates but nothing shows in the dashboard**
1. Open browser DevTools → Network → reload — confirm `data/meetings.json` is fetched and has a `byTransaction` key with your Transaction # in it
2. Confirm the listing's `Transaction` field in Salesforce **exactly** matches the Notion property value (no trailing whitespace, no different casing on letters)

**Action commits a new `meetings.json` every run even though nothing changed**
The `lastSynced` timestamp changes every run, so the file always differs by at least a line. This is intentional — it's how the dashboard knows how fresh the data is. If the noise in commit history bothers you, change the workflow's commit step to ignore `lastSynced`-only diffs.

**Rate limits**
Notion's API limit is ~3 requests/second per integration. The script makes roughly `1 + (pages × 2 + meetings × 1)` requests per run. For a 50-listing portfolio with ~2 meetings each that's ~250 requests over a few seconds — well under the limit. If you blow past it, add `time.sleep(0.4)` between page fetches.
