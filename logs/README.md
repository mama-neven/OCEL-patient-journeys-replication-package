# OCEL logs of the synthetic cohort

Built by `pj2ocel/pj2ocel.py` with `pj2ocel/mapping.yaml`: 280 synthetic patients,
10,364 journey events, 30 activities and 17,706 objects. Every patient in the log
is complete; patients without journey summaries are left out whole (see
`cohort-quality.md`).

| File | What it is |
|---|---|
| `cohort-journey.ocel.json` | The main log, on occurrence time: what happened to the patient and when (treatment starts and ends, diagnoses, visits, milestones). Use this one for process mining. |
| `cohort-provenance.ocel.json` | The second log, on reporting time: when things were said or revised (messages, onboarding answers, documents, each fact asserted). A separate log because a log has one clock (principle P0). |
| `cohort-quality.md` | Readable checks: data checks, events per patient, events per object type against the perspective test, and what was not mapped and why. Start here. |
| `cohort-build-report.json` | The same checks as JSON, with the mapping parameters used. |

Timestamps carry the precision of the source (day, week, month, quarter or
year); each fact event states it in its `precision` attribute. Each event's
`resource` is who reported the fact: `patient`, `caregiver`, or `document`.
`[ORG]` marks an organisation name removed by anonymisation.
