#!/usr/bin/env python3
"""pj2ocel: build OCEL 2.0 logs from patient-journey exports, driven by a declarative mapping file.

The engine knows nothing about patient journeys. What becomes an object, an event, an attribute or a relation,
with which identifiers, activity names, timestamps and qualifiers, is written in the mapping file
(mapping.yaml, or mapping.json without PyYAML). Its `params` block holds the modelling decisions.

    python3 pj2ocel.py TRACE [TRACE ...] [--mapping FILE] [--params FILE] [--set KEY=VALUE ...]
                       [--out DIR] [--name NAME] [--per-patient] [--compare OLD_OCEL.json]

TRACE is a patient-journey export: a JSON file {"patients": [...]} or a list of patients. All patients of
all inputs go into ONE cohort log.

Writes, for <name> (default: the patient code for one patient, else "cohort"), one file per log in the
mapping (e.g. <name>-journey.ocel.json, <name>-provenance.ocel.json), plus
    <name>-quality.md         quality and cohort checks on the first log (and an old log, with --compare)
    <name>-build-report.json  parameters used, counts, and everything that could not be mapped, per patient
--per-patient also writes the same files for each patient on its own.
"""
import argparse, collections, copy, fnmatch, json, re, sys
from datetime import date, timedelta
from pathlib import Path

EPOCH = "1970-01-01T00:00:00Z"
SENTINELS = {"unknown", "n/a", "na", "-", "none", "null", ""}
TEMPLATE = re.compile(r"\{([^{}]+)\}")
OPS = ("fn", "def", "ref", "obj", "find", "index", "index1", "get", "lookup", "if", "switch", "agg", "with",
       "in_source", "test", "dict", "const")


class MappingError(Exception):
    pass


# ------------------------------------------------------------------------------------------- OCEL log
class Log:
    def __init__(self):
        self.objects, self.events = {}, []
        self.otypes, self.etypes = collections.defaultdict(dict), collections.defaultdict(dict)
        self.owner, self.meta = {}, {}          # id -> patient code (None for shared objects); object id -> source row

    @staticmethod
    def kind(v):
        return "boolean" if isinstance(v, bool) else "integer" if isinstance(v, int) else "float" if isinstance(v, float) else "string"

    def obj(self, oid, otype, owner, row=None):
        if oid not in self.objects:
            self.objects[oid] = {"id": oid, "type": otype, "attributes": [], "relationships": [], "_pending": {}}
            self.otypes.setdefault(otype, {})
            self.owner[oid], self.meta[oid] = owner, row
        return oid

    def set_attrs(self, oid, attrs, time=None):
        """time=None: a snapshot value, timestamped when the log is serialised (first event, or the epoch)."""
        o = self.objects[oid]
        for k, v in attrs.items():
            if v is None or v == [] or (isinstance(v, str) and v.strip().lower() in SENTINELS): continue
            if isinstance(v, list): v = ", ".join(map(str, v))      # a list of terms reads as one value
            elif isinstance(v, dict): v = json.dumps(v, ensure_ascii=False)
            self.otypes[o["type"]][k] = self.kind(v)
            if time is None: o["_pending"][k] = v
            else: o["attributes"].append({"name": k, "time": iso(time), "value": v})

    def o2o(self, a, b, q):
        if a in self.objects and b in self.objects and a != b:
            r = {"objectId": b, "qualifier": q}
            if r not in self.objects[a]["relationships"]: self.objects[a]["relationships"].append(r)

    def event(self, eid, etype, time, rels, attrs, owner):
        attrs = {k: v for k, v in attrs.items() if v is not None and not (isinstance(v, str) and v.strip().lower() in SENTINELS)}
        for k, v in attrs.items(): self.etypes[etype][k] = self.kind(v)
        self.etypes.setdefault(etype, {})
        seen, R = set(), []
        for oid, q in rels:
            if oid in self.objects and (oid, q) not in seen:
                seen.add((oid, q)); R.append({"objectId": oid, "qualifier": q})
        self.events.append({"id": eid, "type": etype, "time": iso(time),
                            "attributes": [{"name": k, "value": v} for k, v in attrs.items()], "relationships": R})
        self.owner[eid] = owner

    def serialise(self, initial_time):
        first = {}
        for e in self.events:
            for r in e["relationships"]:
                first[r["objectId"]] = min(first.get(r["objectId"], e["time"]), e["time"])
        out = []
        for o in self.objects.values():
            t = first.get(o["id"], EPOCH) if initial_time == "first_event" else EPOCH
            attrs = [{"name": k, "time": t, "value": v} for k, v in o["_pending"].items()] + o["attributes"]
            out.append({"id": o["id"], "type": o["type"], "attributes": sorted(attrs, key=lambda a: (a["time"], a["name"])),
                        "relationships": o["relationships"]})
        return {"objectTypes": [{"name": n, "attributes": [{"name": k, "type": v} for k, v in a.items()]} for n, a in sorted(self.otypes.items())],
                "eventTypes": [{"name": n, "attributes": [{"name": k, "type": v} for k, v in a.items()]} for n, a in sorted(self.etypes.items())],
                "objects": out, "events": sorted(self.events, key=lambda e: e["time"])}


