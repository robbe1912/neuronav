"""archrules.py — architecture-violation rules over crosstalk (issue #70).

Turns the descriptive crosstalk report into a checkable contract: a
project-local rule list (``<state_dir>/arch-rules.json`` — read at call
time so a routed call serves that project's rules, the memories.py law)
is evaluated against the SAME cross-cluster wire tally crosstalk()
reads (``clusters.cross_tallies`` — issue #114 parity by construction:
rules see exactly the wiring the clusterer's structural graph sees).

Rule file shape::

    {"rules": [
        {"id": "ui-no-net", "kind": "forbid", "from": "UI", "to": "Networking"},
        {"id": "core-budget", "kind": "budget", "from": "Core", "to": "Net", "max": 5},
        {"id": "no-var-into-io", "kind": "forbid", "from": "UI", "to": "IO",
         "types": ["var"], "severity": "warn"}
    ]}

- clusters are referenced by label (exact, else unique case-insensitive)
  or by the ``cN`` id form the clusters/crosstalk tools print
- ``forbid`` allows zero wires from -> to; ``budget`` allows at most
  ``max``; ``types`` (optional) narrows to those structural edge kinds
- ``severity`` defaults to "error" and rides through to the violation

Nothing is ever silently skipped (the typo guard): an unknown rule kind,
an unknown cluster/type name, a malformed file or an unknown key lands
in the report's config errors, named and counted. An ABSENT file is not
an error — the answer says no rules are configured and how to write one.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import clusters
import nav

RULES_NAME = "arch-rules.json"
KINDS = ("forbid", "budget")
SEVERITIES = ("error", "warn")
EDGE_TYPES = ("call", "var", "signal", "inst", "attach", "alias")
TOP_FILES = 5  # offending file pairs shown per violation

_RULE_KEYS = ("id", "kind", "from", "to", "max", "types", "severity")
_ID_RE = re.compile(r"^c(\d+)$")


def rules_path() -> Path:
    """The active config's rules file — read at call time so a routed
    call (nav.config_scope) serves that project's rules."""
    return nav.STATE_DIR / RULES_NAME


def load_rules() -> dict:
    """Read + validate the rule file. Returns {"path", "exists",
    "rules", "errors"}: every rule is either a validated dict in
    ``rules`` (file order) or named in ``errors`` — never dropped."""
    path = rules_path()
    out: dict = {"path": path, "exists": path.is_file(), "rules": [], "errors": []}
    if not out["exists"]:
        return out
    err = out["errors"].append
    try:
        with open(path, encoding="utf-8-sig") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        err(f"{RULES_NAME} unreadable: {exc}")
        return out
    if not isinstance(doc, dict):
        err(f"{RULES_NAME} must be an object with a 'rules' list, got {type(doc).__name__}")
        return out
    for key in doc:
        if key != "rules":
            err(f"unknown key '{key}' (only 'rules' is read)")
    raw = doc.get("rules")
    if not isinstance(raw, list):
        err("'rules' must be a list")
        return out
    ids: Counter = Counter()
    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            err(f"rule #{i + 1}: must be an object, got {type(r).__name__}")
            continue
        where = f"rule #{i + 1}"
        rid = r.get("id")
        if rid is not None:
            if not isinstance(rid, str) or not rid.strip():
                err(f"{where}: 'id' must be a non-empty string")
                rid = None
            else:
                where = f"{where} '{rid}'"
                ids[rid] += 1
        bad = False
        for key in r:
            if key not in _RULE_KEYS:
                err(f"{where}: unknown key '{key}' (known: {', '.join(_RULE_KEYS)})")
                bad = True
        kind = r.get("kind")
        if kind not in KINDS:
            err(f"{where}: unknown kind {kind!r} (known kinds: {', '.join(KINDS)})")
            bad = True
        for side in ("from", "to"):
            ref = r.get(side)
            if not isinstance(ref, str) or not ref.strip():
                err(f"{where}: '{side}' must be a cluster label or cN id")
                bad = True
        if "max" in r:
            mx = r["max"]
            if kind == "forbid":
                err(f"{where}: kind 'forbid' takes no 'max' (use kind 'budget')")
                bad = True
            elif not isinstance(mx, int) or isinstance(mx, bool) or mx < 0:
                err(f"{where}: 'max' must be an integer >= 0")
                bad = True
        elif kind == "budget":
            err(f"{where}: kind 'budget' requires 'max'")
            bad = True
        types = r.get("types")
        if types is not None:
            if (
                not isinstance(types, list)
                or not types
                or any(t not in EDGE_TYPES for t in types)
            ):
                known = ", ".join(EDGE_TYPES)
                err(f"{where}: 'types' must be a non-empty list from [{known}], got {types!r}")
                bad = True
            else:
                types = sorted(set(types))
        sev = r.get("severity", "error")
        if sev not in SEVERITIES:
            err(f"{where}: unknown severity {sev!r} (known: {', '.join(SEVERITIES)})")
            bad = True
        if bad:
            continue
        rule = {"id": rid or f"{kind} {r['from']}->{r['to']}", "kind": kind,
                "from": r["from"], "to": r["to"]}
        if "max" in r:
            rule["max"] = r["max"]
        if types is not None:
            rule["types"] = types
        if "severity" in r:
            rule["severity"] = sev
        out["rules"].append(rule)
    for rid, n in sorted(ids.items()):
        if n > 1:
            err(f"duplicate rule id '{rid}' x{n} — ids must be unique")
    return out


