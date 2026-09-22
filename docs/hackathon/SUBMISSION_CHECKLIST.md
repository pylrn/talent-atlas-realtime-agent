# Samsung PRISM Y2026 Submission Checklist

This sheet mirrors the Google Form and the official submission brief. Repository
artifacts are complete. Only the team/contact fields below must be supplied by
the submitter because they are not stored in source control.

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
| Source Code | Ready | `api/`, `pipeline/`, `db/`, `scripts/` |
| Presentation | Ready for team details | `docs/hackathon/submission/Talent_Atlas_PRISM_Y2026_Presentation.pptx`; replace the team/college/member placeholders |
| Video | Ready | `docs/hackathon/demo/Talent_Atlas_PRISM_Demo.mp4` (4:58.9, H.264/AAC) |
| AI Disclosure | Ready | `AI_DISCLOSURE.md` |
| README | Ready | Root `README.md` with setup, architecture, testing and limitations |
| APK | N/A | Browser application; no Android deliverable is used or required |
| SDK | N/A | Application prototype, not a distributed client SDK |
| Requirements | Ready | `requirements.txt` and `pyproject.toml` |
| Docker | Ready | `Dockerfile` and `docker-compose.yml` |
| TAG | Ready | `PRISM_GENAI_HACKATHON_Y2026` points to the packaged submission commit |

## Final pre-tag gate

Run these commands from a clean clone using Python 3.10-3.12:

```bash
python -m pip install -r requirements.txt
python scripts/benchmark_realtime_interruptions.py
python scripts/evaluate_realtime_agent.py
docker compose config
```

Then verify:

- Team and member placeholders have been removed from the final presentation.
- The final presentation follows Samsung's required sections: theme/project/team,
  problem, architecture, walkthrough, stack, impact, innovation, results,
  limitations and next steps.
- The checked-in demo video is at most five minutes and opens from the README.
- No `.env`, API key, database credential, private candidate record or private
  observability link is tracked.
- The repository is public or explicitly shared with the judging account.
- The final release tag points to the commit containing the deck and video link.

The packaged commit is tagged and pushed with:

```bash
git tag -a PRISM_GENAI_HACKATHON_Y2026 -m "Samsung PRISM GenAI Hackathon Y2026 submission"
git push origin PRISM_GENAI_HACKATHON_Y2026
```

## Official deadlines from the supplied brief

- Registration closes: **16 September 2026, 11:59 PM**
- Final submission closes: **25 September 2026, 11:59 PM**
- Demo video: **maximum five minutes**
- Team size: **up to four members from one college**