# ------------------------------------------------------------------------------- helpers, filters, functions
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")  # every event time starts with a calendar date
def iso(d): return d if "T" in d else d + "T00:00:00Z"
def camel(s): return "".join(w.capitalize() for w in re.split(r"[_\s]+", s.lower()) if w)
def norm(s): return re.sub(r"\s+", " ", s).strip().lower() if isinstance(s, str) else s
def bracket(b):
    """A range like '[2023-01-01,2023-02-01)'; an open bound ('(,2023-01-01)': before a date) is None."""
    if not b: return None, None
    lo, hi = b.strip("[]()").split(",")
    return lo or None, hi or None
def _after(s, sep): return s.split(sep, 1)[1] if sep in s else None


FILTERS = {  # name -> fn(value, engine); a filter applied to None gives None
    "lower": lambda v, E: str(v).lower(), "upper": lambda v, E: str(v).upper(), "camel": lambda v, E: camel(str(v)),
    "norm": lambda v, E: norm(v), "verbatim": lambda v, E: norm(v) if E.params.get("normalize_verbatim") else v,
    "slug": lambda v, E: re.sub(r"[^a-z0-9]+", "_", str(v)).strip("_"),
    "before_dot": lambda v, E: str(v).split(".", 1)[0], "after_dot": lambda v, E: _after(str(v), "."),
    "before_colon": lambda v, E: str(v).split(":", 1)[0], "after_colon": lambda v, E: _after(str(v), ":"),
    "first8": lambda v, E: str(v)[:8], "first19": lambda v, E: str(v)[:19],
    "lo": lambda v, E: bracket(v)[0], "hi": lambda v, E: bracket(v)[1],
    "span_start": lambda v, E: int(str(v).strip("[)").split(",")[0]), "pad6": lambda v, E: f"{int(v):06d}",
    "json": lambda v, E: json.loads(v) if isinstance(v, str) and v else (v or {}),
    "len": lambda v, E: len(v), "join": lambda v, E: ", ".join(map(str, v)), "str": lambda v, E: str(v),
}


def _anchor(lo, hi, how):
    if lo is None: return None
    if how == "mid" and hi:
        a, b = date.fromisoformat(lo), date.fromisoformat(hi)
        return (a + (b - a) / 2).isoformat()
    return lo


FUNCTIONS = {
    "json": lambda s: json.loads(s) if isinstance(s, str) and s else (s or {}),
    "split": lambda s, sep=",": [x.strip() for x in (s or "").split(sep) if x.strip()],
    "len": lambda v: len(v) if v is not None else 0,
    "anchor": _anchor,
    "date_add": lambda d, n: (date.fromisoformat(d) + timedelta(days=n)).isoformat() if d is not None and n is not None else None,
    "days_between": lambda a, b: (date.fromisoformat(b) - date.fromisoformat(a)).days if a and b else None,
    "is_number": lambda v: isinstance(v, (int, float)),
    "coalesce": lambda *a: next((x for x in a if x is not None), None),
    "pair": lambda a, b: "/".join(sorted([a, b])),
    # input adapter: milestone factors are a list of {code, kind} or a string 'code (kind), code (kind)'
    "factors": lambda f: [{"code": a, "kind": b} for a, b in re.findall(r"\s*([^,()]+?)\s*\(([^)]+)\)", f)] if isinstance(f, str) else (f or []),
}


# --------------------------------------------------------------------------------------- evaluation context
class Ctx:
    """Names visible to expressions. `lazy` names are evaluated on first use, in the context that defines them."""
    def __init__(self, engine, parent=None, vals=None, lazy=None):
        self.E, self.parent, self.vals, self.lazy = engine, parent, dict(vals or {}), dict(lazy or {})

    def child(self, vals=None, lazy=None): return Ctx(self.E, self, vals, lazy)

    def get(self, k):
        c = self
        while c is not None:
            if k in c.vals: return c.vals[k]
            if k in c.lazy:
                expr = c.lazy.pop(k); c.vals[k] = None          # guard against self-reference
                c.vals[k] = c.E.eval(expr, c)
                return c.vals[k]
            c = c.parent
        raise MappingError(f"unknown name '{k}'")


