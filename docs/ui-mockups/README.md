# Runway UI proposal

Mockups only. The application, authentication, policy engine and database workflows have not been changed.

Open the preview using a local server:

```bash
.venv/bin/python -m http.server 8765 --bind 127.0.0.1 --directory docs/ui-mockups
```

Visit http://localhost:8765. Navigation switches between the copilot, forecast and approval mockups. The top button switches between light and dark treatments. Chat submission, week selection, scenario comparison, approval decisions and the session dialog demonstrate preview states only.

## Recommended direction

A light financial workspace with warm neutral surfaces, graphite text and a muted green accent. Dark mode uses the same hierarchy and layout; it is an alternative treatment for review. A denser terminal-style dashboard was considered, but the owner needs to understand a financing decision more than monitor market ticks.

The chat supports a readable conversation beside a compact cash context panel. The forecast prioritizes the question of when cash goes below zero, followed by source inspection. Approvals put the decision, financial impact, reason and required reviewers ahead of raw tool arguments. Secondary pages retain access to the existing workflows; their redesign follows the approved visual system.

Reference patterns: [Claude's conversation and artifact workspace](https://www.anthropic.com/news/artifacts), and [Stripe's financial dashboard charts](https://support.stripe.com/questions/dashboard-home-page-charts-for-business-insights). These inform structure, rather than copying their branding.

## Visual choices

- System sans-serif type for readable financial detail and an accurate browser preview without external font dependencies. Tabular numbers keep amounts aligned.
- Green identifies the copilot, baseline cash series and primary decisions. Text labels identify all warning and approval states independently of color.
- Amber marks review requirements; red identifies negative cash balances. No glowing statuses, decorative gradients or marketing badges.
- Small radii for controls, larger radii for conversation surfaces. Borders separate information; a restrained composer shadow identifies the input surface.
- Different layouts reflect the job on each screen. The mobile view puts navigation above the conversation, moves cash context below it and keeps wide tables within their own scrolling container.
- ENERGY 2 / RHYTHM 3 / MOTION 1: restrained character, layouts shaped by content, interaction feedback without continuous animation.

## Data and scope

`sample-forecast.json` is a snapshot computed from the existing generated demo ledger on simulated 19 October 2026. The displayed baseline preserves all 13 weeks and the week 6/week 10 shortfalls. Illustrative approvals and the sample conversation come from the README demo story; they are not a representation of pending requests in the current database.

The proposed implementation retains Streamlit and the existing Python financial and approval functions. Main application changes would be in `ui/app.py`, `ui/style.css` and `.streamlit/config.toml`. The chart treatment would use the existing Altair dependency, and raw tool traces would move into collapsed disclosure controls. Authentication, four-eyes approval, audit logging, imports, document flags and reset confirmation retain their current behavior.

Before implementation, review the light/dark choice, the split chat layout and the approval hierarchy. Implementation starts after approval of this concrete design, as required by the brainstorming skill.

## Preview validation

- Visually inspected the desktop copilot in both themes, the forecast, approvals and the 390px mobile layouts. Screenshots are saved alongside this file.
- Checked all three screens at 320px and 768px widths; no page-level horizontal overflow. The financial table keeps its own scroll area when needed.
- Verified week selection, an empty week, scenario comparison/clear, sample chat, approval feedback, mobile session access and Escape dismissal of the dialog.
- Checked keyboard focus and mobile controls against the 44px target height.
- Verified the main text pairings against 4.5:1 and control/chart contrast against 3:1 in both themes using the antislop contrast checker.
- JavaScript syntax check passed. Application tests were not run because this stage changes only standalone mockup artifacts.

This is a design review, not a full accessibility certification or live workflow test. Implementation will need Streamlit smoke tests and browser checks against the actual financial and approval flows.
