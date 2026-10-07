<div align="center">

# Rest Tester

**A desktop workbench for exploring, testing and load-testing REST APIs.**

Rest Tester turns an API catalog into a guided workspace: browse every endpoint, build requests from
their schemas, verify responses, organise regression suites, drive requests from CSV data and
measure behaviour under load. Everything runs in one native app on your machine.

[![License: GPL v3+](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
![Version](https://img.shields.io/badge/version-2.1.0-6f42c1.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-3776ab.svg?logo=python&logoColor=white)
![PyQt6](https://img.shields.io/badge/UI-PyQt6-41cd52.svg?logo=qt&logoColor=white)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)
![Tests](https://img.shields.io/badge/tests-pytest-0a9edc.svg?logo=pytest&logoColor=white)
[![Website](https://img.shields.io/badge/website-rest--tester-0878f9.svg)](https://priyankdalal.github.io/rest-tester/)

[Website](https://priyankdalal.github.io/rest-tester/) ·
[Features](#features) ·
[Quick start](#quick-start) ·
[Screenshots](#screenshots) ·
[Documentation](#documentation) ·
[AI guide](https://priyankdalal.github.io/rest-tester/ai.html) ·
[Contributing](#contributing) ·
[License](#license)

<img src="docs/images/api-explorer.png" alt="Rest Tester API Explorer" width="100%">

</div>

---

## Table of contents

- [Why Rest Tester?](#why-rest-tester)
- [Features](#features)
- [Quick start](#quick-start)
- [Screenshots](#screenshots)
- [Documentation](#documentation)
  - [API Explorer](#api-explorer)
  - [Test Suites](#test-suites)
  - [Data Runner](#data-runner)
  - [Load Studio](#load-studio)
  - [Catalog Builder](#catalog-builder)
  - [Environments and authentication](#environments-and-authentication)
  - [Ask AI (local or hosted LLM)](#ask-ai-local-or-hosted-llm)
  - [The API catalog format](#the-api-catalog-format)
  - [Local data storage](#local-data-storage)
  - [Keyboard shortcuts](#keyboard-shortcuts)
- [Building a standalone executable](#building-a-standalone-executable)
- [Running the tests](#running-the-tests)
- [Project website](#project-website)
- [Project structure](#project-structure)
- [Contributing](#contributing)
- [License](#license)

---

## Why Rest Tester?

Most REST clients give you an empty URL box. Rest Tester starts from a **catalog** of your API
(services, endpoints, parameters, filterable and sortable fields, enums and payload schemas) and
uses it to build the right UI for every endpoint:

- **Schema-driven, not free-text.** Payloads are rendered as forms with typed fields. Enums become
  drop-downs, flags become checkboxes, and nested objects become groups.
- **Domain-aware query builder.** Each endpoint offers only the fields it can actually filter and
  sort on, and only the operators that make sense for each field's type.
- **Testing is built in.** You go from an ad-hoc request to a saved regression case in one click,
  then to a data-driven batch or a load test.
- **Local workspaces, optional hosted AI.** No workbench account or cloud sync. API requests go
  straight from your machine to your API, and history lives in local SQLite files. Opting into
  hosted AI sends sanitized prompts and selected catalog context to your configured provider.

---

## Features

| | Workspace | What it gives you |
|---|---|---|
| 🔎 | **API Explorer** | Searchable endpoint tree with favourites and recents. It includes filter and sort builders, schema-driven payload forms, seed-data generation, a response viewer, a request timeline waterfall, saved requests and collections, and cURL/Python code generation. |
| ✅ | **Test Suites** | Ordered regression cases with expected status and rich assertions (body, JSON path, headers, timing, response schema). Run analysis, per-case results and a suite waterfall are included. |
| 📊 | **Data Runner** | Drives one endpoint with every row of a CSV file: map columns to parameters and payload fields, validate before sending, run in parallel, then review and export the results. Runs can be saved and reopened. |
| ⚡ | **Load Studio** | Closed-model load tests with ramp-up, steady and ramp-down stages and safety limits. It shows a live dashboard (throughput, latency percentiles, errors, virtual users), a detailed final report and PDF export. Runs can be saved and reopened. |
| 🛠️ | **Catalog Builder** | Creates and edits the catalog: services, endpoints, parameters, schemas, allowed values and filter/sort fields. It can also scan ASP.NET Core service repositories to generate a catalog. |
| 🌐 | **Environments** | Per-environment base URLs, variables, custom headers, TLS and timeout settings, plus managed authentication (OAuth 2.0, Microsoft Entra/B2C, API keys, JWT and more) with automatic token renewal. |
| ✦ | **Ask AI** | Catalog-aware drafts for **Single request**, **Test Suite**, **Data Runner** and **Load Testing**. Named Ollama, Azure OpenAI, OpenAI, Claude and Sarvam AI connections; bounded repairs, Activity diagnostics, token details and usage history. Review a validated draft, open it in its native workspace, then explicitly Send or Run. [AI guide](https://priyankdalal.github.io/rest-tester/ai.html). |
| 🎨 | **Polished desktop UX** | Light and dark themes, a responsive layout and keyboard shortcuts. An About dialog shows version, system and license details. |

---

## Quick start

> **Requirements:** Python 3.11 or newer and Git. Rest Tester runs on Windows, macOS and Linux.

### 1. Clone and install

```bash
git clone https://github.com/priyankdalal/rest-tester.git
cd rest-tester

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Start the demo API (optional, but recommended)

The repository ships with a small, dependency-free **Store** API so you can try every feature
without pointing the app at a real service:

```bash
python examples/demo_store_server.py            # serves http://127.0.0.1:8765
python examples/demo_store_server.py --port 9000 --quiet
```

It serves products, categories, customers and orders with seeded data. It supports filtering,
sorting, paging, field selection, JSON Patch, `HEAD` counts, `problem+json` validation errors,
file downloads and image responses.

### 3. Launch Rest Tester

```bash
python run.py
```

### 4. Load the demo catalog

1. Click the **Settings** gear in the top toolbar, immediately after the theme button, then open **API catalog → Load catalog…** and choose `examples/store_catalog.json`.
2. Each environment's base URLs are pre-filled from the catalog (for example
   `http://127.0.0.1:8765/catalog/v1/`). Change them under **Environments** if you started the
   demo server on another port.
3. Back in **API Explorer**, pick `GET /Product` under **Catalog** and click **Send**. Then open the
   `Filter` row's builder on the **Query** tab to try a filter.

That's it: you're exploring a fully described API.

---

## Screenshots

Explorer, query/sort builders and AI screenshots were refreshed on **3 October 2026**.
AI examples use the demo Store catalog, synthetic plans and illustrative usage from an
offline fake provider. No model or test API was called to capture them; other screenshots
illustrate the existing workspaces.

<table>
  <tr>
    <td width="50%"><img src="docs/images/api-explorer-request.png" alt="Request builder"><br><sub><b>API Explorer</b>: endpoint tree, request builder and response viewer</sub></td>
    <td width="50%"><img src="docs/images/api-explorer-dark.png" alt="Dark theme"><br><sub><b>Dark theme</b>: every workspace supports light and dark</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/payload-form.png" alt="Payload form"><br><sub><b>Payload form</b>: rendered from the payload schema</sub></td>
    <td><img src="docs/images/payload-json.png" alt="Payload JSON"><br><sub><b>Raw JSON</b>: always available and authoritative</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/test-suites.png" alt="Test suites"><br><sub><b>Test Suites</b>: run analysis across every case</sub></td>
    <td><img src="docs/images/suite-case-result.png" alt="Case result"><br><sub><b>Case result</b>: assertion-by-assertion outcome</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/suite-timeline.png" alt="Suite timeline"><br><sub><b>Suite timeline</b>: waterfall of the whole run</sub></td>
    <td><img src="docs/images/data-runner-source.png" alt="Data runner source"><br><sub><b>Data Runner</b>: pick an endpoint and a data source</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/data-runner-mapping.png" alt="Data runner mapping"><br><sub><b>Column mapping</b>: bind columns to params and payload</sub></td>
    <td><img src="docs/images/data-runner-results.png" alt="Data runner results"><br><sub><b>Batch results</b>: per-row outcome, timing and export</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/load-studio-configure.png" alt="Load studio configure"><br><sub><b>Load Studio</b>: stage-based load profile</sub></td>
    <td><img src="docs/images/load-studio-live.png" alt="Load studio live"><br><sub><b>Live dashboard</b>: throughput, latency and users in real time</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/load-studio-results.png" alt="Load studio results"><br><sub><b>Final results</b>: min / avg / max for every metric</sub></td>
    <td><img src="docs/images/catalog-builder-endpoint.png" alt="Catalog builder"><br><sub><b>Catalog Builder</b>: describe endpoints visually</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/environments.png" alt="Environments"><br><sub><b>Environments</b>: base URLs, headers and auth per target</sub></td>
    <td><img src="docs/images/about.png" alt="About dialog"><br><sub><b>About</b>: version, system details and licenses</sub></td>
  </tr>
</table>

---

## Documentation

### API Explorer

The explorer is the home workspace. On the left is the **endpoint tree**, grouped by service.
It supports search (`Ctrl+L`), method and scope filters, favourites and recently used endpoints.
On the right, each endpoint has **Request**, **Documentation**, **Examples** and **Test History**
tabs. The request itself is edited in five sub-tabs:

| Request tab | Purpose |
|---|---|
| **Path** | Path parameters with typed editors and catalog defaults. |
| **Query** | Query parameters. `Filter` and `Sort` rows open their schema-driven builders. |
| **Form** | `multipart/form-data` fields. File fields get **Browse…** and **File options** controls. |
| **Payload JSON** | The raw body. **Payload fields** opens a schema-driven form that writes back to the JSON. |
| **Headers** | Catalog header fields and request-specific custom headers, with inclusion toggles and a masked effective-header preview showing their source. |

#### Request headers

Use **Request > Headers > Add header** to enter a name and value for this request.
Environment **Custom Headers** remain shared defaults; request rows override them
case-insensitively. If a name appears more than once, the last row wins. Unticking
a row disables that override, leaving any environment default intact.
Values support `{{variables}}`, including when a request becomes a suite case.
Required catalog headers can also be supplied by environment defaults or managed
authentication. Invalid names and values are rejected before sending.

The preview shows **Default**, **Environment**, **Request**, and **Managed authentication**
sources without acquiring credentials. Known credential values are masked, and conflicting
manual/managed headers are flagged and blocked. Select **Manual headers only** intentionally
to send a manual credential; **No authentication** omits known credential headers.
The HTTP client adds body and transport headers at send time.

Headers follow endpoint drafts, saved requests, collections and suite handoff.
**Open in Data Runner** retains them in the request template; **Open in Load Studio**
opens them in a matching **Request headers** section. Header variables are resolved
against the frozen execution environment. Load-run artifacts deliberately omit header values.
Saved Requests omit known credential headers (including configured managed header names).
Suite files can store header values in plain text, so use variable placeholders rather
than literal credentials; arbitrary custom header names are not automatically classified
as secrets.

#### Multipart file options

For an upload, select a file on **Form**, then click its **File options** button.
Each file has independent settings:

| Option | Behaviour |
|---|---|
| **Upload filename** | Overrides the filename sent to the API without renaming the local file. Blank uses the local filename. |
| **Content type** | For example, `image/png`, `application/pdf`, or `application/octet-stream`. Blank detects the type from the upload filename's extension, falling back to `application/octet-stream`; it does not inspect file contents. |
| **Part headers** | Optional name/value headers sent inside that file's multipart section, not as request-wide headers. |

**OK** applies changes; **Cancel** keeps the previous options. The `*` on **File options**
indicates saved overrides. Clear an override to return to automatic values. File metadata
supports `{{variables}}` and follows drafts, saved requests, suites, collections, Data Runner
templates, and Load Studio. cURL and Python exports include the multipart options.

Leave the outer request **Content-Type** unset for uploads: the HTTP client generates
`multipart/form-data` with the correct boundary. An explicit request-wide content type is
rejected for file uploads. Part `Content-Type`, `Content-Disposition`, and `Content-Length`
are reserved; use the filename/content-type fields instead of adding these as custom headers.
Invalid names, media types and control characters are rejected before sending.

Known credential part headers are masked in the editor, exported examples and recorded
request bodies, omitted by **No authentication**, and stripped from Saved Requests.
Load-run artifacts omit part-header values. Suite files can contain literal part headers;
prefer variables for sensitive data. Arbitrary header names are not automatically treated
as credentials. Existing file-list fields still accept one file per request.

The header actions are:

- **Send**: issues the request. Its drop-down offers **Send and Verify** (`Ctrl+Enter`), which also
  validates the response against the endpoint's response schema.
- **Add to Test Suite**: captures the current request as a suite case. Its drop-down also offers
  **Open in Load Studio** and **Open in Data Runner**, which carry the same request over.
- **Save Request** and **Favorite**: keep requests in **Saved Requests** and **Collections**.
- **More** (**⋯**): **Generate cURL**, **Generate Python Request** and **Copy URL**.

**Seed data** fills payloads and filters with realistic, type-aware values:

- GUID fields get fresh UUIDs and dates get ISO dates.
- Enum fields get real members only.
- Range operators (`bt`) get two values, and list operators (`in`, `nin`) get several.
- Text operators (`ct`, `sw`, `ew`) get a substring fragment.

The **response viewer** has **Body**, **Headers**, **Timeline**, **Request** and **Validation**
tabs. JSON is syntax-highlighted, images are previewed and file downloads can be saved. The
**timeline** breaks each call into a DNS, TCP connect, TLS and HTTP waterfall.

<p align="center"><img src="docs/images/api-explorer-request.png" alt="API explorer request" width="85%"></p>

### Test Suites

A **suite** is an ordered list of test cases. Each case pins one endpoint with its parameters,
payload, expected status and assertions. Any number of cases can target the same endpoint, so
one endpoint can have a happy path, a not-found case and a validation-failure case.

| Assertion group | Assertions |
|---|---|
| Body | contains / does not contain text, matches regex |
| JSON | field equals / not equals / contains, exists / absent, not empty, length equals / at least / at most, type is |
| Headers | header equals, header exists |
| Timing | responds within *N* ms |
| Schema | matches the endpoint's response schema |

Cases can be renamed, duplicated, reordered and individually disabled. A suite run produces a
**run analysis** summarising outcomes and timings across all cases and a detailed **result** for
each case. It also produces a **timeline** waterfall of the whole run. Suites are plain JSON files
in `suites/`, so they diff cleanly and can live in version control.

<p align="center"><img src="docs/images/suite-case-result.png" alt="Suite case result" width="85%"></p>

### Data Runner

The Data Runner executes one endpoint once per row of a CSV file. It is a step-by-step wizard:

1. **Source**: pick the service and endpoint (with type-ahead search), then load a CSV file.
2. **Mapping**: bind columns to path, query, header and payload fields. Matching names are
   mapped automatically, and values can optionally be parsed as JSON.
3. **Validate**: preview the resolved requests and catch type or required-field problems before
   anything is sent.
4. **Execute & review**: run with configurable concurrency, watch progress and inspect each
   row's status, timing and response. Export the results when you're done.
5. **History**: reopen earlier executions.

Every execution is a **portable SQLite run file** that can be loaded back later.

<p align="center">
  <img src="docs/images/data-runner-mapping.png" alt="Data runner mapping" width="49%">
  <img src="docs/images/data-runner-results.png" alt="Data runner results" width="49%">
</p>

### Load Studio

Load Studio runs **closed-model load tests** against a single endpoint:

- **Configure**: choose the endpoint with type-ahead search, then build a profile from
  ramp-up, steady and ramp-down stages with think time.
- **Safety & Thresholds**: cap peak virtual users, scenario duration and request timeout. Then
  set pass/fail thresholds (for example error rate, P95 or P99 latency) for the verdict.
- **Live & Results**: the live dashboard shows throughput, average latency, P99, errors,
  completed requests and active virtual users, with charts and per-stage progress. When the run
  finishes, the cards switch to **min / avg / max** summaries.

The final report breaks down every metric, highlights shortcomings and suggests likely causes
and fixes. It can be **exported to PDF** with charts and tables. Runs can be persisted to a
SQLite file and reopened from the Live & Results title bar.

<p align="center">
  <img src="docs/images/load-studio-live.png" alt="Load studio live" width="49%">
  <img src="docs/images/load-studio-results.png" alt="Load studio results" width="49%">
</p>

### Catalog Builder

The Catalog Builder (`python run_builder.py`, or **Settings → Open Catalog Builder**) is a visual
editor for the catalog. It covers:

- services and base paths;
- endpoints with method, route, summary and content types;
- path, query, header and form parameters, with allowed-value lists;
- payload and response schemas, including enums, nested objects and arrays;
- filterable and sortable fields with their types and operators.

It can also **generate** a catalog by statically scanning ASP.NET Core service repositories
(`python -m tools.generate_catalog`). It reads controllers, routes, DTOs and entity attributes.
Use `Alt+←` / `Alt+→` to go back and forward through the items you have visited.

<p align="center">
  <img src="docs/images/catalog-builder-endpoint.png" alt="Catalog builder endpoint" width="49%">
  <img src="docs/images/catalog-builder-filter.png" alt="Catalog builder filters" width="49%">
</p>

### Environments and authentication

Each environment profile has five tabs:

| Tab | Contents |
|---|---|
| **Base URLs** | one URL per service in the loaded catalog |
| **Variables** | name/value pairs substituted into requests |
| **Custom headers** | name/value pairs; `Authorization` and `x-api-key` have one-click presets |
| **Transport** | TLS verification and request timeout |
| **Authentication** | managed credential profiles, service bindings, sign-in and renewal |

Managed authentication obtains tokens and **renews them automatically**:

- **Microsoft identity (MSAL):** Entra ID browser sign-in (PKCE), Azure AD B2C, client credentials.
- **Generic OAuth 2.0:** authorization code + PKCE, client credentials, device code, resource
  owner password, refresh token. This works with Auth0, Okta, Keycloak, Google and others.
- **Static credentials:** bearer token, API key (header or query), Basic, JWT bearer (signed
  locally) and Atlassian ASAP.

> **Note:** static credentials (API keys, bearer tokens, custom headers) are stored in plain text in
> `data/settings.json`. Keep that file out of version control and off shared drives.

<p align="center">
  <img src="docs/images/environments.png" alt="Environments" width="60%">
  <img src="docs/images/environment-editor.png" alt="Environment editor" width="36%">
</p>

### Ask AI (local or hosted LLM)

**Start here:** [Dedicated AI documentation](https://priyankdalal.github.io/rest-tester/ai.html)
covers provider setup, all four workflows, privacy, token limits, usage and troubleshooting.
For architecture, code examples and diagrams, read the
[illustrated implementation handbook](roadmaps/ai-integration/AI-Implementation-Guide.pdf).

<table>
  <tr>
    <td width="50%"><img src="docs/images/ai-request.png" alt="Current Ask AI single-request draft"><br><sub>Single request: validate, review, then open.</sub></td>
    <td width="50%"><img src="docs/images/ai-suite.png" alt="Current Ask AI suite draft"><br><sub>Test Suite: cases, assertions, captures and cleanup.</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/ai-data.png" alt="Current AI CSV mapping plan"><br><sub>Data Runner: headers only, never CSV rows.</sub></td>
    <td><img src="docs/images/ai-load.png" alt="Current AI load-test profile"><br><sub>Load Testing: a draft does not grant execution approval.</sub></td>
  </tr>
  <tr>
    <td><img src="docs/images/ai-settings.png" alt="Named AI provider profiles"><br><sub>Five providers, one active connection.</sub></td>
    <td><img src="docs/images/ai-usage.png" alt="Metadata-only AI usage dashboard"><br><sub>Illustrative recorded usage, not provider billing.</sub></td>
  </tr>
</table>

Press the **sparkles icon (Ask AI)** in the header (or `Ctrl+K`) and describe the request.
An unavailable connection opens AI Settings first:

> *List brands whose name starts with Pi, Z to A, 10 per page*

The minimalist prompt window puts **AI plan** above a Copy / Open action row
and a compact prompt composer. A yellow light-bulb icon beside a muted warning at the top reminds you that AI can
make mistakes: review the plan before execution. Nothing will run without your approval.
Its test selector offers **Single request**, **Test Suite**, **Data Runner**, and **Load Testing**.
The composer shows
the active provider/model as a read-only label and a connection dot: blue when connected,
grey otherwise. The gear beside the provider label opens **AI Settings** directly;
it is disabled during generation. Providers can also be changed through
**Settings → AI Settings** in the main window.
Press the arrow button or `Ctrl+Enter` to generate. Cancel stops subsequent processing
cooperatively; an in-flight non-streaming model call may finish and still incur usage.
Example cards, extra headings, and the unused change-summary row are omitted.

**AI Data Runner.** Select Data Runner and **Choose CSV** before asking for a plan,
for example "Map this CSV to create brands using BrandName for the name". The planner
receives only column names, never CSV row values or the local file path, and proposes
catalog-aware mappings and transformations plus a base request. Review the plan and
use **Open in Data Runner** to replace the current draft after confirmation. The CSV
headers are checked again at hand-off. Review mappings and use the native validation
step before Run; planning and opening a draft do not execute any endpoint.

**AI Load Testing.** Describe a single endpoint, virtual-user stages, think time, and
optional thresholds, for example "Load test the brands list: ramp from 1 to 10 users
over 30 seconds, then hold for 60 seconds; P95 below 500 ms". The plan uses the studio's
supported closed virtual-user model and warm-up/ramp-up/steady/ramp-down stages.
Stage SLA participation uses native defaults; explicit overrides are rejected
both when parsing AI output and when revalidating an existing workflow draft.
**Open in Load Testing** transfers the editable request, stages, and thresholds after
confirmation. Environment permission, write-endpoint consent, and pre-run approval
are cleared, never granted by AI. Review safety limits and build the native summary
before explicitly starting. Open arrival-rate and multi-target scenarios are not supported.

The assistant answers with a plan, not a sent request:

| Plan part | Example |
|---|---|
| Endpoint | `GET /Brand`, MasterData `Brand.GetAllBrands` |
| Parameters | `PageSize = 10` |
| Filters | Name starts with Pi → `Filter=Name__sw:=Pi` |
| Sort | Name descending → `Sort=Name-` |
| Assumptions / warnings | e.g. "This DELETE request changes data on the server." |

**Open in API Explorer** loads the plan as an editable draft; you review it and press Send as usual.

**Token usage.** **Token details** below the composer is always clickable. Before a model
response, it explains that usage is not available yet.
It opens a per-call table with input/output/total tokens, purpose, time, and status for this
generation (including repair/refinement calls); **Copy usage** copies the
breakdown as JSON. Both single-request and test-suite planning use this view.
These are consumed tokens, not a percentage of the model's context capacity. Missing
provider counts and invalid responses whose usage was unavailable show **Not reported**,
not invented zeros. Local models have no cloud API charge; hosted cost is not estimated
without configured pricing. Previous prompts and connection checks are excluded.
Hosted invalid or truncated responses retain provider-reported token counts when
available, so failed generation is included in usage instead of discarded.

**Activity.** Toggle Activity below the composer to inspect timestamped execution
diagnostics while planning: retrieval counts, endpoint selection, model-call duration,
tokens, provider-reported finish reason, validation issue locations/codes, and refinement
or failure events. Copy activity exports the visible sanitized log. It is not private
model reasoning and does not include prompts or raw provider responses. Logs are bounded
to 300 lines, kept only in session memory, and restored with notification results.
A hosted structured response reports token-limit truncation when the provider's
finish reason identifies it, even if the returned text is parseable JSON.
Planning stops immediately rather than repeating the same budget. Ordinary
non-truncated JSON/validation errors still use the configured repair rounds.
Suite refinement adds each missing detailed endpoint card once, keyed by catalog
endpoint ID, even when several cases use the same endpoint. Assertion values are
literal comparisons; `{{variables}}` in assertions are reported for repair rather
than substituted (request parameters and payloads still support variables).
Failure diagnostics include reported tokens and whether final text was empty or
non-empty, not the raw content; unavailable finish reasons are not guessed.

<p align="center"><img src="docs/images/ai-activity.png" alt="Sanitized AI Activity diagnostics in the current composer" width="70%"></p>

**Token controls are not spending limits.** Per-call output ceilings are 2,000 tokens
for requests, 4,000 for Data Runner/Load Testing, and 6,000 for suites. The default
allows two repairs after the initial call; AI Settings permits up to 50 repair rounds
(51 total model calls including the initial plan). Each attempt can consume input and
output tokens again and may incur hosted-provider charges. Ollama's default context
window is 16,384; there is no exact local
provider-tokenizer preflight. There is no daily quota or hard monetary budget: use
your provider's spending controls for financial limits.

**Implementation handbook.** The beginner-friendly
[AI Implementation Guide (PDF)](roadmaps/ai-integration/AI-Implementation-Guide.pdf)
explains all four workflows, components, diagrams, examples, token controls,
usage accounting, safety boundaries and failure corrections. An
[HTML version](roadmaps/ai-integration/AI-Implementation-Guide.html) is included.
Rebuild locally with `python roadmaps/ai-integration/build-implementation-guide.py`
(requires Microsoft Edge or Chrome; no additional Python package or cloud call).
The generated function index reflects the source snapshot at build time.

**Usage history.** Open **Settings → AI Usage** for persistent app-recorded totals by
connection. Filter by Today, Last 7 days, Last 30 days, This session, or All recorded;
click Refresh to pick up new calls. The overview shows tokens, model calls, and validated
ready plans. **Export usage** saves the filtered per-call records as CSV.
History includes planning, repair/refinement calls and hosted connection checks, including
startup checks. Ollama model-list checks consume no generation tokens and are excluded.
Missing counts are marked unreported, and partial totals are labelled accordingly.
The ledger records connection/model identity, timestamps, purpose, counts, duration and
status—not prompts, keys, endpoint URLs or responses. Deleted connections retain their
history. Recording starts with this version; earlier usage cannot be reconstructed.
This dashboard is not provider-wide billing or quota reporting; hosted cost is not estimated.

**Test suite mode.** Switch the dialog to **Test suite** and describe a scenario:

> *Create a brand, read it back, rename it with PATCH, verify the new name, then delete it and confirm it is gone*

The preview lists every case in run order: method and path, expected status, dependencies,
parameters, filters, sort, body, checks (`json_equals`, `json_exists`, `response_schema`, ...) and
captures such as `brandId ← $.Id`. **Open in Test Suites** loads the suite unsaved and not yet run.
If Test Suites already has cases, you choose **Replace** or **Append**.

Each case goes through the same request validator. On top of that, the suite validator checks:

- Case keys are unique.
- Every `{{variable}}` is captured by an earlier case or defined as a suite variable. A missing
  dependency on the capturing case is added automatically.
- Dependencies exist and contain no cycles, and normal cases never depend on cleanup cases.
- Assertion kinds and values are valid, and response paths exist in the endpoint's response schema
  when one is declared.
- Delete or "undo" cases run in the always-run cleanup phase.

A suite request costs several model calls. On a CPU-only machine, expect several minutes.

**Settings window.** Click the gear immediately after the theme button in the top toolbar.
Settings opens as a separate window, with a left navigation rail:

- **Catalog** holds the catalog heading, descriptions, current catalog details, and all four
  actions: Load catalog, Reload, Use bundled catalog, and Open Catalog Builder.
- **AI Settings** manages named connections like environments. You can configure several
  Ollama servers/models, Azure deployments, OpenAI, Claude, or Sarvam AI accounts—even multiple
  connections for the same provider. Each has its own endpoint, model, key and hosted consent.
  The initial view is a table with **Use**, **Connection**, **Provider**, **Model / Endpoint**,
  and **Actions** columns, matching the Environments workspace.
- **Add provider** opens a new connection editor. Use a row's **Edit** action (or double-click
  the row) to change its configuration. **Save** persists the connection; **Cancel** discards
  its draft, including a newly added connection. The name field renames a connection.
- Tick a row's **Use** box to activate it. Exactly **one** connection is active for Ask AI;
  selecting or editing a row does not change that. The active row's tick stays selected.
  Use another connection before deleting the active one. **Delete** asks for confirmation
  and removes the saved connection and its unused encrypted key.
  Ask AI displays the active connection name/provider/model.
  **Test connection** in the editor checks the draft without saving or activating it.
  On startup, Rest Tester checks the saved active connection in the background. The
  icon is grey and static if no connection is configured, while checking, or if the
  connection fails. A healthy idle connection has a solid blue icon. While a prompt
  is executing a thin blue arc rotates around the stationary sparkles once every
  1.5 seconds, and disappears when execution finishes. The check also repeats
  after AI settings change. Clicking an unavailable icon opens AI Settings.
  Hosted startup checks send a tiny billed completion when hosted consent is enabled.
  Completed, failed, or cancelled AI generations add a bell notification. Clicking
  one opens a read-only Ask AI result window for that exact prompt, including its
  preview and usage, without rerunning it or interrupting another active generation.
  Copy/open-plan actions remain available for usable results. These result snapshots
  remain in memory for this app session, bounded to the notification history size.
  Inactive connections may be saved as unfinished drafts; their missing endpoint or model
  does not block saving another connection. Full validation applies when activating or
  testing a connection, and the active connection must remain valid.

| Provider | Configuration |
|---|---|
| Ollama | Server URL (default `http://localhost:11434`), installed model tag, reasoning toggle, and context window. Test connection lists installed models. |
| Azure OpenAI | Resource base URL (`https://<resource>.openai.azure.com`), deployment name, API version, and API key. Use a chat deployment supporting JSON mode. |
| OpenAI | API base URL (default `https://api.openai.com/v1`), model ID supporting JSON mode, and API key. |
| Claude / Anthropic | API base URL (default `https://api.anthropic.com`), model ID, and API key. Plans use structured tool output. |
| Sarvam AI | API base URL (default `https://api.sarvam.ai/v1`), model ID (default `sarvam-105b`), and API subscription key. Plans use JSON mode. Optional environment-variable binding: `SARVAM_API_KEY`. |

Sarvam connection checks explicitly disable reasoning, allow up to 128 output
tokens, and use a minimum 30-second timeout so a tiny probe does not exhaust its
budget before producing final text. Sarvam structured plan generation also disables
reasoning, retaining the existing 2,000-token request, 4,000-token Data/Load and
6,000-token suite budgets. Non-structured calls retain default reasoning.
An empty answer remains a failed check, with a safe explanation
of truncation, filtering, or the reported stop condition; only a successful check
activates the AI toolbar icon.

OpenAI and Azure OpenAI checks use `max_completion_tokens`; Claude uses
`max_tokens`. These probes allow up to 4,096 tokens and a minimum 60-second
timeout, leaving room for models whose default thinking consumes tokens before
the final answer. Probes omit temperature and do not force a reasoning setting:
support varies by model, and Azure deployment names do not identify the underlying
model. Other providers' normal planning settings are unchanged. These are application-selected
caps, not provider-prescribed limits or guarantees; hosted checks remain billed
for actual usage and do not automatically retry or increase the budget.

Ollama checks use `GET /api/tags` to confirm the configured model is available
without generating an answer or loading it into memory. Malformed/non-JSON model
lists are reported as failures. This checks availability, not inference quality.

Provider references:
[OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning),
[OpenAI parameter compatibility](https://developers.openai.com/api/docs/guides/latest-model/gpt-5.4),
[Azure reasoning](https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/reasoning),
[Claude thinking](https://platform.claude.com/docs/en/build-with-claude/thinking),
[Claude Messages](https://platform.claude.com/docs/en/api/messages/create),
[Ollama model list](https://docs.ollama.com/api/tags).

For local AI, install [Ollama](https://ollama.com), pull a structured-output model
(for example `ollama pull qwen3:14b`), and keep it running. Enter its model tag in AI Settings.
For hosted AI, enter a key per connection, or set an environment variable before starting
Rest Tester and enter its name in **Key environment variable**. This allows separate keys for
multiple accounts of the same provider. That variable's value takes precedence over the
connection's saved key. Keys entered in Settings are saved separately with OS-backed encryption,
not in the JSON settings file. Existing single-provider settings and keys are migrated
automatically; conventional `AZURE_OPENAI_API_KEY`, `OPENAI_API_KEY`, and `ANTHROPIC_API_KEY`
bindings are retained for migrated connections.
Enable **Allow prompts and selected API catalog metadata to be sent to hosted AI** before using
a cloud provider. **Test connection** makes a small billed request for hosted providers; it does
not invoke a test endpoint. A valid connection still requires a model compatible with planning.
Request timeout and maximum repair rounds are shared across providers.

**How it stays accurate.** The model never guesses from memory:

1. Rest Tester searches the loaded catalog locally and sends the model only the best-matching
   endpoints, including their real parameters, filterable fields, operators, enum values and payload fields.
2. The answer is constrained to a JSON schema, and the endpoint must be one of those candidates.
3. A validator checks every field, operator, enum value, sort field, parameter type and payload
   key against the catalog. It fixes harmless slips (letter case, `contains` → `ct`, a body on a GET),
   then sends anything else back to the model with the valid options, for up to two repair rounds.
4. A required path parameter left empty gets one targeted follow-up.

**Privacy.** Ollama runs locally. Hosted providers receive your prompt and selected catalog
metadata; do not use them for confidential data. Before each call, the access token, API key,
environment variable values, custom header values and auth-profile secrets are masked, along with
anything that looks like a JWT, bearer token or key. Settings are stored in `data/ai_settings.json`;
encrypted hosted keys live separately in `data/ai_credentials/` (excluded from Git).

**Speed.** A 36B model on a workstation GPU typically answers in 25–60 seconds; the first request
also loads the model. On CPU only, a single request can take minutes and a suite 5–20 minutes.
The request timeout (`timeout_seconds`, default 600) and the model context size
(`context_window`, default 16384, sent as Ollama `num_ctx`) live in `data/ai_settings.json`.
You can keep working while it runs. Closing the dialog doesn't stop the plan,
and **Cancel** abandons it.

### The API catalog format

A catalog is a single JSON file. An abridged example:

```json
{
  "schema_version": 2,
  "name": "Demo Store API",
  "services": [
    {
      "name": "Catalog",
      "default_base_url": "http://127.0.0.1:8765/catalog/v1/",
      "filter_sort_builders": true,
      "endpoints": [
        {
          "id": "f76aeb49ca37",
          "service": "Catalog",
          "controller": "Product",
          "method": "GET",
          "path": "/Product",
          "parameters": [
            { "name": "Filter",   "source": "query", "type": "string?", "required": false },
            { "name": "Sort",     "source": "query", "type": "string?", "required": false },
            { "name": "PageSize", "source": "query", "type": "int",     "required": false, "sample": 25 }
          ],
          "payload": null,
          "filter_entity": "Product",
          "expected_status": "200-299"
        }
      ]
    }
  ],
  "filter_schemas":   { "Product": { "...": "filterable / sortable fields and operators" } },
  "payload_schemas":  { },
  "form_schemas":     { },
  "response_schemas": { },
  "enums":            { }
}
```

The top-level schema maps hold reusable definitions that endpoints reference by name: filter
entities, payload and form data classes, response shapes and enum members.

See [`examples/store_catalog.json`](examples/store_catalog.json) for a complete catalog. It
includes payload schemas, enums, filterable and sortable fields, form uploads and file downloads.
The Catalog Builder reads and writes this format, so you rarely need to edit it by hand.

### Local data storage

| Data | Location | Notes |
|---|---|---|
| Settings and environments | `data/settings.json` | Catalog path, theme and environment profiles. |
| Ask AI settings | `data/ai_settings.json` | Named connections, one active connection ID, and planner options. No secrets. |
| Hosted AI keys | `data/ai_credentials/` | OS-encrypted API keys, excluded from Git. Environment variables can be used instead. |
| AI usage history | `data/ai_usage.db` | Persistent metadata-only call ledger, excluded from Git. |
| History, saved requests, collections | `data/workspace.db` (SQLite) | History older than 100 days is pruned automatically. |
| Test suites | `suites/*.json` | Plain JSON, friendly to version control. |
| Data Runner / Load Studio runs | `*.db` files you choose | One portable SQLite file per run; never pruned. |

When running from a packaged build, these folders are created next to the executable, which keeps
the app portable.

### Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+Enter` | Send and Verify the current request |
| `Ctrl+L` | Focus the endpoint search |
| `Ctrl+K` | Ask AI: describe a request in plain words |
| `Alt+←` / `Alt+→` | Back / forward through visited items (Catalog Builder) |

---

## Building a standalone executable

Rest Tester is packaged with [PyInstaller](https://pyinstaller.org/):

```powershell
pip install -r requirements-dev.txt
pyinstaller --noconfirm --clean RestTester.spec
```

The build is written to `dist/RestTester/`. Keep the whole folder together, because it contains
the Qt runtime. The application icon is generated at build time by `python -m tools.make_icon`.

The packaged build **ships without a catalog** on purpose. On first launch the explorer explains
how to load one from **Settings**, or how to create one with the Catalog Builder.

---

## Running the tests

The project has a large pytest suite covering the UI, request building, the filter grammar,
assertions, the runners and packaging.

```powershell
pip install -r requirements-dev.txt

# Headless Qt (Windows PowerShell)
$env:QT_QPA_PLATFORM = "offscreen"
$env:QT_QPA_FONTDIR  = "C:\Windows\Fonts"   # real font metrics for layout tests

python -m pytest                           # full suite, parallel via pytest-xdist
python -m pytest tests/test_load_testing_ui.py -q
```

On macOS or Linux, use `export QT_QPA_PLATFORM=offscreen` and point `QT_QPA_FONTDIR` at a system
font directory (for example `/usr/share/fonts`).

---

## Project website

The website lives in [`docs/`](docs). It is plain HTML and CSS with no build step, and it reuses
the screenshots in `docs/images/`. To publish it, open **Settings → Pages** in your GitHub
repository, choose **Deploy from a branch**, then select your branch and the `/docs` folder. The site
will be served at `https://<user>.github.io/rest-tester/`.

If you fork the project or use a custom domain, replace the base URL in `docs/` first.
[`docs/images/README.md`](docs/images/README.md) lists every place it appears, along with the
screenshot and SEO conventions.

The [AI guide](docs/ai.html) is a dedicated page linked from every site's main navigation.
Its downloadable handbook is published inside `docs/guides/` so GitHub Pages can serve it.
The site content is updated locally; publishing still requires your normal commit/push and
Pages deployment.

To refresh documentation assets safely:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m tools.capture_documentation
python -m tools.update_website_assets --date 2026-10-03
python -m pytest tests/test_website.py -n 4 --dist loadfile
```

The capture tool uses temporary settings/databases, the demo catalog and a fake provider.
It never reads real AI usage, sends test requests or calls a model. The metadata tool updates
image dimensions and the sitemap, and copies the implementation PDF into the published site.
After rebuilding the handbook, rerun the metadata tool to refresh that copy.

---

## Project structure

```text
rest-tester/
├── run.py                     # launches Rest Tester
├── run_builder.py             # launches the Catalog Builder on its own
├── api_tester/                # application package
│   ├── main.py                #   startup, splash, main window
│   ├── catalog.py             #   catalog model and loader
│   ├── schema_editor.py       #   schema-driven query / sort / payload editors
│   ├── widgets.py, theme.py   #   shared widgets and light/dark themes
│   ├── about.py, branding.py  #   About dialog, name, version, license
│   ├── authentication.py      #   managed authentication providers
│   ├── execution/             #   HTTP execution and timing
│   ├── scanners/              #   source scanners used by the catalog generator
│   ├── data_runner/           #   Data Runner workspace
│   ├── load_testing/          #   Load Studio workspace
│   ├── ai/                    #   Ask AI: LLM providers, catalog retrieval, plan validation, dialog
│   └── assets/                #   icons and bundled license text
├── tools/                     # catalog generator, schema extractor, icon builder
├── examples/                  # demo Store API server and catalog
├── suites/                    # saved test suites (JSON)
├── data/                      # settings, local database, default catalog
├── docs/                      # project website (GitHub Pages)
│   └── images/                #   screenshots shared by the README and the website
├── tests/                     # pytest suite
├── RestTester.spec            # PyInstaller build definition
├── requirements.txt
└── requirements-dev.txt
```

---

## Contributing

Contributions are welcome! To get started:

1. **Fork** the repository and create a feature branch: `git checkout -b feature/my-change`.
2. Install the dev dependencies: `pip install -r requirements-dev.txt`.
3. Make your change and **add or update tests** under `tests/`.
4. Run the test suite (see [Running the tests](#running-the-tests)) and make sure it passes.
5. Open a pull request that describes the change. Include screenshots for UI changes.

Guidelines:

- Keep changes focused. One feature or fix per pull request.
- Follow the existing code style: type hints, small widgets and shared helpers from `widgets.py`.
- UI changes must work in both the light and dark themes and at narrow window widths.
- Use the demo Store server (`examples/demo_store_server.py`) to reproduce issues without a real backend.

Found a bug or have an idea? Please [open an issue](https://github.com/priyankdalal/rest-tester/issues).
Include your version, which you can copy from **? → About → System → Copy details**.

---

## License

Rest Tester is free software, licensed under the
**[GNU General Public License v3.0 or later](LICENSE)** (`GPL-3.0-or-later`).

The license is GPL because the application is built on **PyQt6**, which is distributed under the
GPL v3. You may use, study, share and modify Rest Tester. If you distribute modified versions, they
must be released under the same license.

### Third-party components

| Component | License |
|---|---|
| [PyQt6](https://www.riverbankcomputing.com/software/pyqt/) / Qt 6 | GPL-3.0 |
| [requests](https://requests.readthedocs.io/) | Apache-2.0 |
| [MSAL for Python](https://github.com/AzureAD/microsoft-authentication-library-for-python) and msal-extensions | MIT |
| [PyJWT](https://pyjwt.readthedocs.io/) | MIT |
| [cryptography](https://cryptography.io/) | Apache-2.0 OR BSD-3-Clause |

The full list, with the versions actually in use, is shown under **? → About → Licenses**.

<div align="center">
<sub>Made with ❤️ and PyQt6 · © 2026 Rest Tester contributors</sub>
</div>
