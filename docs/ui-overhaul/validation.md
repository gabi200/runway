# Prototype validation
Date: 13 October 2026.

## Scope and result

The two isolated HTML prototypes were checked with the Playwright browser connector. The standard shell helper could not start in this environment, and the local Playwright skill helper lacked its Playwright module; filesystem/preview work used the Node REPL and browser validation used the already available connector. No application dependencies were installed.

- **90 responsive view checks passed:** nine destinations × five widths × two proposals.
- Widths checked: 320, 390, 768, 1024 and 1440 CSS pixels.
- Each view had a visible title and no document-level horizontal overflow.
- Both comparison preview images loaded, and the comparison page reflowed at phone width.
- Opening the Streamlit design study directly through a local file URL loaded the UI without a server.
- No JavaScript page errors occurred during the complete interaction run.
- Desktop, phone and approval screenshots were captured and visually inspected. After increasing narrow chart label size, both phone renders were rechecked with four visible week labels and no overflow.

## Interaction checks passed in both proposals

- Financing review opens with exact terms and required roles.
- Recording Owner approval keeps Accountant approval required.
- Repeat Owner approval is disabled in the mockup.
- Pausing the agent disables approval; resuming restores the local interaction.
- Payment-move scenario shows week 6 at −€6,303.32 and preserves the week-10 gap.
- Selecting week 10 changes the displayed source breakdown.
- Flag inspection says linked payments remain blocked.
- Escape closes the document dialog.
- Reject confirmation updates History.
- Loading, connection-error and first-run previews are reachable.
- Import preview requires both selected CSV files.
- Agent pause is reachable and works through the phone Menu.
- Proposal B's modal retains keyboard focus after Tab, and Escape closes review.

These checks prove prototype behavior only. The browser is not enforcing Runway's real permissions; the real engine was never invoked.

## Data and numerical checks

The 13 baseline points and source lines were extracted from the first `get_forecast` JSON response in `docs/execution_logs/demo_run.md`. The chart is not an invented series. The optional scenario locally shifts the recorded €12,000 payment from week 6 to week 8; it is illustrative and is never written to the application.

Sample approval state is in-memory only. The test inbox and audit timeline are labelled illustrative output/sequence rather than a live financial state.

## Contrast checks

Twenty explicit token pairings were calculated with the WCAG relative-luminance formula. All tested text pairs exceeded 4.5:1; control boundaries exceeded 3:1.

| Pair | Ratio |
| --- | ---: |
| A body on canvas | 16.19 |
| A muted text on panel | 8.29 |
| A muted text on risk surface | 7.17 |
| A white text on primary button | 6.14 |
| A evidence accent on panel | 9.18 |
| A warning text on warning surface | 8.41 |
| A danger text on risk surface | 8.19 |
| A success text on success surface | 7.91 |
| A control boundary on panel | 3.75 |
| B body on white | 14.42 |
| B muted text on canvas | 5.57 |
| B white text on primary button | 7.20 |
| B evidence accent on white | 7.20 |
| B warning text on warning surface | 6.39 |
| B danger text on risk surface | 6.31 |
| B success text on success surface | 6.57 |
| B control boundary on canvas | 3.45 |
| B header text | 12.91 |
| B header muted text | 8.26 |
| B header button text | 9.03 |

This is a targeted contrast and interaction check, not a complete WCAG conformance audit.

## Change boundary

The working tree was clean before creating the proposals. SHA-256 checks of all **96 existing tracked files** found **zero changes**. Git reported only the new `docs/ui-overhaul/` directory.

No Runway application code, dependency lockfile, configuration, bank state or company data was modified. Canary was read as a reference only.

## Not checked

- The Python test suite was not rerun: no Python application implementation changed.
- No Streamlit redesign or React application was built; these are design studies.
- No live API, real model, actual bank mutation, email send or import was exercised.
- No real-device, screen-reader, complete keyboard traversal or browser-matrix acceptance was performed.
- Production session/security behavior and migration effort remain implementation work; the timing estimates are judgment, not benchmarks.
