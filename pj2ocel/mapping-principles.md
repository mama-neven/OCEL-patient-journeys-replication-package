# Mapping principles: patient journey → OCEL 2.0

Each principle is a test applied to an element of the domain model; the examples are from two pilot patients, S31 and S01. The mapping itself is `mapping.yaml`: its `params` block holds the switches named under each principle, and its rules are tagged with the principle they implement. `pj2ocel.py` only executes the mapping.

## P0 One clock per log

A log has one time axis. The **journey log** is on occurrence time (when it happened to the patient); the **provenance log** is on knowledge time (when it was reported or revised). Never mix the two in one log.

- Revisions are knowledge changes: the journey log keeps the current version as a snapshot; the history goes to the provenance log (`AssertFact`, `ReviseFact`).
- S31: the old log stamped attribute values at disclosure time next to clinical-time events → 716 attribute changes outside the object's span. Now 0.
- parameter: `provenance_log`

## P1 Event

An element is an event iff it **happened**, has a **resolved time**, is **atomic** and **immutable**. Name it verb + object as a safe identifier (`StartTreatment`, `ReceiveDiagnosis`).

- Negated (14), intended (3) and temporally unresolved (15) facts are not events.
- A period gives a start event and, if its end is known, an end event.
- A time with an open start ("before 2023") is unresolved. A fact dated after it was reported is not an event either, and a period whose end comes before its start gets no end event; all are reported.
- parameters: `event_statuses`, `time_anchor` (`lower` | `mid` of the date bracket)

## P2 Object

An element is an object iff it is

1. a **perspective**: we want to project the log on it and follow its behaviour; its lifecycle has at least *k* events, measured on the cohort; or
2. a **carrier**: it holds what an event cannot: a non-occurrence, an unresolved time, or an endpoint of a relation.

Anything else with an empty lifecycle is not an object: make it an attribute or an O2O.

- Perspectives: Patient, PatientCondition, TreatmentCourse.
- A concept (a clinical term, a journey factor) is not an object: it has no history of its own. It is an attribute of the fact or milestone that names it (P4).
- parameters: `perspectives`, `min_lifecycle`, `fact_object_types` (`per_type` | `single`: one `ClinicalFact` type)

## P3 A change is an event on the object it changes

A change of an object is an event on that object, not a new object.

- A period fact (treatment, symptom, adverse event, hospital stay) carries its own end: `period_state` says whether it is ongoing, stopped, or changed (dose, schedule or severity changed, and a new period carries on), and `clinical_end` dates the end when the sources do. A stopped period ends with `End<Type>` (`EndTreatment`, `DischargeFromHospital`); a changed one with `Change<Type>` (`ChangeTreatment`). Both are events on the fact, so they reach its TreatmentCourse through `in_course`, and `EndTreatment` sets the course `status` to stopped.
- Without `clinical_end`, a stopped period ends at start + a stated `duration_days`; otherwise it gets no end event and is reported as undated.
- A disease status stays a change event on its PatientCondition: `ChangeDiseaseStatus` sets `disease_status`.
- parameter: `change_types` (`events_on_target` | `objects`)

## P4 Object attribute

A property of the object that can change in the world and holds over a period. Timestamp it at valid time. Corrections overwrite (snapshot), they are not changes. Promote it to an object if it exists independently or its behaviour is of interest.

- S31: `TreatmentCourse.status` active → stopped; `PatientCondition.disease_status` diagnosed → in_remission.
- Values without a valid time are stamped at the object's first event, or the epoch if it has none.
- parameter: `initial_time` (`first_event` | `epoch`)

## P5 Event attribute

Observed once, at the event; nothing later depends on it. Missing means absent, never a placeholder (`unknown`, `n/a`).
- Every fact event carries a `resource`: who reported the fact (`patient`, `caregiver`), or `document` for a fact taken from a document, whose author is not established.
- A milestone event takes the reporters of the facts whose start dates it.

## P6 O2O

A lasting link between objects that no observed event creates. The qualifier names it (`of_patient`, `in_journey`, `causes`, `followed_by`). It becomes an object only if it carries properties that vary.

- S31: relation basis takes 2 values, confidence 1 → plain O2O loses little.
- parameter: `relations` (`o2o` | `reified` as EventRelation objects)

## P7 E2O

Each event relates to its **subject** and, by propagating the subject's O2O, to the perspective objects (`concerns` Patient, `in_journey` PatientCondition, `in_course` TreatmentCourse).

- parameter: `perspectives`

## P8 Terms keep the code and the patient's words

A term is two attributes on the fact: the normalised code (`treatment_name`: `treatments.azathioprine`) and the patient's own words (`treatment_verbatim`: "Azathioprine"). A catch-all code (`*.other`) stays as it is; the verbatim says what it stands for.

- parameter: `normalize_verbatim` (lower-case and trim the patient's words)

## P9 Evidence

Evidence stays a reference on the fact (`evidence_refs`); its text stays out of the log by default.

- parameters: `evidence` (`attribute` | `none`), `evidence_text`

## P10 One log per cohort

All patients go into one journey log and one provenance log. A patient goes in whole or not at all: one without journey summaries (milestones, treatment courses) is left out and reported, so every patient offers the same perspectives. Every identifier carries the patient code (`pat:S31`, `ce:S31:<lineage>`, `e:S31:<fact>`). Patients are compared through shared attribute values (term codes, factor codes), not shared objects.

- Perspective tests (P2) and the cohort checks are computed on the merged log: patients reached per type, shared objects, objects in more than half the events, one fact in two journeys.

## What OCEL 2.0 cannot carry

- Pairing rules between event types (which relation may link which types).
- Date-bracket width and ordering within a bracket: only one timestamp per event (ties: 14 events on 2025-02-11).
- Bitemporality within one log: hence two logs (P0).
- Objects with no events: valid in OCEL 2.0, but pm4py drops them on import (59 of 139 in S31).