# ------------------------------------------------------------------------------------------------- engine
class Engine:
    def __init__(self, spec, params):
        self.spec, self.params = spec, params
        self.vocab, self.defs = spec.get("vocab", {}), spec.get("defs", {})
        self.sources, self.index_spec = spec.get("sources", {}), spec.get("indexes", {})
        self.pid_key = spec.get("input", {}).get("patient_id", "code")

    # -------------------------------------------------------------- expressions
    def eval(self, e, ctx):
        if isinstance(e, str):
            if e.startswith("$"): return self.path(e[1:], ctx)
            if "{" in e: return TEMPLATE.sub(lambda m: "" if (v := self.path(m.group(1), ctx)) is None else str(v), e)
            return e
        if isinstance(e, list): return [self.eval(x, ctx) for x in e]
        if not isinstance(e, dict): return e
        op = next((k for k in OPS if k in e), None)
        if op is None: raise MappingError(f"expression without operator: {e}")
        ev = lambda x: self.eval(x, ctx)
        if op == "const": return e["const"]
        if op == "fn":
            if e["fn"] not in FUNCTIONS: raise MappingError(f"unknown function '{e['fn']}'")
            return FUNCTIONS[e["fn"]](*[ev(a) for a in e.get("args", [])])
        if op == "def":
            if e["def"] not in self.defs: raise MappingError(f"unknown def '{e['def']}'")
            return self.eval(self.defs[e["def"]], ctx)
        if op == "ref": return self.construct(e["ref"], {k: ev(v) for k, v in (e.get("args") or {}).items()}, ctx)
        if op == "obj":
            i = ev(e["obj"]); return i if i in self.log.objects else None
        if op == "find": return self.find(e, ctx)
        if op in ("index", "index1"):
            if e[op] not in self.indexes: raise MappingError(f"unknown index '{e[op]}'")
            hits = self.indexes[e[op]].get(ev(e["key"]), [])
            return hits if op == "index" else (hits[0] if hits else None)
        if op == "get":
            c, k = ev(e["get"]), ev(e["key"])
            if c is None or k is None: return None
            return c.get(k) if isinstance(c, dict) else (c[k] if isinstance(c, list) and isinstance(k, int) and k < len(c) else None)
        if op == "lookup":
            d, k = ev(e["lookup"]), ev(e["key"])
            v = d.get(k) if isinstance(d, dict) and k is not None else None
            return v if v is not None else ev(e.get("default"))
        if op == "if": return ev(e.get("then")) if self.test(e["if"], ctx) else ev(e.get("else"))
        if op == "switch":
            for case in e["switch"]:
                if self.test(case.get("when"), ctx): return ev(case.get("then"))
            return ev(e.get("default"))
        if op == "agg": return self.agg(e, ctx)
        if op == "with": return self.eval(e["expr"], ctx.child(lazy=e["with"]))
        if op == "in_source":
            row = ev(e["row"])
            return None if row is None else self.eval(e["expr"], self.row_ctx(e["in_source"], row, 0, self.base))
        if op == "test": return self.test(e["test"], ctx)
        if op == "dict": return {k: ev(v) for k, v in e["dict"].items()}

    def path(self, p, ctx):
        parts = p.split("|")
        segs = parts[0].strip().split(".")
        v = ctx.get(segs[0])
        for s in segs[1:]:
            if v is None: break
            v = v.get(s) if isinstance(v, dict) else (v[int(s)] if isinstance(v, list) and s.isdigit() and int(s) < len(v) else None)
        for f in parts[1:]:
            f = f.strip()
            if f not in FILTERS: raise MappingError(f"unknown filter '{f}'")
            if v is not None: v = FILTERS[f](v, self)
        return v

    def test(self, c, ctx):
        if c is None: return True
        if isinstance(c, list): return all(self.test(x, ctx) for x in c)
        if "all" in c: return all(self.test(x, ctx) for x in c["all"])
        if "any" in c: return any(self.test(x, ctx) for x in c["any"])
        if "not" in c: return not self.test(c["not"], ctx)
        if "v" not in c: raise MappingError(f"condition without 'v': {c}")
        v, ops = self.eval(c["v"], ctx), {k: x for k, x in c.items() if k != "v"}
        if not ops: return bool(v)
        for op, x in ops.items():
            o = self.eval(x, ctx)
            ok = {"eq": lambda: v == o, "ne": lambda: v != o, "in": lambda: v in (o or []), "not_in": lambda: v not in (o or []),
                  "contains": lambda: o in (v or []), "exists": lambda: (v not in (None, "", [], {})) == bool(o),
                  "matches": lambda: isinstance(v, str) and re.search(o, v) is not None,
                  "not_matches": lambda: not (isinstance(v, str) and re.search(o, v) is not None),
                  "gt": lambda: v is not None and v > o, "ge": lambda: v is not None and v >= o,
                  "lt": lambda: v is not None and v < o, "le": lambda: v is not None and v <= o}.get(op)
            if ok is None: raise MappingError(f"unknown condition operator '{op}'")
            if not ok(): return False
        return True

    def agg(self, e, ctx):
        vals = []
        for i, x in enumerate(self.eval(e["over"], ctx) or []):
            c = ctx.child({"item": x, "item_i": i})
            if not self.test(e.get("where"), c): continue
            v = self.eval(e.get("item", "$item"), c)
            if v is not None: vals.append(v)
        op = e["agg"]
        if op == "list": return vals
        if op == "unique": return list(dict.fromkeys(vals))
        if op == "unique_sorted": return sorted(set(vals))
        if op == "join": return e.get("sep", ", ").join(map(str, vals)) if vals else None
        if op == "count": return len(vals)
        if op == "count_distinct": return len(set(map(json.dumps, vals)))
        if op in ("min", "max"): return (min if op == "min" else max)(vals) if vals else self.eval(e.get("default"), ctx)
        if op == "first": return vals[0] if vals else None
        raise MappingError(f"unknown aggregate '{op}'")

    # -------------------------------------------------------------- objects
    def construct(self, otype, args, ctx):
        """object_types: create on first reference and return the id; a `drop` variant returns None (and may report)."""
        spec = self.L.get("object_types", {}).get(otype)
        if spec is None: raise MappingError(f"no object_types entry for '{otype}'")
        shared = spec.get("shared", False)
        key = (otype, json.dumps(args, sort_keys=True, default=str), None if shared else self.pid)
        if key in self.memo: return self.memo[key]
        c = ctx.child({"args": args}, spec.get("let"))
        for var in spec.get("variants") or [{}]:
            if not self.test(var.get("when"), c): continue
            if "drop" in var:
                d = var["drop"]
                if isinstance(d, dict):
                    tag = (d["label"], id(self._row_of(ctx)), key)    # once per calling row and key
                    if tag not in self.reported:
                        self.reported.add(tag); self.rep[d.get("kind", "unmapped")][self.eval(d["label"], c)] += 1
                return None
            oid = self.eval(var.get("id", spec.get("id")), c)
            if not oid: return None
            new = oid not in self.log.objects
            self.log.obj(oid, otype, None if shared else self.pid, args)
            self.memo[key] = oid
            if new:
                self.log.set_attrs(oid, self.attr_values([spec.get("attrs") or {}, var.get("attrs") or {}], c))
                self.apply_o2o(oid, (spec.get("o2o") or []) + (var.get("o2o") or []), c)
            return oid
        return None

    @staticmethod
    def _row_of(ctx):
        try: return ctx.get("row")
        except MappingError: return None

    def find(self, e, ctx):
        hits = []
        for oid, o in self.log.objects.items():
            if o["type"] != e["find"] or self.log.owner.get(oid) != self.pid: continue
            if self.test(e.get("where"), ctx.child({"cand": {"id": oid, "row": self.log.meta.get(oid)}})): hits.append(oid)
        if e.get("unique", True): return hits[0] if len(hits) == 1 else None
        return hits

    def attr_values(self, specs, ctx):
        """attrs: a dict, or a list of dicts / {use: attr_set} / {spread: expr, map, exclude, scalars_only}."""
        out = {}
        for s in (specs if isinstance(specs, list) else [specs]):
            if not s: continue
            if "use" in s: out.update(self.attr_values(self.L.get("attr_sets", {})[s["use"]], ctx)); continue
            if "spread" in s:
                excl = set(self.eval(s.get("exclude", []), ctx) or [])
                for k, v in (self.eval(s["spread"], ctx) or {}).items():
                    if k in excl or (s.get("scalars_only") and isinstance(v, (list, dict))): continue
                    for pat, flt in (s.get("map") or {}).items():
                        if fnmatch.fnmatch(k, pat) and v is not None: v = FILTERS[flt](v, self); break
                    out[k] = v
                continue
            for k, v in s.items(): out[self.eval(k, ctx)] = self.eval(v, ctx)
        return out

    def apply_o2o(self, oid, entries, ctx):
        for ent in entries:
            for c in self.expand(ent, ctx):
                a = self.eval(ent["from_object"], c) if "from_object" in ent else oid
                b = self.eval(ent["to"], c)
                if a and b: self.log.o2o(a, b, self.eval(ent["qualifier"], c))

    def expand(self, ent, ctx):
        """Contexts for an entry with optional `let`, `each` (binds item) and `where`."""
        c = ctx.child(lazy=ent["let"]) if ent.get("let") else ctx
        if "each" not in ent:
            if self.test(ent.get("where"), c): yield c
            return
        for i, x in enumerate(self.eval(ent["each"], c) or []):
            if x is None: continue
            ci = c.child({"item": x, "item_i": i})
            if self.test(ent.get("where"), ci): yield ci

    def e2o(self, entries, ctx):
        R = []
        for ent in entries:
            if "use" in ent: R += self.e2o(self.L.get("e2o_sets", {})[ent["use"]], ctx); continue
            for c in self.expand(ent, ctx):
                o = self.eval(ent["object"], c)
                if o: R.append((o, self.eval(ent["qualifier"], c)))
        return R

    # -------------------------------------------------------------- rows and indexes
    def row_ctx(self, source, row, i, base):
        return base.child({"row": row, "i": i}, self.sources.get(source, {}).get("let"))

    def rows(self, rule, base):
        source = rule["from"]
        table = self.sources.get(source, {}).get("table", source)
        data = [self.P] if table == "_patient" else (self.P.get(table) or [])
        for i, row in enumerate(data):
            c = self.row_ctx(source, row, i, base)
            if rule.get("let"): c = c.child(lazy=rule["let"])
            yield from self.expand({k: rule[k] for k in ("where", "each") if k in rule}, c)

    def build_indexes(self, base):
        self.indexes = {}
        for name, parts in self.index_spec.items():
            idx = collections.defaultdict(list)
            for part in parts:
                for c in self.rows(part, base):
                    k = self.eval(part["key"], c)
                    if k is not None: idx[k].append(self.eval(part.get("value", "$row"), c))
            self.indexes[name] = idx

    # -------------------------------------------------------------- one patient into one log
    def run_patient(self, L, log, P, rep, memo):
        self.L, self.log, self.P, self.rep, self.memo = L, log, P, rep, memo
        self.pid = P[self.pid_key]
        self.reported, collect = set(), collections.defaultdict(list)
        self.base = Ctx(self, vals={"patient": self.pid, "P": P, "param": self.params, "vocab": self.vocab})
        self.build_indexes(self.base)
        for rule in L.get("objects", []):
            for c in self.rows(rule, self.base):
                if "ref" in rule: self.eval(rule["ref"], c); continue
                oid = self.eval(rule["id"], c)
                if not oid: continue
                new = oid not in log.objects
                log.obj(oid, self.eval(rule["type"], c), self.pid, c.get("row"))
                if new:
                    log.set_attrs(oid, self.attr_values(rule.get("attrs") or {}, c))
                    self.apply_o2o(oid, rule.get("o2o") or [], c)
        for rule in L.get("o2o", []):
            for c in self.rows(rule, self.base):
                a, b = self.eval(rule["from_object"], c), self.eval(rule["to"], c)
                if a and b: log.o2o(a, b, self.eval(rule["qualifier"], c))
        for rule in L.get("attributes", []):
            for c in self.rows(rule, self.base):
                oid = self.eval(rule["object"], c)
                if not oid or oid not in log.objects: continue
                vals = self.attr_values(rule["attrs"], c)
                for k in rule.get("collect", []):
                    if vals.get(k) is not None: collect[(oid, k)].append(vals.pop(k))
                log.set_attrs(oid, vals, self.eval(rule.get("time"), c))
        for (oid, k), vs in collect.items(): log.set_attrs(oid, {k: sorted(vs)})
        for rule in L.get("events", []):
            for c in self.rows(rule, self.base):
                t = self.eval(rule["time"], c)
                if t is None: continue
                if not _DATE.match(str(t)): raise MappingError(f"event time {t!r} is not a date (rule {rule.get('activity')})")
                if "subject" in rule: c = c.child({"subject": self.eval(rule["subject"], c)})
                log.event(self.eval(rule["id"], c), self.eval(rule["activity"], c), t, self.e2o(rule.get("e2o", []), c),
                          self.attr_values(rule.get("attrs") or {}, c), self.pid)
        for rule in L.get("reports", []):
            for c in self.rows(rule, self.base):
                kind, label = rule.get("kind", "unmapped"), self.eval(rule["label"], c)
                if kind == "note": rep["notes"].append(label)
                else: rep[kind][label] += 1

    def finalise(self, L, log):
        """Object states derived from the events of the whole log (e.g. a treatment course: active, stopped)."""
        for st in L.get("states_from_events", []):
            at = collections.defaultdict(dict)
            for ev in sorted(log.events, key=lambda x: x["time"]):
                if ev["type"] not in st["events"]: continue
                for r in ev["relationships"]:
                    if r["qualifier"] in st["qualifiers"] and log.objects[r["objectId"]]["type"] == st["object_type"]:
                        if at[r["objectId"]].get(ev["time"]) != st.get("same_time_wins"):
                            at[r["objectId"]][ev["time"]] = st["events"][ev["type"]]
            for oid, seq in at.items():
                last = None
                for t in sorted(seq):
                    if seq[t] != last: log.set_attrs(oid, {st["attribute"]: seq[t]}, time=t); last = seq[t]

    def run(self, patients):
        """Returns {log name: (OCEL JSON, owner map)} and the per-patient reports."""
        reps = {P[self.pid_key]: {"unmapped": collections.Counter(), "mapped_specially": collections.Counter(), "notes": []}
                for P in patients}
        out, base = {}, Ctx(self, vals={"param": self.params, "vocab": self.vocab})
        required = self.spec.get("input", {}).get("require", [])
        mapped = [P for P in patients if all(P.get(table) for table in required)]
        for P in patients:
            for table in required:
                if not P.get(table): reps[P[self.pid_key]]["unmapped"][f"patient left out: no {table}"] += 1
        for lname, L in self.spec["logs"].items():
            if not self.test({"v": L.get("enabled", True)}, base): continue
            log, shared_memo = Log(), {}
            for P in mapped:
                memo = {k: v for k, v in shared_memo.items()}               # shared constructors persist across patients
                self.run_patient(L, log, P, reps[P[self.pid_key]], memo)
                shared_memo = {k: v for k, v in memo.items() if k[2] is None}
            self.finalise(L, log)
            out[lname] = (log.serialise(self.eval(L.get("initial_time", "first_event"), base)), log.owner)
        main_log, owner = next(iter(out.values())) if out else ({"events": [], "objects": []}, {})
        ev_ids, ob_ids = {e["id"] for e in main_log["events"]}, {o["id"] for o in main_log["objects"]}
        for code, r in reps.items():
            r["unmapped"], r["mapped_specially"] = dict(r["unmapped"]), dict(r["mapped_specially"])
            r["size"] = {"events": sum(1 for i in ev_ids if owner.get(i) == code),
                         "objects (own)": sum(1 for i in ob_ids if owner.get(i) == code)}
        return out, reps


