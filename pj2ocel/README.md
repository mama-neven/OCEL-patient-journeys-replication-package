# pj2ocel

Builds OCEL 2.0 event logs from patient-journey exports. The mapping is a file, not code.

| File | What it is |
|---|---|
| `mapping.yaml` | The mapping: what becomes an object, event, attribute or relation, with ids, activity names, timestamps and qualifiers. Its `params` block holds the modelling decisions. |
| `mapping.json` | The same mapping as JSON, for machines without PyYAML. After editing the YAML, regenerate it: `python3 -c "import json,yaml; json.dump(yaml.safe_load(open('mapping.yaml')), open('mapping.json','w'), indent=1)"` |
| `pj2ocel.py` | The engine. It executes a mapping file and knows nothing about patient journeys. Python 3.8 or newer (tested with 3.10 and 3.13), standard library only (PyYAML to read `.yaml`). |
| `mapping-principles.md` | The principles P0–P10 behind the mapping; rules in `mapping.yaml` are tagged with them. |

## Run

```
python3 pj2ocel.py INPUT [INPUT ...] [--out DIR] [--per-patient]
```

- INPUT is a patient-journey export: a JSON file `{"patients": [...]}`.
- All patients of all inputs go into one cohort log. `--per-patient` also writes one log per patient.
- Change a decision without editing the mapping: `--set time_anchor=mid`, or `--params my-params.json`.
- Use another mapping: `--mapping my-mapping.yaml`.

Output, per run: `<name>-journey.ocel.json` (occurrence time), `<name>-provenance.ocel.json` (reporting time), `<name>-quality.md` (quality and cohort checks, and what could not be mapped) and `<name>-build-report.json`.

## Input the mapping reads

One record per patient, with these tables (field names as in the export):

- facts: `events_active`, `evidence`, `edges`, `supersessions` (optional)
- journey summaries: `gold_episodes`, `gold_anchors`, `gold_transitions`, `gold_labels`, `characteristics`
- sources: `marts_attributes`, `marts_messages`, `marts_onboarding`, `marts_documents`

A missing table is treated as empty. Milestone factors may be a list of `{code, kind}` or a string `code (kind), ...`.

## Reference output

`../logs/` holds the cohort logs and their quality report. A rerun on the same export gives the same sizes as `../logs/cohort-quality.md`.

## Privacy

The journey log holds sex, birth year, country and the patient's own words (`*_verbatim`). Quoted evidence text is off by default (`evidence_text: false`).
