# job-agent

A human-in-the-loop job application assistant. It finds openings on public job
boards, opens each application form in a real browser, fills in everything it
can from your profile, and then **stops so you can review and submit it
yourself**.

> **It never clicks submit.** There is no submit code in this project. Every
> run ends with prepared browser tabs waiting for you to read, correct and
> send.

## Quick start

```bash
# 1. Install
git clone https://github.com/Dashakid/job-agent.git && cd job-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && playwright install chromium

# 2. Tell it about you (then edit both files; put your résumé PDF in this folder)
cp profile.example.json profile.json
cp candidate_context.example.md candidate_context.md

# 3. Find jobs, then prepare them
python scraper.py all "Data Engineer" "Python"
python batch_runner.py --concurrency 1
```

A Chromium window opens with one tab per job, each filled in as far as the
agent could go. The terminal lists what still needs you. Check every tab,
finish any remaining questions, and click submit yourself.

A run's output looks like this:

```text
=== Preparing: Backend Engineer, Platform ===
  [ok] Filled 'first_name'
  [ok] Uploaded resume: resume.pdf (confirmed on page)
  [ok] Answered 'Will you now or in the future require sponsors...' -> No
  [ok] Declined to self-identify on 3 EEO topic(s)
  [review] 1 required field(s) still need you:
      - What are your salary expectations?
  [review] Ready for manual completion and submission
```

The rest of this README covers each step in detail.

## What it does

| Step | Script | What happens |
|---|---|---|
| 1. Discover | `scraper.py` | Pulls open roles from the public Greenhouse, Ashby, Lever and Workable board APIs, filters by keyword, US/remote location and seniority, and writes a queue file. |
| 2. Prepare | `batch_runner.py` | Opens each queued job in a Chromium tab and fills in contact details, résumé upload, work authorization, location, education, logistics and screener questions. Leaves every tab open for review. |
| 3. Draft | built in | Open-ended prompts ("Why this role?") are filled from `prepared_answers.json` or drafted with Gemini, using **only** facts from your `candidate_context.md`. |
| 4. Track | `cli.py sync-*` | Optionally logs applications to a Google Sheet and reads confirmation emails from Gmail to update statuses. |

It handles Ashby, Greenhouse, Lever and Workday form layouts, with generic
fallbacks for other sites. If a step fails, it saves a screenshot to
`failures/`, asks Gemini for a plain-language diagnosis, and retries.

There is also an **outreach agent** (`outreach_agent.py`) that researches a
company's tech stack and drafts a short cold message to a founder or
engineering lead in a LinkedIn, X, Gmail or contact-form tab. It never sends
anything either.

### Safety defaults

- **No auto-submit.** Neither the application runner nor the outreach agent
  sends anything.
- **Sensitive questions are left for you:** salary, criminal history, and
  anything certifying a fact about you (such as citizenship or export-control
  status).
- **Agreements are opt-in.** Privacy consents and "I have read the agreement"
  boxes are only ticked if you turn on `acknowledge_privacy_statements` or
  `auto_accept_legal_acknowledgements` in your profile.
- **Résumé uploads are verified.** The agent checks that the site actually
  shows your file. If it doesn't, you get a warning to attach it yourself.
- **EEO / demographic questions** (gender, race, veteran, disability) are
  answered only if `eeo_response` is
  `"decline"`, and then only with the form's own "decline to answer" option.
  With any other value they are left blank for you.
- **No invented facts.** AI drafts may only use what is written in
  `candidate_context.md`. If there isn't enough information, the field is left
  empty.
- **Bot challenges** (CAPTCHA, Cloudflare) are never bypassed. The tab is
  flagged and left open for you to clear.

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/Dashakid/job-agent.git
cd job-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### 1. Your profile

```bash
cp profile.example.json profile.json
cp candidate_context.example.md candidate_context.md
cp prepared_answers.example.json prepared_answers.json   # optional
```

- **`profile.json`** holds your contact details, links, work authorization,
  education and screener answers. Set `resume_path` to your résumé PDF (paths
  are relative to the project folder).
  A few settings decide how screening questions are answered:
  - `start_availability` is the text used for "When can you start?".
  - `open_to_in_office` and `willing_to_relocate` answer hybrid and relocation
    questions. Set them to `null` to leave those questions for you.
  - `auto_accept_legal_acknowledgements` ticks agreement boxes such as
    "I have read the Arbitration Agreement". It is off by default. Boxes that
    certify facts about you (citizenship, export-control status) are never
    ticked.
- **`candidate_context.md`** is your background written out in prose. It is the
  only source the AI may draw on.
