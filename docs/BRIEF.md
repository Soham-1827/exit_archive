# Product brief — Exit Archive

Source: overnight market research, 2026-11-16, and the API test on 2026-11-17.

## The problem
Harvest (time tracking and invoicing) was acquired by Bending Spoons and repriced. One user reports going from $13.32 to $133.98 a month. "RIP, Harvest" drew 445 points and 147 comments on Hacker News on 2026-11-09. Many users are leaving. At least six replacements were launched in that one thread, but **none carries your history out**:
- Harvest has no single export. Data is spread across about nine separate exports.
- The invoice report and the payments-received report cannot be run once the plan lapses.
- Data is deleted 10 days after an immediate close, or six months after a scheduled cancellation. Reactivating to get it back starts a new subscription at the new price.
- A comparison of replacements says "none of these tools imports your Harvest invoices".

## Evidence (real, linked)
- "I went through the same process earlier this year after receiving a note that they intended to raise my annual charge to over $1100, from the $150 or so I’d been paying for a decade or more." — https://news.ycombinator.com/item?id=50422470
- "when I got the bill this month and saw the price was over $200 and then noticed the Bending Spoons LLC tagline at the bottom, that was the end. Claude is backing up all of my data via the API and will be vibe coding my next invoice." — https://news.ycombinator.com/item?id=50420826
- "The cost of moving is high because you have been on their platform for years. The cost of staying is high because of the price increases. Either choice feels bad." — https://news.ycombinator.com/item?id=50423835
- A comparable product exists for QuickBooks Online: Books Backup, $499 per company, one-time (https://booksbackup.com/).

## Who buys
Freelancers, consultancies and small agencies (1-20 people) with 5-15 years in Harvest, in the weeks between the price-increase email and cancelling. They need invoices and time records for tax and client disputes. The buyers who matter are non-developers: designers, consultants, agencies. Developers will script it themselves.

## Price (estimate)
$49 one-time per account. Test $79 for agencies with more than five users.

## The MVP
**Core:** token in, zip out. The zip contains:
- every time entry, client, contact, project, task, user, expense, invoice (data plus PDF), estimate, and payment, as both CSV and JSON
- every receipt image, linked to its expense
- `index.html`: one offline page to search and filter everything by client, date, project and invoice number. It works from `file://` with no network and no server.
- `MANIFEST.json`: the count of each record type, plus a checksum per file, so the buyer can verify completeness.

**Skip (binding unless the user changes it):** importing into other tools, user accounts and logins, teams, scheduled backups, any vendor other than Harvest, SMS, and a mobile app.

**Flow:** landing page → pay (Stripe Payment Link) → paste token → job runs in the background with a progress page → download link valid for 7 days → the files are deleted.

**Suggested stack:** Python 3.12, `uv`, httpx (async) for the Harvest client, FastAPI with server-rendered pages for the web side, an in-process background job runner, local disk or S3-compatible storage with expiry, Stripe Payment Link, and Docker. The orchestrator may change this with reasons.

**Constraints:** solo developer, about 10-15 hours a week, under $500 total spend, MVP in 14 days (about 28 hours).

## API facts (verified 2026-11-17 on a trial account)
- Account discovery: `GET https://id.getharvest.com/api/v2/accounts` (Bearer token) returns `accounts[].id`.
- Data: `https://api.harvestapp.com/v2/<resource>`, with headers `Authorization: Bearer <token>`, `Harvest-Account-Id`, and `User-Agent`. Responses are paginated (`next_page`, `total_entries`, `links.next`).
- Company: `GET /v2/company` returns `base_uri` and the feature flags (`invoice_feature`, `expense_feature`, `estimate_feature`).
- Receipts: `expense.receipt = {url, file_name, file_size, content_type}`. A GET on `url` with the Bearer token returns the original file byte-for-byte; without the token it returns a login page.
- Invoice PDF: `{base_uri}/client/invoices/{client_key}.pdf` returns `application/pdf`, including for draft invoices, with no token needed.
- Not yet verified: rate limits on a large account, estimate PDF URLs, archived projects, and pagination beyond page 1.

## Risks
- Leavers are a one-time wave and each buys once. The fix is to add more repriced vendors later (FreshBooks, HoneyBook).
- A replacement vendor could ship a complete free importer.
- Harvest could remove the public invoice link or the receipt URL.
