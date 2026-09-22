# Samsung PRISM Y2026 Submission Checklist

This sheet mirrors the Google Form and the official submission brief. Replace
every `<...>` value before submitting.

## Google Form fields

| Field | Value to enter |
|---|---|
| Team Name | `<CollegeName>_<TeamName>_Theme05` |
| Theme Selected | `Theme 05 — Interruptible Real-Time Agents` |
| Primary Member's Name | `<required>` |
| Primary Member's Contact | `<required>` |
| Primary Member's Email ID | `<required>` |
| College | `<choose exactly one: SRM / VITV / MSRIT / Thapar>` |
| GitHub Link | `https://github.com/pylrn/talent-atlas-realtime-agent` |

Samsung's briefing slide says the registration team name is
`CollegeName_TeamName`; the submission form shown to the team asks for
`CollegeName_Teamname_ThemeNo.`. Use the more specific submission-form value
above unless the form validation says otherwise.

## GitHub checklist

| Item | Status | Repository evidence / action |
|---|---|---|
| Source Code | Ready | `api/`, `pipeline/`, `db/`, `scripts/`, `tests/` |
| Presentation | Draft present | `docs/hackathon/submission/CollegeName_TeamName_Submission_ppt/`; replace placeholders and export the final PPT/PDF |
| Video | Pending | Record a maximum five-minute demo; add the YouTube/Drive link to this file and the README |
| AI Disclosure | Ready | `AI_DISCLOSURE.md` |
| README | Ready | Root `README.md` with setup, architecture, testing and limitations |
| APK | N/A | Browser application; no Android deliverable is used or required |
| SDK | N/A | Application prototype, not a distributed client SDK |
| Requirements | Ready | `requirements.txt` and `pyproject.toml` |
| Docker | Ready | `Dockerfile` and `docker-compose.yml` |
| TAG | Pending final artifacts | Create `PRISM_GENAI_HACKATHON_Y2026` only on the actual final commit |

## Final pre-tag gate

Run these commands from a clean clone using Python 3.10–3.12:

```bash
python -m pip install -r requirements.txt
python -m pip install -e ".[dev]"
python -m pytest -q
python scripts/benchmark_realtime_interruptions.py
python scripts/evaluate_realtime_agent.py
docker compose config
```

Then verify:

- Team and member placeholders have been removed from the final presentation.
- The final presentation follows Samsung's required sections: theme/project/team,
  problem, architecture, walkthrough, stack, impact, innovation, results,
  limitations and next steps.
- The demo video is at most five minutes and its link is accessible without the
  submitter's account.
- No `.env`, API key, database credential, private candidate record or private
  observability link is tracked.
- The repository is public or explicitly shared with the judging account.
- The final release tag points to the commit containing the deck and video link.

Create and push the judging tag only after those checks:

```bash
git tag -a PRISM_GENAI_HACKATHON_Y2026 -m "Samsung PRISM GenAI Hackathon Y2026 submission"
git push origin PRISM_GENAI_HACKATHON_Y2026
```

## Official deadlines from the supplied brief

- Registration closes: **16 September 2026, 11:59 PM**
- Final submission closes: **25 September 2026, 11:59 PM**
- Demo video: **maximum five minutes**
- Team size: **up to four members from one college**