# ------------------------------------------------------------------ quality checks (OCELot-style)
def quality(log):
    q, ev_t = {}, collections.defaultdict(list)
    for e in log["events"]:
        for r in e["relationships"]: ev_t[r["objectId"]].append(e["time"])
    out_of_span = before = epoch = 0
    for o in log["objects"]:
        ts = ev_t.get(o["id"])
        for a in o["attributes"]:
            if a["time"].startswith("1970"): epoch += 1
            if ts and not a["time"].startswith("1970") and not (min(ts) <= a["time"] <= max(ts)): out_of_span += 1
        if ts and o["attributes"]:
            first_attr = min(a["time"] for a in o["attributes"])
            before += sum(1 for t in ts if t < first_attr)
    q["attribute changes outside the object's event span"] = out_of_span
    q["event references before the object's first attribute"] = before
    q["epoch timestamps (initial values)"] = epoch
    types = collections.Counter(o["type"] for o in log["objects"])
    noev = collections.Counter(o["type"] for o in log["objects"] if o["id"] not in ev_t)
    q["objects never referenced by an event"] = sum(noev.values())
    q["objects never referenced by an event, by type"] = dict(noev.most_common())
    per = collections.defaultdict(list)
    for o in log["objects"]: per[o["type"]].append(len(ev_t.get(o["id"], [])))
    q["object types with exactly one event per object"] = sorted(t for t, v in per.items() if v and all(n == 1 for n in v))
    q["singleton object types"] = sorted(t for t, n in types.items() if n == 1)
    safe = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    names = {o["type"] for o in log["objects"]} | {e["type"] for e in log["events"]}
    q["type names unsafe as identifiers"] = sorted(n for n in names if not safe.match(n))
    sent, variants = collections.Counter(), collections.defaultdict(set)
    for o in log["objects"]:
        for a in o["attributes"]:
            v = a["value"]
            if isinstance(v, str):
                if v.strip().lower() in SENTINELS: sent[f"{o['type']}.{a['name']}"] += 1
                variants[(o["type"], a["name"], v.strip().lower())].add(v)
    q["placeholder values"] = dict(sent)
    q["attributes with case/whitespace variants"] = sorted({f"{t}.{n}" for (t, n, _), vs in variants.items() if len(vs) > 1})
    ties = collections.Counter(e["time"] for e in log["events"])
    q["timestamps shared by 6+ events"] = {t[:10]: n for t, n in ties.items() if n >= 6}
    changes = collections.Counter()
    for o in log["objects"]:
        c = collections.Counter(a["name"] for a in o["attributes"])
        changes[o["type"]] += sum(1 for n in c.values() if n > 1)
    q["attributes with more than one value"] = {t: n for t, n in changes.items() if n}
    q["size"] = {"objects": len(log["objects"]), "object types": len(types), "events": len(log["events"]),
                 "event types": len({e["type"] for e in log["events"]}),
                 "E2O": sum(len(e["relationships"]) for e in log["events"]), "O2O": sum(len(o["relationships"]) for o in log["objects"])}
    return q