- **`prepared_answers.json`** holds reusable answers to recurring essay
  questions. Each entry is a case-insensitive regex `pattern` matched against a
  question's label, plus the `answer` to fill in. These are used before any AI
  call, so they still work when you have no API key.

All three files are listed in `.gitignore`, along with your résumé, logs and
queues, so they stay on your machine.

### 2. Gemini API key (optional)

Answer drafting and failure diagnosis use Google's Gemini. Get a key at
<https://aistudio.google.com/apikey>, then:

```bash
export GEMINI_API_KEY="your-key"
```

Without a key everything else still works. Open-ended fields are left blank
for you to write.

### 3. Google Sheets and Gmail tracking (optional)

- **Sheets:** create a Google Cloud service account, download its key as
  `service_account.json` into the project folder, and share a spreadsheet named
  **Job Application Tracker** with the service account's `client_email`.
- **Gmail:** enable the Gmail API, create an OAuth client of type *Desktop
  app*, and save it as `credentials.json`. The first `sync-email` run opens a
  browser to grant read-only access and caches the result in `token.json`.

If these files are missing, the sync steps are skipped with a warning.

### Check your setup

```bash
python smoke_test.py
```

This checks your Gemini key, local logging (in a temp folder), Sheets
credentials (read-only) and the CLI. It doesn't touch your real logs or your
sheet.

## Usage

### Find jobs

```bash
python scraper.py all                                  # every board in TARGET_COMPANIES, default keywords
python scraper.py all "Data Engineer" "Python"         # your own keywords
python scraper.py greenhouse stripe Backend            # a single board
python scraper.py all --roles "Product Designer" --output queues/design_jobs.json --allow-senior
```

Results are appended (de-duplicated) to `queues/pending_jobs.json`. Edit
`TARGET_COMPANIES` and `DEFAULT_KEYWORDS` in `scraper.py` to set which
companies and roles you search. The default seniority filter is tuned for
junior and mid-level searches: it drops senior, staff, lead, manager and
intern titles. Use `--allow-senior` or `--exclude-titles REGEX` to change it.

### Prepare applications

```bash
python batch_runner.py --concurrency 1
python batch_runner.py --file queues/design_jobs.json --concurrency 3
```

A headed Chromium window opens with one tab per job. When it finishes, the
runner lists any tabs that did **not** finish cleanly. Review each tab, submit
the ones you want, then close the browser.

Jobs that already reached the review step are skipped next time. Use
`--ignore-history` to prepare them again. To ignore history from before a
certain date, put an ISO-8601 timestamp in `queues/history_cutoff.txt`.

> Tip: `--concurrency 1` is the most reliable setting. Also close browser
> windows from earlier runs before starting a new one. An old tab can show
> stale values that look like a bug.

### One job, or the CLI

```bash
python cli.py apply --url "https://jobs.ashbyhq.com/..."   # one form, pauses in the Playwright inspector
python cli.py review-batch --file queues/fit_jobs.json     # a hand-picked list, one tab each
python cli.py sync-email                                   # Gmail -> application log
python cli.py sync-sheets                                  # application log -> Google Sheet
python cli.py --help
```

### Outreach

```bash
cp examples/outreach_targets.example.json queues/outreach_targets.json   # then edit
python cli.py outreach-run --targets queues/outreach_targets.json
python cli.py outreach-mark-sent --company "Acme Inc" --contact "Jane Doe"
python cli.py outreach-sync-sheet
```

The outreach runner uses a persistent browser profile
(`~/.job-agent-browser-profile`), so you only need to log in to LinkedIn, X or
Gmail once. It records state changes (drafted → reviewed → sent) in a local
SQLite database. A message is only marked as sent when you confirm it with
`outreach-mark-sent`.

## Files it creates

| Path | Contents |
|---|---|
| `queues/` | Job queues, the batch runner's status log, the outreach database |
| `applications_log.json` / `.csv` | Every application prepared with `cli.py` |
| `self_heal_log.json`, `failures/` | Failure diagnoses and screenshots |

All of these are git-ignored.

## Tests

```bash
pytest
```

The tests make no network or API calls. Most use mocks. `tests/test_form_e2e.py`
runs the real form-filling pipeline in headless Chromium against a local sample
form (`tests/fixtures/sample_form.html`). It checks that fields are filled,
sensitive items are left alone, and nothing is submitted. It is skipped if
Chromium isn't installed.

## Responsible use

This tool fills in forms. You are still the applicant. Read every
application before you submit it, make sure every answer is true, and follow
each site's terms of service. The scraper only uses public job-board APIs and
backs off when it is rate-limited. Please keep it that way.

## License

[MIT](LICENSE)
