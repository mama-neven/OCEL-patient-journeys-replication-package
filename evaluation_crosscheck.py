"""Independent re-computation of the headline results of evaluation.ipynb.

Plain Python (json, datetime, statistics): no pandas, no PM4Py, no code shared with the notebook.
Run it after the notebook, from the repository root:

    python3 evaluation_crosscheck.py

It recomputes the headline results from the logs (and, where the logs cannot tell, from the per-patient
columns of logs/cohort-quality.md), compares each with evaluation-results.json, and exits with
status 1 if any differs.
"""
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

root = sys.argv[1] if len(sys.argv) > 1 else "."
J = json.load(open(f"{root}/logs/cohort-journey.ocel.json"))
PV = json.load(open(f"{root}/logs/cohort-provenance.ocel.json"))
R = {r["id"]: r["value"] for r in json.load(open(f"{root}/evaluation-results.json"))["results"]}


def t(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def last_attrs(o):
    d = {}
    for a in sorted(o.get("attributes", []), key=lambda a: a.get("time", "")):
        d[a["name"]] = a["value"]
    return d


def median(xs):
    return statistics.median(xs)


def quantile(xs, q):  # linear interpolation, as pandas
    xs = sorted(xs)
    pos = (len(xs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


out = []


def check(rid, mine, digits=None):
    v = round(mine, digits) if digits is not None else mine
    out.append((rid, R.get(rid), v, R.get(rid) == v))


types = {o["id"]: o["type"] for o in J["objects"]}
A = {o["id"]: last_attrs(o) for o in J["objects"]}
events = J["events"]
fact_events = [e for e in events if not e["type"].startswith("Reach")]
rels = lambda x, q: [r["objectId"] for r in x.get("relationships", []) if r["qualifier"] == q]

# --- fact categories -------------------------------------------------------------------------
fact_of = {}
for e in fact_events:
    rec = rels(e, "recorded_as")
    sub = rels(e, "subject")[0]
    fact_of[e["id"]] = rec[0] if rec else (sub if sub.startswith("ce:") else None)
start_ids = [e["id"] for e in fact_events if len(e["id"].split(":")) == 3 or e["id"].endswith(":start")]
with_start = {fact_of[i] for i in start_ids if fact_of[i]}
pure = sum(1 for i in start_ids if fact_of[i] is None)
facts = [o for o in J["objects"] if o["id"].startswith("ce:")]
disc = {}
for e in PV["events"]:
    if e["type"] in ("AssertFact", "ReviseFact"):
        disc[rels(e, "subject")[0]] = e["time"]
cat = Counter()
for o in facts:
    a = A[o["id"]]
    if o["id"] in with_start:
        cat["event"] += 1
    elif a["status"] != "occurred":
        cat[a["status"]] += 1
    elif not a.get("clinical_from"):
        cat["unresolved"] += 1
    elif a["clinical_from"] > disc[o["id"]][:19]:
        cat["after"] += 1
    else:
        cat["UNEXPLAINED"] += 1
check("RQ1.facts_with_event", cat["event"])
check("RQ1.facts_change_only", pure)
check("RQ1.facts_negated", cat["negated"])
check("RQ1.facts_intended", cat["intended"])
check("RQ1.facts_unresolved", cat["unresolved"])
check("RQ1.facts_after_report", cat["after"])
assert cat["UNEXPLAINED"] == 0

# --- traceability: id join to the assertion, which quotes an existing source ----------------
ptype = {o["id"]: o["type"] for o in PV["objects"]}
quotes_by_short = {}
for e in PV["events"]:
    if e["type"] in ("AssertFact", "ReviseFact"):
        _, p, _, short = e["id"].split(":")
        quotes_by_short[(p, short)] = [q for q in rels(e, "quotes") if ptype.get(q) in
                                       ("Message", "Document", "OnboardingAnswer", "OnboardingSession")]
traceable = sum(1 for e in fact_events if quotes_by_short.get(tuple(e["id"].split(":")[1:3])))
check("RQ2.R1.traceable_events", traceable)

# --- reporting delay -----------------------------------------------------------------------
delays = []
for e in fact_events:
    rep = next(a["value"] for a in e["attributes"] if a["name"] == "reported_at")
    delays.append((t(rep) - t(e["time"])).days / 365.25)
check("RQ2.R1.delay_median_years", median(delays), 1)
check("RQ2.R1.delay_over_1y_pct", 100 * sum(d > 1 for d in delays) / len(delays), 1)

# --- tied directly-follows pairs, Patient perspective, fact events --------------------------
seq = defaultdict(list)
for i, e in enumerate(events):
    if e["type"].startswith("Reach"):
        continue
    seq[rels(e, "concerns")[0]].append((t(e["time"]), i))
pairs = tied = 0
for s in seq.values():
    s.sort()
    for a, b in zip(s, s[1:]):
        pairs += 1
        tied += a[0] == b[0]
check("RQ2.R2.tied_pairs_Patient_pct", 100 * tied / pairs, 1)

# --- DF edges per perspective (ties in file order) ------------------------------------------
def edges(event_filter, prefix, qualifiers):
    s = defaultdict(list)
    for i, e in enumerate(events):
        if not event_filter(e):
            continue
        seen = set()
        for r in e["relationships"]:
            if r["objectId"].startswith(prefix) and r["qualifier"] in qualifiers and r["objectId"] not in seen:
                seen.add(r["objectId"])
                s[r["objectId"]].append((t(e["time"]), i, e["type"]))
    out_ = set()
    for x in s.values():
        x.sort()
        out_ |= {(a[2], b[2]) for a, b in zip(x, x[1:])}
    return len(out_)


is_fact = lambda e: not e["type"].startswith("Reach")
check("RQ2.R5.dfg_edges_Patient", edges(is_fact, "pat:", {"concerns"}))
check("RQ2.R5.dfg_edges_PatientCondition", edges(is_fact, "pc:", {"subject", "in_journey"}))
check("RQ2.R5.dfg_edges_TreatmentCourse", edges(is_fact, "tc:", {"in_course"}))
check("RQ2.R5.dfg_edges_Patient_milestones", edges(lambda e: not is_fact(e), "pat:", {"concerns"}))

# --- terminology ----------------------------------------------------------------------------
TERM = {"Treatment": "treatment_name", "Symptom": "symptom_name", "Diagnosis": "condition_name",
        "DiagnosticProcedure": "test_name", "AdverseEvent": "experience", "HcpVisit": "hcp_specialty"}
coded = specific = 0
for e in fact_events:
    f = fact_of[e["id"]]
    if f and types[f] in TERM:
        coded += 1
        code = A[f].get(TERM[types[f]])
        specific += bool(code) and not re.search(r"\.other(_[a-z0-9_]+)?$", code)
check("RQ2.R3.coded_events", coded)
check("RQ2.R3.specific_pct", 100 * specific / coded, 1)

# --- causes order ---------------------------------------------------------------------------
first = second = later_beyond = 0
for o in J["objects"]:
    for r in o.get("relationships", []):
        if r["qualifier"] != "causes":
            continue
        s, g = o["id"], r["objectId"]
        if s in with_start and g in with_start:
            first += A[s]["clinical_from"] <= A[g]["clinical_from"]
            second += 1
check("RQ2.R6.causes_both_dated", second)
check("RQ2.R6.causes_source_first_pct", 100 * first / second, 1)

# --- journeys per fact event ------------------------------------------------------------------
none = two = 0
for e in fact_events:
    pcs = {r["objectId"] for r in e["relationships"] if r["objectId"].startswith("pc:")}
    none += len(pcs) == 0
    two += len(pcs) > 1
check("RQ3.fact_events_no_journey_pct", 100 * none / len(fact_events), 1)
check("RQ3.fact_events_two_journeys", two)

# --- concurrency (lower and upper bound) and interleaving -----------------------------------
course_ev = []
for e in fact_events:
    c = rels(e, "in_course")
    if c:
        course_ev.append((c[0], rels(e, "concerns")[0], e["type"], t(e["time"])))
report_date = defaultdict(lambda: None)
for e in fact_events:
    p = rels(e, "concerns")[0]
    rd = t(next(a["value"] for a in e["attributes"] if a["name"] == "reported_at"))
    report_date[p] = rd if report_date[p] is None or rd > report_date[p] else report_date[p]
by_course = defaultdict(list)
for c, p, act, tm in course_ev:
    by_course[c].append((tm, act, p))
span = {}
for c, evs in by_course.items():
    starts = [tm for tm, act, _ in evs if act == "StartTreatment"]
    if not starts:
        continue
    last = max(tm for tm, _, _ in evs)
    stopped = any(act == "EndTreatment" and tm == last for tm, act, _ in evs)
    p = evs[0][2]
    span[c] = (p, min(starts), last, last if stopped else max(last, report_date[p]))
lower = upper = 0
for c, p, act, tm in course_ev:
    others = [v for k, v in span.items() if k != c and v[0] == p and v[1] <= tm]
    lower += any(v[2] >= tm for v in others)
    upper += any(v[3] >= tm for v in others)
check("RQ3.concurrent_lower_pct", 100 * lower / len(course_ev), 1)
check("RQ3.concurrent_upper_pct", 100 * upper / len(course_ev), 1)
pat_courses = defaultdict(lambda: defaultdict(list))
for c, p, act, tm in course_ev:
    pat_courses[p][c].append(tm)
multi = {p: cs for p, cs in pat_courses.items() if len(cs) > 1}
inter = sum(1 for cs in multi.values()
            if any(min(ts) < x < max(ts) for c, ts in cs.items() for d, us in cs.items() if d != c for x in us))
check("RQ3.patients_two_courses", len(multi))
check("RQ3.patients_interleaved", inter)

# --- diagnostic delay -------------------------------------------------------------------------
ms = defaultdict(dict)
for e in events:
    if e["type"].startswith("Reach"):
        ms[rels(e, "concerns")[0]][e["type"]] = t(e["time"])
gaps, yes, no = [], [], []
for p, d in ms.items():
    if "ReachSymptomOnset" in d and "ReachDiagnosis" in d:
        g = (d["ReachDiagnosis"] - d["ReachSymptomOnset"]).days / (365.25 / 12)
        gaps.append(g)
        (yes if "ReachMisdiagnosis" in d else no).append(g)
check("RQ4.symptom_onset_to_diagnosis_patients", len(gaps))
check("RQ4.symptom_onset_to_diagnosis_median_months", median(gaps), 1)
check("RQ4.symptom_onset_to_diagnosis_q3_months", quantile(gaps, 0.75), 1)
check("RQ4.misdiagnosis_yes_median_months", median(yes), 1)
check("RQ4.misdiagnosis_no_median_months", median(no), 1)

# --- durations --------------------------------------------------------------------------------
ended = {fact_of[e["id"]] for e in fact_events if e["id"].endswith((":end", ":change"))}
dur = [o["id"] for o in facts if {"duration_min_days", "duration_max_days"} & set(A[o["id"]])]
check("RQ5.facts_with_duration", len(dur))
check("RQ5.duration_no_end_event", sum(1 for f in dur if A[f]["status"] == "occurred" and f not in ended))

# --- every fact, with or without an event ----------------------------------------------------
n_facts = len(facts) + pure
check("RQ1.fact_records", n_facts)
check("RQ1.facts_without_event_pct", 100 * (n_facts - cat["event"] - pure) / n_facts, 1)

# --- what PM4Py leaves out --------------------------------------------------------------------
referenced = {r["objectId"] for e in events for r in e["relationships"]}
check("RQ1.objects_without_event", sum(1 for o in J["objects"] if o["id"] not in referenced))
o2o_all = [(o["id"], r["objectId"], r["qualifier"]) for o in J["objects"] for r in o.get("relationships", [])]
check("RQ1.o2o_not_in_pm4py", sum(1 for s_, g, _ in o2o_all if s_ not in referenced or g not in referenced))

# --- milestones: not reached and unknown ------------------------------------------------------
milestones = [A[o["id"]] for o in J["objects"] if o["type"] == "Milestone"]
n_pat = sum(1 for o in J["objects"] if o["type"] == "Patient")
check("RQ2.R4.milestones_not_reached", sum(1 for m in milestones if m.get("presence") == "not_reached"))
check("RQ2.R4.milestones_unknown", n_pat * len({m["milestone_type"] for m in milestones}) - len(milestones))

# --- terms: catch-all and wording -------------------------------------------------------------
WORDS = {"Treatment": "treatment_verbatim", "Symptom": "symptom_verbatim", "Diagnosis": "condition_verbatim",
         "DiagnosticProcedure": "test_verbatim", "AdverseEvent": "experience_verbatim", "HcpVisit": "hcp_specialty_verbatim"}
catch_all = words_kept = 0
for e in fact_events:
    f = fact_of[e["id"]]
    if f and types[f] in TERM:
        code = A[f].get(TERM[types[f]])
        catch_all += bool(code) and bool(re.search(r"\.other(_[a-z0-9_]+)?$", code))
        w_ = A[f].get(WORDS[types[f]])
        words_kept += isinstance(w_, str) and w_.strip() != ""
check("RQ2.R3.catch_all_pct", 100 * catch_all / coded, 1)
check("RQ2.R3.words_kept_pct", 100 * words_kept / coded, 1)

# --- relations between facts ------------------------------------------------------------------
STRUCTURAL = {"in_journey", "of_patient", "in_course", "followed_by"}
fact_rel = [(s_, g, q) for s_, g, q in o2o_all if q not in STRUCTURAL]
check("RQ2.R6.relations", len(fact_rel))
check("RQ2.R6.visible_pct", 100 * sum(1 for s_, g, _ in fact_rel if s_ in referenced and g in referenced) / len(fact_rel), 1)
overlap = beyond = 0
for s_, g, q in fact_rel:
    if q == "causes" and s_ in with_start and g in with_start and A[s_]["clinical_from"] > A[g]["clinical_from"]:
        if A[s_]["clinical_from"] < A[g]["clinical_to"]:
            overlap += 1
        else:
            beyond += 1
check("RQ2.R6.causes_source_later_overlap_pct", 100 * overlap / second, 1)
check("RQ2.R6.causes_source_later_beyond_pct", 100 * beyond / second, 1)

# --- the patient as case; treatment events --------------------------------------------------------
check("RQ3.flat_patient_events", sum(1 for e in events if len(rels(e, "concerns")) == 1))
check("RQ3.course_events", len(course_ev))
check("RQ4.symptom_onset_to_diagnosis_q1_months", quantile(gaps, 0.25), 1)
check("RQ4.misdiagnosis_yes_patients", len(yes))
check("RQ4.misdiagnosis_no_patients", len(no))

# --- decisions beyond the principles: the per-patient columns of cohort-quality.md ------------------
# The logs do not carry period_state or the clinical end, so these are read from the engine's readable
# report. The notebook uses its Total column and the build report; here the per-patient columns are summed.
BR = json.load(open(f"{root}/logs/cohort-build-report.json"))
check("RQ1.patients_exported", len(BR["patients"]))
not_mapped = {}
for line in open(f"{root}/logs/cohort-quality.md").read().split("## Not mapped", 1)[1].splitlines():
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    if len(cells) > 2 and cells[1].isdigit():
        not_mapped[cells[0]] = sum(int(c) for c in cells[2:])
check("RQ5.periods_end_undated", not_mapped["no end event: period stopped, end undated"]
      + not_mapped["no end event: period changed, end undated"])
check("RQ5.ends_before_start", not_mapped["no end event: ends before it starts"])
check("RQ5.contradictory_dates", not_mapped["no end event: ends before it starts"] + cat["after"])
check("RQ5.duration_no_end_with_start",
      sum(1 for f in dur if A[f]["status"] == "occurred" and f not in ended and f in with_start))

# --- R2: precision and date range of the time of each event ------------------------------------------
# An end or change event is timed by the end of its period (end_precision, clinical_end_from/to).
coarse = ranged = 0
for e in events:
    a = {x["name"]: x["value"] for x in e.get("attributes", [])}
    is_end = e["id"].endswith((":end", ":change"))
    prec = a.get("end_precision") if is_end else a.get("precision")
    lo, hi = (a.get("clinical_end_from"), a.get("clinical_end_to")) if is_end else (a.get("clinical_from"), a.get("clinical_to"))
    coarse += prec != "day"
    ranged += bool(prec) and lo is not None and hi is not None
check("RQ2.R2.coarser_than_day_pct", 100 * coarse / len(events), 1)
check("RQ2.R2.events_with_range_pct", 100 * ranged / len(events), 1)

# --- the case-centric milestone journey -----------------------------------------------------------------
ms_seq = defaultdict(list)
for i, e in enumerate(events):
    if e["type"].startswith("Reach"):
        ms_seq[rels(e, "concerns")[0]].append((t(e["time"]), i, e["type"]))
check("CC.cases", len({rels(e, "concerns")[0] for e in events}))
check("CC.milestone_variants", len({tuple(x[2] for x in sorted(v)) for v in ms_seq.values()}))
check("CC.milestone_activities", len({e["type"] for e in events if e["type"].startswith("Reach")}))

# --- comorbidity: the journeys of conditions other than myasthenia gravis ---------------------------------
condition = {o["id"]: A[o["id"]].get("condition_code") for o in J["objects"] if o["type"] == "PatientCondition"}
como_j = {oid for oid, c in condition.items() if c != "conditions.myasthenia_gravis"}
como_ev = [e for e in events if {r["objectId"] for r in e["relationships"]} & como_j]
check("OC.patients_comorbidity", len({rels(e, "concerns")[0] for e in como_ev}))
check("OC.comorbidity_journeys", len({r["objectId"] for e in como_ev for r in e["relationships"] if r["objectId"] in como_j}))
check("OC.comorbidity_events", len(como_ev))
mg_dx = {rels(e, "concerns")[0]: t(e["time"]) for e in events if e["type"] == "ReachDiagnosis"}
cdx = [(rels(e, "concerns")[0], t(e["time"])) for e in como_ev if e["type"] == "ReceiveDiagnosis"]
check("OC.comorbidity_diagnoses", len(cdx))
cdx_dated = [(tm, mg_dx[p]) for p, tm in cdx if p in mg_dx]
check("OC.comorbidity_diagnoses_mg_dated", len(cdx_dated))
check("OC.comorbidity_diagnoses_before_mg", sum(a < b for a, b in cdx_dated))
check("OC.comorbidity_diagnoses_after_mg", sum(a > b for a, b in cdx_dated))
check("OC.comorbidity_diagnoses_same_time_mg", sum(a == b for a, b in cdx_dated))

# --- journeys and treatment courses: the OC-DFG of the figure ------------------------------------------
THREE = {"ReceiveDiagnosis", "StartTreatment", "EndTreatment"}


def seqs(prefix, qualifiers):
    out_ = defaultdict(list)
    for i, e in enumerate(events):
        if e["type"] not in THREE:
            continue
        for oid in {r["objectId"] for r in e["relationships"] if r["qualifier"] in qualifiers and r["objectId"].startswith(prefix)}:
            out_[oid].append((t(e["time"]), i, e["type"], e["id"]))
    return {k: sorted(v) for k, v in out_.items()}


def end_start(sq):
    return [(o, a[3], b[3]) for o, v in sq.items() for a, b in zip(v, v[1:]) if (a[2], b[2]) == ("EndTreatment", "StartTreatment")]


course_of = {e["id"]: rels(e, "in_course")[0] for e in events if rels(e, "in_course")}
j_seq = seqs("pc:", {"subject", "in_journey"})
j_pairs = end_start(j_seq)
check("OC.course_end_to_start", len(end_start(seqs("tc:", {"in_course"}))))
check("OC.journey_end_to_start", len(j_pairs))
check("OC.journeys_with_switch", len({o for o, _, _ in j_pairs}))
check("OC.switch_to_other_course", sum(1 for _, a, b in j_pairs if a in course_of and b in course_of and course_of[a] != course_of[b]))
check("OC.journeys_start_with_diagnosis", sum(1 for v in j_seq.values() if v[0][2] == "ReceiveDiagnosis"))
check("CC.patient_end_to_start", len(end_start(seqs("pat:", {"concerns"}))))

w = max(len(r[0]) for r in out)
for rid, nb, mine, ok in out:
    print(f"{rid:<{w}}  notebook={nb!s:>8}  independent={mine!s:>8}  {'OK' if ok else 'MISMATCH'}")
print(f"\n{sum(o[3] for o in out)} of {len(out)} agree")
sys.exit(0 if all(o[3] for o in out) else 1)