def patients_of(log, owner):
    """Object id -> patients it is connected to (its owner, or through E2O and O2O for shared objects)."""
    pats = collections.defaultdict(set)
    for o in log["objects"]:
        p = owner.get(o["id"])
        if p: pats[o["id"]].add(p)
        for r in o["relationships"]:
            if p: pats[r["objectId"]].add(p)
    for e in log["events"]:
        for r in e["relationships"]: pats[r["objectId"]].add(owner.get(e["id"]))
    return pats


def cohort(log, owner, journey_type="PatientCondition", patient_type="Patient"):
    """Checks that only mean something across patients."""
    q, pats = {}, patients_of(log, owner)
    T = {o["id"]: o["type"] for o in log["objects"]}
    q["patients"] = sum(1 for t in T.values() if t == patient_type)
    q["events per patient"] = dict(collections.Counter(owner.get(e["id"]) for e in log["events"]).most_common())
    shared = collections.defaultdict(lambda: [0, 0])
    for o in log["objects"]:
        if owner.get(o["id"]) is None:
            shared[o["type"]][0] += 1; shared[o["type"]][1] += len(pats[o["id"]]) > 1
    q["shared objects used by 2+ patients"] = {t: f"{k} of {n}" for t, (n, k) in shared.items()}
    n_ev, touch = len(log["events"]), collections.Counter(r["objectId"] for e in log["events"] for r in e["relationships"])
    q["objects referenced by more than half of all events"] = sorted({T[i] for i, n in touch.items() if n > n_ev / 2})
    pc = collections.defaultdict(set)
    for o in log["objects"]:
        for r in o["relationships"]:
            if T.get(r["objectId"]) == journey_type and o["type"] != journey_type: pc[o["id"]].add(r["objectId"])
    for e in log["events"]:
        js = {r["objectId"] for r in e["relationships"] if T.get(r["objectId"]) == journey_type}
        if len(js) > 1: pc[e["id"]] |= js
    q["facts or events in 2+ journeys"] = sum(1 for v in pc.values() if len(v) > 1)
    par = {}
    def f(x):
        while par.setdefault(x, x) != x: par[x] = par[par[x]]; x = par[x]
        return x
    for e in log["events"]:
        for r in e["relationships"]:
            if T[r["objectId"]] != patient_type: par[f(e["id"])] = f(r["objectId"])
    q[f"connected pieces without {patient_type} objects"] = len({f(e["id"]) for e in log["events"]})
    return q