def _match_cluster(ref: str, cs: list[dict]) -> tuple[dict | None, str]:
    """Resolve one rule-side cluster reference against the partition:
    exact label, else unique case-insensitive label, else the cN id
    form. Misses and ambiguity are errors (the typo guard)."""
    m = _ID_RE.match(ref)
    if m:
        cid = int(m.group(1))
        for c in cs:
            if c["id"] == cid:
                return c, ""
        return None, f"no cluster with id c{cid} (partition has {len(cs)} clusters)"
    hits = [c for c in cs if str(c.get("label", "")) == ref]
    if not hits:
        fold = ref.casefold()
        hits = [c for c in cs if str(c.get("label", "")).casefold() == fold]
    if len(hits) == 1:
        return hits[0], ""
    if len(hits) > 1:
        return None, f"'{ref}' is ambiguous ({len(hits)} case-insensitive label matches)"
    return None, f"no cluster labelled '{ref}'"


def check(cs: list[dict], g=None) -> dict:
    """Evaluate the project's rules against the partition + wiring.
    Returns the machine-consumable report (fmt renders it): absent file
    -> {"configured": False}; broken rules land in config_errors and
    never mask the violations the other rules produced."""
    load = load_rules()
    report: dict = {
        "path": load["path"],
        "configured": load["exists"],
        "rules": len(load["rules"]),
        "violations": [],
        "config_errors": list(load["errors"]),
    }
    if not load["exists"]:
        return report
    tallies = clusters.cross_tallies(cs, g)
    known = ", ".join(f"c{c['id']} {c.get('label', '')}".strip() for c in cs)
    for r in load["rules"]:
        src, serr = _match_cluster(r["from"], cs)
        dst, derr = _match_cluster(r["to"], cs)
        for side, e in (("from", serr), ("to", derr)):
            if e:
                report["config_errors"].append(
                    f"rule '{r['id']}' {side}: {e} — known clusters: {known}")
        if src is None or dst is None:
            continue
        wires = tallies["pair_wires"].get((src["id"], dst["id"]), [])
        types = r.get("types")
        if types:
            tset = set(types)
            wires = [w for w in wires if tset & set(w[2])]
        limit = 0 if r["kind"] == "forbid" else r["max"]
        if len(wires) > limit:
            files: Counter = Counter(f"{sf} -> {df}" for sf, df, _t in wires)
            report["violations"].append(
                {
                    "rule": r["id"],
                    "kind": r["kind"],
                    "severity": r.get("severity", "error"),
                    "from": {"id": src["id"], "label": str(src.get("label", ""))},
                    "to": {"id": dst["id"], "label": str(dst.get("label", ""))},
                    "wires": len(wires),
                    "limit": limit,
                    "types": list(types) if types else None,
                    "files": [
                        {"pair": p, "w": w}
                        for p, w in sorted(files.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_FILES]
                    ],
                    "more_files": max(len(files) - TOP_FILES, 0),
                }
            )
    report["violations"].sort(
        key=lambda v: (v["severity"] != "error", v["rule"], v["from"]["id"], v["to"]["id"])
    )
    return report


def fmt(report: dict) -> str:
    """Render a check() report for humans — the arch_check tool's
    answer. Deterministic: violations by severity then rule id, file
    pairs by weight then path, config errors in file order."""
    if not report["configured"]:
        return (
            f"no arch rules configured — write {report['path']} with "
            '{"rules": [...]} to turn crosstalk into a checked contract '
            '(kinds: forbid (zero wires), budget (at most max); clusters '
            "by label or cN id; types call/var/signal/inst/attach/alias "
            'narrow a rule to those wires — e.g. {"id": "ui-no-net", '
            '"kind": "forbid", "from": "UI", "to": "Networking"})'
        )
    n_err = len(report["config_errors"])
    n_v = len(report["violations"])
    head = (
        f"arch check: {report['rules']} rule(s), "
        + (f"{n_v} violation(s)" if n_v else "clean")
        + (f", {n_err} rule config error(s)" if n_err else "")
        + f" ({report['path']})"
    )
    lines = [head]
    for v in report["violations"]:
        kinds = f", types {'/'.join(v['types'])}" if v["types"] else ""
        allow = "forbid: 0 allowed" if v["kind"] == "forbid" else f"budget: max {v['limit']}"
        lines.append(
            f"  [{v['severity']}] {v['rule']}: "
            f"{v['from']['label']} (c{v['from']['id']}) -> "
            f"{v['to']['label']} (c{v['to']['id']}) — {v['wires']} wires ({allow}{kinds})"
        )
        for f in v["files"]:
            lines.append(f"    {f['pair']} x{f['w']}")
        if v["more_files"]:
            lines.append(f"    … +{v['more_files']} more file pair(s)")
    if n_err:
        lines.append("rule config errors (fix the file — these rules were NOT evaluated):")
        for e in report["config_errors"]:
            lines.append(f"  - {e}")
    return "\n".join(lines)


def run(cs: list[dict], g=None) -> str:
    """The arch_check tool body (server.py wraps this in _route)."""
    if not cs:
        return "index empty — call rescan first"
    return fmt(check(cs, g))
