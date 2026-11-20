# ocel-patient-journeys

An OCEL 2.0 event log of synthetic patient journeys in myasthenia gravis, and the
declarative mapping that builds it from patient-journey data.

The patients are **synthetic**: their stories were generated from sampled journey
specifications and processed by the same extraction pipeline as real patient
data. No real patient data is included.

## Contents

| Path | What it is |
|---|---|
| `logs/` | The event logs: 280 patients, 10,364 journey events, 30 activities. See `logs/README.md`. |
| `pj2ocel/` | The mapping (`mapping.yaml`), its principles (`mapping-principles.md`) and the engine that executes it (`pj2ocel.py`). |
| `evaluation.ipynb`, `evaluation_crosscheck.py`, `evaluation-results.json` | The paper's evaluation, computed from the logs. |
| `ocel_analysis.ipynb` | Exploratory analysis of the logs. |
| `paper/figures/` | Figures used in the paper. |

The input the logs were built from is not included: it holds identifiers of the
system that produced it. The mapping documents every step from that input to the
logs.

## Evaluation

The notebooks draw process graphs with Graphviz; install its `dot` program first (for example `brew install graphviz` or `apt install graphviz`).

`evaluation.ipynb` computes every number of the paper's evaluation from the
cohort logs. Each result is computed by two independent routes and recorded only
if they agree; `evaluation_crosscheck.py` recomputes the headline results in plain
Python as a third check.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-evaluation.txt
.venv/bin/python -m ipykernel install --user --name ocel-patient-journeys --display-name "Python (ocel-patient-journeys)"
.venv/bin/jupyter nbconvert --to notebook --execute --inplace evaluation.ipynb
python3 evaluation_crosscheck.py
```

The notebook pins its inputs by SHA-256 hash and stops if a log has changed.
`requirements-evaluation.txt` pins the exact environment (Python 3.14.6).

## Analysis notebook

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-notebook.txt
.venv/bin/python -m ipykernel install --user --name ocel-patient-journeys --display-name "Python (ocel-patient-journeys)"
```

Open `ocel_analysis.ipynb` and select **Python (ocel-patient-journeys)** as its kernel.

## Licence

Copyright 2026 mama health.

- The event logs (`logs/`) and figures (`paper/figures/`) are licensed under
  [CC BY 4.0](LICENSE-data).
- The code (`pj2ocel/`, the notebooks and `evaluation_crosscheck.py`) is licensed
  under the [Apache License 2.0](LICENSE).