def lifecycle(log, owner, params):
    ev = collections.Counter(r["objectId"] for e in log["events"] for r in e["relationships"])
    pats = patients_of(log, owner)
    per, reach = collections.defaultdict(list), collections.defaultdict(set)
    for o in log["objects"]:
        per[o["type"]].append(ev.get(o["id"], 0)); reach[o["type"]] |= pats[o["id"]]
    rows = {}
    for t, v in per.items():
        v = sorted(v); med = v[len(v) // 2]
        rows[t] = {"objects": len(v), "patients": len(reach[t]), "median_events": med,
                   "perspective": t in params.get("perspectives", []), "meets_min_lifecycle": med >= params.get("min_lifecycle", 1)}
    return rows


def fmt(v):
    if isinstance(v, dict): return ", ".join(f"{k}: {n}" for k, n in v.items()) or "none"
    if isinstance(v, list): return ", ".join(map(str, v)) or "none"
    return str(v)


def md(q_new, q_old, coh, life, reps, params, name, mapping_name):
    codes = list(reps)
    L = [f"# OCEL build quality: {name}", "", f"Patients: {', '.join(codes)}", "", f"Mapping: `{mapping_name}`", "",
         "Parameters: `" + json.dumps(params) + "`", "",
         "## Checks", "", "| Check | New log" + (" | Previous log |" if q_old else " |"), "|---|---" + ("|---|" if q_old else "|")]
    for k in q_new:
        L.append(f"| {k} | {fmt(q_new[k])}" + (f" | {fmt(q_old.get(k, ''))} |" if q_old else " |"))
    L += ["", "## Cohort checks", "", "| Check | Value |", "|---|---|"] + [f"| {k} | {fmt(v)} |" for k, v in coh.items()]
    L += ["", "## Lifecycle per object type", "",
          "| Object type | Objects | Patients | Median events | Perspective | Meets min lifecycle |", "|---|---|---|---|---|---|"]
    for t, r in sorted(life.items(), key=lambda kv: -kv[1]["objects"]):
        L.append(f"| {t} | {r['objects']} | {r['patients']} | {r['median_events']} | {'yes' if r['perspective'] else 'carrier'} | {'yes' if r['meets_min_lifecycle'] else 'no'} |")
    if len(codes) < 5: L += ["", f"Lifecycle is measured on {len(codes)} patient(s); judge perspectives on a larger cohort."]
    for title, key in (("Not mapped", "unmapped"), ("Mapped by a special rule", "mapped_specially")):
        items = sorted({k for r in reps.values() for k in r[key]})
        if not items: continue
        L += ["", f"## {title}", "", "| Item | Total | " + " | ".join(codes) + " |", "|---|---|" + "---|" * len(codes)]
        for k in items:
            vals = [reps[c][key].get(k, 0) for c in codes]
            L.append(f"| {k} | {sum(vals)} | " + " | ".join(map(str, vals)) + " |")
    notes = [n for r in reps.values() for n in r["notes"]]
    if notes: L += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------- CLI
def load_mapping(path):
    if path: p = Path(path)
    else:
        here = Path(__file__).parent
        try:
            import yaml  # noqa: F401
            p = here / "mapping.yaml"
        except ImportError:
            p = here / "mapping.json"
    if p.suffix in (".yaml", ".yml"):
        try: import yaml
        except ImportError: sys.exit(f"{p}: reading YAML needs PyYAML (pip install pyyaml); or pass the .json mapping")
        return yaml.safe_load(p.read_text(encoding="utf-8")), p.name
    return json.loads(p.read_text(encoding="utf-8")), p.name


def load_patients(paths, spec):
    key, pid = spec.get("input", {}).get("patients", "patients"), spec.get("input", {}).get("patient_id", "code")
    out, seen = [], set()
    for p in paths:
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        for P in (data if isinstance(data, list) else data[key]):
            if P[pid] in seen: sys.exit(f"{p}: patient {P[pid]} appears in more than one input")
            seen.add(P[pid]); out.append(P)
    return out


def write(out, name, patients, spec, params, q_old, mapping_name):
    logs, reps = Engine(spec, params).run(patients)
    for lname, (lg, _) in logs.items():
        (out / f"{name}-{lname}.ocel.json").write_text(json.dumps(lg, indent=1, ensure_ascii=False))
    main_log, owner = next(iter(logs.values()))
    qn, coh, life = quality(main_log), cohort(main_log, owner), lifecycle(main_log, owner, params)
    (out / f"{name}-quality.md").write_text(md(qn, q_old, coh, life, reps, params, name, mapping_name))
    (out / f"{name}-build-report.json").write_text(json.dumps({"mapping": mapping_name, "params": params, "quality": qn,
        "quality_previous": q_old, "cohort": coh, "lifecycle": life, "patients": reps}, indent=1))
    print(name, json.dumps(qn["size"]), "| events per log:", json.dumps({k: len(v[0]["events"]) for k, v in logs.items()}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("traces", nargs="+", help="patient-journey exports (JSON)")
    ap.add_argument("--mapping", help="mapping file (.yaml or .json); default: mapping.yaml/.json beside this script")
    ap.add_argument("--params", help="JSON file overriding the mapping's params")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override one param (VALUE as JSON, else a string)")
    ap.add_argument("--out", default=".")
    ap.add_argument("--name", help="output name (default: the patient code for one patient, else 'cohort')")
    ap.add_argument("--per-patient", action="store_true", help="also write one set of files per patient")
    ap.add_argument("--compare", help="a previous OCEL 2.0 JSON log to compare against")
    args = ap.parse_args()
    spec, mapping_name = load_mapping(args.mapping)
    params = copy.deepcopy(spec.get("params", {}))
    if args.params: params.update(json.loads(Path(args.params).read_text()))
    for kv in args.set:
        k, _, v = kv.partition("=")
        try: params[k] = json.loads(v)
        except json.JSONDecodeError: params[k] = v
    patients = load_patients(args.traces, spec)
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    q_old = quality(json.loads(Path(args.compare).read_text())) if args.compare else None
    pid = spec.get("input", {}).get("patient_id", "code")
    name = args.name or (patients[0][pid].lower() if len(patients) == 1 else "cohort")
    try:
        write(out, name, patients, spec, params, q_old, mapping_name)
        if args.per_patient and len(patients) > 1:
            for P in patients: write(out, P[pid].lower(), [P], spec, params, None, mapping_name)
    except MappingError as e:
        sys.exit(f"mapping error: {e}")


if __name__ == "__main__":
    main()
