#!/usr/bin/env python3
"""Track where GitHub's published REST description and GitHub's real behaviour disagree.

gh-safe-repo depends on a few dozen REST endpoints, and several of them do
not behave the way the OpenAPI description (github/rest-api-description)
says they do. Those gaps were found one at a time, live, and recorded in
docs/. This script keeps them in one machine-readable catalog
(``catalog.json`` next to this file) and checks the catalog against a copy
of the description, so we notice when either side moves.

Subcommands (stdlib only; run with ``uv run tools/api_drift/api_drift.py``):

  check     Compare the catalog with a REST description file. Reports
            spec drift since the catalog snapshot (endpoint gone or
            deprecated, status codes or fields added/removed), quirks whose
            spec condition no longer holds (GitHub may have fixed or
            documented them, so the workaround may be removable), observed
            behaviour the spec does not document, and endpoints the
            gh_safe_repo source calls that the catalog does not track.
  snapshot  Rewrite each endpoint's ``spec`` block from a description
            file. Run it after reviewing a ``check`` report to accept the
            spec's current state as the new baseline.
  probe     Live, read-only: GET each tracked GET endpoint on a repo you
            admin via ``gh api`` and compare status and top-level fields
            with the spec. Never sends anything but GET.

Exit codes: 0 nothing new, 1 drift or an untracked/uncataloged item,
2 usage or input error.
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_CATALOG = os.path.join(HERE, "catalog.json")
DEFAULT_SOURCE = os.path.join(REPO_ROOT, "gh_safe_repo")

METHODS = ("get", "put", "post", "patch", "delete")
_PARAM = re.compile(r"\{[^}]*\}")


class DriftError(Exception):
    """Bad input: unreadable catalog/spec, malformed entry."""


def norm(path):
    """Normalize a path template so `{owner}` and `{}` compare equal."""
    p = _PARAM.sub("{}", path.split("?", 1)[0]).rstrip("/")
    return p or "/"


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise DriftError(f"cannot read {path}: {e}")


# --------------------------------------------------------------------------
# OpenAPI access
# --------------------------------------------------------------------------

class Spec:
    """Just enough OpenAPI 3 reading for the checks below."""

    def __init__(self, doc):
        if not isinstance(doc, dict) or not isinstance(doc.get("paths"), dict):
            raise DriftError("not an OpenAPI description (no `paths` object)")
        self.doc = doc
        self.paths = doc["paths"]
        self._by_norm = {norm(p): p for p in self.paths}
        self._patterns = None

    @property
    def version(self):
        return (self.doc.get("info") or {}).get("version")

    def template(self, path):
        """The spec's own template for `path`, or None.

        Parameter names do not matter (`{owner}` == `{}`), and a concrete
        segment matches a parameter (`contents/README.md` is
        `contents/{path}`). When several templates match, the one with the
        most literal segments wins.
        """
        if path in self.paths:
            return path
        key = norm(path)
        if key in self._by_norm:
            return self._by_norm[key]
        if self._patterns is None:
            self._patterns = []
            for tmpl in self.paths:
                segs = norm(tmpl).split("/")
                rx = "/".join("[^/]+" if s == "{}" else re.escape(s) for s in segs)
                literal = sum(1 for s in segs if s != "{}")
                self._patterns.append((re.compile(rx + r"\Z"), literal, tmpl))
        hits = [(lit, t) for rx, lit, t in self._patterns if rx.match(key)]
        return max(hits)[1] if hits else None

    def operation(self, method, path):
        tmpl = self.template(path)
        if tmpl is None:
            return None
        op = self.paths[tmpl].get(method.lower())
        return op if isinstance(op, dict) else None

    def resolve(self, obj, depth=0):
        """Follow local `$ref`s."""
        while isinstance(obj, dict) and "$ref" in obj and depth < 50:
            ref = obj["$ref"]
            if not ref.startswith("#/"):
                return {}
            node = self.doc
            for part in ref[2:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                node = node.get(part, {}) if isinstance(node, dict) else {}
            obj = node
            depth += 1
        return obj if isinstance(obj, dict) else {}

    def status_codes(self, op):
        return sorted(str(k) for k in (op.get("responses") or {}))

    def _json_schema(self, holder):
        holder = self.resolve(holder)
        content = holder.get("content") or {}
        for ctype, media in content.items():
            if "json" in ctype:
                return self.resolve((media or {}).get("schema") or {})
        return None

    def response_schema(self, op):
        """Schema of the first documented 2xx JSON response, or None."""
        for code in self.status_codes(op):
            if code.startswith("2"):
                schema = self._json_schema((op.get("responses") or {})[code])
                if schema is not None:
                    return schema
        return None

    def request_schema(self, op):
        body = op.get("requestBody")
        return self._json_schema(body) if body else None

    def fields(self, schema, descend=()):
        """(properties, required) of an object schema; arrays use their items.

        allOf merges; oneOf/anyOf union the properties and keep only the
        fields every branch requires. `descend` walks into nested properties.
        """
        schema = self.resolve(schema or {})
        for name in descend:
            props, _ = self._fields(schema)
            schema = self.resolve(props.get(name) or {})
        props, required = self._fields(schema)
        return sorted(props), sorted(required)

    def _fields(self, schema, depth=0):
        schema = self.resolve(schema)
        if depth > 10:
            return {}, set()
        if schema.get("type") == "array" and "items" in schema:
            return self._fields(schema["items"], depth + 1)
        props = dict(schema.get("properties") or {})
        required = set(schema.get("required") or [])
        for sub in schema.get("allOf") or []:
            p, r = self._fields(sub, depth + 1)
            props.update(p)
            required |= r
        branches = (schema.get("oneOf") or []) + (schema.get("anyOf") or [])
        if branches:
            common = None
            for sub in branches:
                p, r = self._fields(sub, depth + 1)
                props.update(p)
                common = r if common is None else common & r
            required |= common or set()
        return props, required

    def description(self, op, field=None, where="response", descend=()):
        """Description text of an operation, or of one of its fields."""
        if field is None:
            return " ".join(filter(None, [op.get("summary"), op.get("description")]))
        schema = self.response_schema(op) if where == "response" else self.request_schema(op)
        schema = self.resolve(schema or {})
        for name in descend:
            props, _ = self._fields(schema)
            schema = self.resolve(props.get(name) or {})
        props, _ = self._fields(schema)
        return self.resolve(props.get(field) or {}).get("description") or ""


def spec_block(spec, method, path):
    """What the spec says about one operation, in catalog form."""
    op = spec.operation(method, path)
    if op is None:
        return None
    resp_fields, resp_req = spec.fields(spec.response_schema(op))
    req_fields, req_req = spec.fields(spec.request_schema(op))
    return {
        "status_codes": spec.status_codes(op),
        "response_fields": resp_fields,
        "response_required": resp_req,
        "request_fields": req_fields,
        "request_required": req_req,
        "deprecated": bool(op.get("deprecated")),
    }


# --------------------------------------------------------------------------
# Quirk conditions
# --------------------------------------------------------------------------

def _field_state(spec, op, check):
    where = check.get("where", "response")
    schema = spec.response_schema(op) if where == "response" else spec.request_schema(op)
    return spec.fields(schema, tuple(check.get("descend") or ()))


def condition_holds(spec, quirk):
    """True while the spec still says what it said when the quirk was cataloged.

    Returns (holds, detail). `holds` is None when the condition cannot be
    evaluated because the endpoint itself is gone (reported separately).
    """
    check = quirk.get("check") or {}
    kind = check.get("type")
    method, path = quirk["method"], quirk["path"]
    op = spec.operation(method, path)

    if kind == "method_absent":
        if op is None:
            return True, f"{method} not documented"
        return False, f"{method} {path} is now documented"
    if kind == "none":
        return True, "behavioural; no spec condition to test"
    if op is None:
        return None, f"{method} {path} not in spec"

    if kind == "status_undocumented":
        codes = spec.status_codes(op)
        s = str(check["status"])
        return (s not in codes), f"documented: {', '.join(codes)}"
    if kind == "status_documented":
        codes = spec.status_codes(op)
        s = str(check["status"])
        return (s in codes), f"documented: {', '.join(codes)}"
    if kind in ("field_optional", "field_required", "field_absent"):
        props, required = _field_state(spec, op, check)
        f = check["field"]
        if kind == "field_absent":
            return (f not in props), f"{f} {'in' if f in props else 'not in'} schema"
        if f not in props:
            return False, f"{f} no longer in schema"
        state = "required" if f in required else "optional"
        want = "required" if kind == "field_required" else "optional"
        return state == want, f"{f} is {state}"
    if kind == "text_lacks":
        text = spec.description(
            op, check.get("field"), check.get("where", "response"),
            tuple(check.get("descend") or ()),
        )
        hit = re.search(check["pattern"], text, re.IGNORECASE)
        if hit:
            return False, f"description now mentions {hit.group(0)!r}"
        return True, f"description does not match /{check['pattern']}/"
    raise DriftError(f"quirk {quirk.get('id')}: unknown check type {kind!r}")


# --------------------------------------------------------------------------
# Source scan: which endpoints does gh_safe_repo call?
# --------------------------------------------------------------------------

_API_CALLS = {"call_api", "call_json", "get_json", "get_json_pages"}


def _render(node):
    """A str constant or f-string as a template (`{}` for each expression)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            else:
                out.append("{}")
        return "".join(out)
    return None


def _candidates(tree):
    """Yield (template, explicit) for strings that look like API paths.

    explicit=True: an argument to repo_path() or a literal "/repos/..." or
    "/user..." path, which must exist in the spec. explicit=False: any other
    string that might be a repo-path suffix (e.g. a toggle name in a
    lookup table); kept only if the spec has a matching path.
    """
    explicit_nodes = set()
    for node in ast.walk(tree):
        # The literal pieces of an f-string only mean something as a whole.
        if isinstance(node, ast.JoinedStr):
            explicit_nodes.update(id(v) for v in node.values)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name == "repo_path" and len(node.args) >= 3:
            s = _render(node.args[2])
            if s is not None:
                explicit_nodes.add(id(node.args[2]))
                yield "/repos/{}/{}/" + s.lstrip("/"), True
        elif name == "repo_path" and len(node.args) == 2:
            yield "/repos/{}/{}", True
        elif name in _API_CALLS:
            for arg in node.args:
                s = _render(arg)
                if s and (s.startswith("/repos/") or s.startswith("/user")):
                    explicit_nodes.add(id(arg))
                    yield s, True
                elif s and s.startswith("{}/"):
                    # f"{path}/topics", path being a repo_path() result
                    explicit_nodes.add(id(arg))
                    yield "/repos/{}/{}/" + s[3:], False
    for node in ast.walk(tree):
        if id(node) in explicit_nodes:
            continue
        s = _render(node) if isinstance(node, (ast.Constant, ast.JoinedStr)) else None
        if not s or " " in s or len(s) > 100:
            continue
        if s.startswith("/repos/") or s.startswith("/user"):
            yield s, True
        elif ("-" in s or "/" in s) and re.fullmatch(r"[a-z][a-z0-9_{}/-]*", s):
            yield "/repos/{}/{}/" + s, False


def scan_source(source_dir, spec):
    """{normalized path: {"files": [...], "in_spec": bool}} for the package."""
    found = {}
    for root, _dirs, files in os.walk(source_dir):
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, REPO_ROOT)
            try:
                with open(full, encoding="utf-8") as f:
                    tree = ast.parse(f.read(), filename=full)
            except (OSError, SyntaxError) as e:
                raise DriftError(f"cannot parse {full}: {e}")
            for tmpl, explicit in _candidates(tree):
                spec_tmpl = spec.template(tmpl)
                in_spec = spec_tmpl is not None
                if not explicit and not in_spec:
                    continue
                key = norm(spec_tmpl or tmpl)
                entry = found.setdefault(key, {"files": [], "in_spec": in_spec})
                if rel not in entry["files"]:
                    entry["files"].append(rel)
    return found


# --------------------------------------------------------------------------
# check
# --------------------------------------------------------------------------

def _validate_catalog(catalog):
    if not isinstance(catalog, dict):
        raise DriftError("catalog must be a JSON object")
    for key in ("endpoints", "quirks"):
        if not isinstance(catalog.get(key), list):
            raise DriftError(f"catalog has no `{key}` list")
    seen = set()
    for ep in catalog["endpoints"]:
        if not ep.get("method") or not ep.get("path"):
            raise DriftError(f"endpoint entry without method/path: {ep}")
        k = (ep["method"].upper(), norm(ep["path"]))
        if k in seen:
            raise DriftError(f"duplicate endpoint {ep['method']} {ep['path']}")
        seen.add(k)
    ids = set()
    for q in catalog["quirks"]:
        for f in ("id", "method", "path", "check"):
            if f not in q:
                raise DriftError(f"quirk without `{f}`: {q.get('id', q)}")
        if q["id"] in ids:
            raise DriftError(f"duplicate quirk id {q['id']}")
        ids.add(q["id"])


def _diff_list(old, new):
    old, new = set(old or []), set(new or [])
    return sorted(new - old), sorted(old - new)


def check(catalog, spec, source_dir=None):
    """Return the report dict; `drift` True means something needs a human."""
    _validate_catalog(catalog)
    report = {
        "spec_version": spec.version,
        "snapshot_version": (catalog.get("snapshot") or {}).get("info_version"),
        "spec_changes": [],
        "notices": [],
        "quirks": [],
        "undocumented": [],
        "uncataloged_gaps": [],
        "untracked_code_paths": [],
        "code_paths_not_in_spec": [],
    }

    for ep in catalog["endpoints"]:
        method, path = ep["method"].upper(), ep["path"]
        label = f"{method} {path}"
        now = spec_block(spec, method, path)
        if now is None:
            if spec.template(path) is None:
                hint = _similar_paths(spec, path)
                report["spec_changes"].append({
                    "endpoint": label, "change": "path_missing",
                    "detail": "path not in spec" + (f"; similar: {', '.join(hint)}" if hint else ""),
                })
            else:
                report["spec_changes"].append({
                    "endpoint": label, "change": "method_missing",
                    "detail": f"{method} no longer documented on this path",
                })
            continue
        snap = ep.get("spec") or {}
        if now["deprecated"] and not snap.get("deprecated"):
            report["spec_changes"].append({
                "endpoint": label, "change": "deprecated", "detail": "operation marked deprecated",
            })
        for key in ("status_codes", "response_fields", "response_required",
                    "request_fields", "request_required"):
            if key not in snap:
                continue
            added, removed = _diff_list(snap[key], now[key])
            if key == "response_fields" and added and not removed:
                # New response fields break nothing; report, don't fail.
                report["notices"].append({
                    "endpoint": label, "change": key, "added": added, "removed": [],
                })
            elif added or removed:
                report["spec_changes"].append({
                    "endpoint": label, "change": key,
                    "added": added, "removed": removed,
                })

        obs = ep.get("observed") or {}
        undoc_status = sorted(set(map(str, obs.get("status_codes") or [])) - set(now["status_codes"]))
        undoc_fields = sorted(set(obs.get("response_fields") or []) - set(now["response_fields"]))
        if undoc_status or undoc_fields:
            item = {"endpoint": label}
            if undoc_status:
                item["status_codes"] = undoc_status
            if undoc_fields:
                item["response_fields"] = undoc_fields
            item["evidence"] = obs.get("evidence")
            report["undocumented"].append(item)
            for s in undoc_status:
                if not _has_quirk(catalog, method, path, "status_undocumented", status=s):
                    report["uncataloged_gaps"].append({
                        "endpoint": label,
                        "detail": f"observed {s} is not documented and has no quirk entry",
                    })

    for q in catalog["quirks"]:
        holds, detail = condition_holds(spec, q)
        if holds is None:
            state = "endpoint_missing"
        elif holds:
            state = "still_open"
        else:
            state = "spec_changed"
        report["quirks"].append({
            "id": q["id"], "endpoint": f"{q['method'].upper()} {q['path']}",
            "state": state, "detail": detail, "summary": q.get("summary"),
        })

    if source_dir:
        tracked = {norm(spec.template(ep["path"]) or ep["path"]) for ep in catalog["endpoints"]}
        for key, info in sorted(scan_source(source_dir, spec).items()):
            if not info["in_spec"]:
                report["code_paths_not_in_spec"].append({"path": key, "files": info["files"]})
            elif key not in tracked:
                report["untracked_code_paths"].append({
                    "path": spec.template(key), "files": info["files"],
                })

    report["drift"] = bool(
        report["spec_changes"]
        or any(q["state"] != "still_open" for q in report["quirks"])
        or report["uncataloged_gaps"]
        or report["untracked_code_paths"]
        or report["code_paths_not_in_spec"]
    )
    return report


def _has_quirk(catalog, method, path, kind, **match):
    for q in catalog["quirks"]:
        c = q.get("check") or {}
        if (q["method"].upper() == method and norm(q["path"]) == norm(path)
                and c.get("type") == kind
                and all(str(c.get(k)) == str(v) for k, v in match.items())):
            return True
    return False


def _similar_paths(spec, path, limit=3):
    last = norm(path).rsplit("/", 1)[-1]
    if last == "{}":
        return []
    return sorted(p for p in spec.paths if p.rstrip("/").endswith("/" + last))[:limit]


def format_check(report):
    lines = [f"REST description version: {report['spec_version']} "
             f"(catalog snapshot: {report['snapshot_version']})", ""]

    def section(title, rows):
        lines.append(f"{title}: {len(rows) if rows else 'none'}")
        for r in rows:
            lines.append(f"  - {r}")
        lines.append("")

    def change_line(c):
        if "added" not in c:
            return f"{c['endpoint']}: {c['change']} ({c['detail']})"
        parts = []
        if c["added"]:
            parts.append("+" + ", +".join(c["added"]))
        if c["removed"]:
            parts.append("-" + ", -".join(c["removed"]))
        return f"{c['endpoint']}: {c['change']} {' '.join(parts)}"

    section("Spec changes since snapshot", [change_line(c) for c in report["spec_changes"]])
    if report["notices"]:
        section("Notices (new response fields; not drift)",
                [change_line(c) for c in report["notices"]])

    changed = [q for q in report["quirks"] if q["state"] != "still_open"]
    section("Quirks whose spec condition changed (workaround may be removable)",
            [f"[{q['id']}] {q['endpoint']}: {q['state']}: {q['detail']}" for q in changed])
    section("Uncataloged gaps",
            [f"{g['endpoint']}: {g['detail']}" for g in report["uncataloged_gaps"]])
    section("Endpoints called by gh_safe_repo but not in the catalog",
            [f"{u['path']} ({', '.join(u['files'])})" for u in report["untracked_code_paths"]])
    section("Paths called by gh_safe_repo that the spec does not have",
            [f"{u['path']} ({', '.join(u['files'])})" for u in report["code_paths_not_in_spec"]])

    open_q = [q for q in report["quirks"] if q["state"] == "still_open"]
    lines.append(f"Known spec-vs-behaviour gaps still open: {len(open_q)}")
    for q in open_q:
        lines.append(f"  - [{q['id']}] {q['endpoint']}: {q['summary']}")
    lines.append("")
    lines.append("DRIFT: review needed" if report["drift"] else "No drift.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------

def snapshot(catalog, spec, today=None, source_ref=None):
    """Return a copy of the catalog with every `spec` block refreshed."""
    _validate_catalog(catalog)
    out = json.loads(json.dumps(catalog))
    missing = []
    for ep in out["endpoints"]:
        block = spec_block(spec, ep["method"], ep["path"])
        if block is None:
            missing.append(f"{ep['method']} {ep['path']}")
            continue
        ep["spec"] = block
    if missing:
        raise DriftError("not in spec, fix the catalog first: " + "; ".join(missing))
    out["snapshot"] = {
        "info_version": spec.version,
        "taken": (today or date.today()).isoformat(),
    }
    if source_ref:
        out["snapshot"]["source_ref"] = source_ref
    return out


_SCALAR_LIST = re.compile(r"\[\s*((?:\"(?:[^\"\\]|\\.)*\"|-?\d+|true|false|null)"
                          r"(?:,\s*(?:\"(?:[^\"\\]|\\.)*\"|-?\d+|true|false|null))*)\s*\]")


def dump_catalog(catalog):
    """indent=2 JSON with lists of scalars kept on one line (reviewable diffs)."""
    text = json.dumps(catalog, indent=2, ensure_ascii=False)
    return _SCALAR_LIST.sub(lambda m: "[" + re.sub(r",\n\s*", ", ", m.group(1)) + "]", text) + "\n"


# --------------------------------------------------------------------------
# probe (live, GET only)
# --------------------------------------------------------------------------

_STATUS_LINE = re.compile(r"^HTTP/\S+\s+(\d{3})", re.MULTILINE)


def gh_get(path, runner=subprocess.run):
    """GET `path` with `gh api -i`; return (status, body or None)."""
    cmd = ["gh", "api", "--method", "GET", "-i", path.lstrip("/")]
    assert cmd[2:4] == ["--method", "GET"]  # read-only by construction
    result = runner(cmd, capture_output=True, text=True, timeout=60)
    out = result.stdout or ""
    m = _STATUS_LINE.search(out)
    status = int(m.group(1)) if m else None
    head, sep, body = out.replace("\r\n", "\n").partition("\n\n")
    text = body if sep else ""
    try:
        parsed = json.loads(text) if text.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return status, parsed


def _fill(path, params):
    def sub(m):
        return params.get(m.group(0)[1:-1], m.group(0))
    return _PARAM.sub(sub, path)


def probe(catalog, spec, owner_repo, runner=subprocess.run):
    """GET every tracked GET endpoint on `owner_repo` and compare with the spec."""
    _validate_catalog(catalog)
    owner, _, repo = owner_repo.partition("/")
    if not owner or not repo or "/" in repo:
        raise DriftError("probe needs OWNER/REPO")
    params = {"owner": owner, "repo": repo, "path": "README.md"}

    status, repo_body = gh_get(f"/repos/{owner}/{repo}", runner)
    if status != 200 or not isinstance(repo_body, dict):
        raise DriftError(f"GET /repos/{owner}/{repo} returned {status}")
    branch = repo_body.get("default_branch") or "main"
    params.update({"branch": branch})
    _, rulesets = gh_get(f"/repos/{owner}/{repo}/rulesets", runner)
    if isinstance(rulesets, list) and rulesets and isinstance(rulesets[0], dict):
        params["ruleset_id"] = str(rulesets[0].get("id"))

    results = []
    for ep in catalog["endpoints"]:
        if ep["method"].upper() != "GET":
            continue
        tmpl = ep["path"]
        if norm(tmpl).endswith("matching-refs/{}"):
            local = dict(params, ref="heads/")
        elif norm(tmpl).endswith("commits/{}"):
            local = dict(params, ref=branch)
        else:
            local = params
        path = _fill(tmpl, local)
        if _PARAM.search(path):
            results.append({"endpoint": f"GET {tmpl}", "skipped": "unresolved parameter"})
            continue
        status, body = gh_get(path, runner)
        op = spec.operation("GET", tmpl)
        codes = spec.status_codes(op) if op else []
        fields, required = spec.fields(spec.response_schema(op)) if op else ([], [])
        item = {"endpoint": f"GET {tmpl}", "status": status,
                "status_documented": str(status) in codes}
        sample = body[0] if isinstance(body, list) and body else body
        if isinstance(sample, dict) and status and 200 <= status < 300:
            keys = set(sample)
            item["undocumented_fields"] = sorted(keys - set(fields))
            item["missing_required_fields"] = sorted(set(required) - keys)
        known = set(map(str, (ep.get("observed") or {}).get("status_codes") or []))
        item["new"] = (not item["status_documented"] and str(status) not in known) or bool(
            item.get("undocumented_fields") or item.get("missing_required_fields"))
        results.append(item)
    return {"repo": owner_repo, "results": results,
            "drift": any(r.get("new") for r in results)}


def format_probe(report):
    lines = [f"Live GET probe of {report['repo']}", ""]
    for r in report["results"]:
        if "skipped" in r:
            lines.append(f"  {r['endpoint']}: skipped ({r['skipped']})")
            continue
        flag = "ok" if r["status_documented"] else "UNDOCUMENTED STATUS"
        line = f"  {r['endpoint']}: {r['status']} [{flag}]"
        if r.get("undocumented_fields"):
            line += f" undocumented fields: {', '.join(r['undocumented_fields'])}"
        if r.get("missing_required_fields"):
            line += f" missing required: {', '.join(r['missing_required_fields'])}"
        if r.get("new"):
            line += "  <- not in catalog"
        lines.append(line)
    lines += ["", "DRIFT: review needed" if report["drift"] else "Nothing new."]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="api_drift.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--catalog", default=DEFAULT_CATALOG, help="catalog JSON (default: next to this script)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="diff the catalog against a REST description")
    c.add_argument("--spec", required=True, help="api.github.com(.2022-11-28).json from github/rest-api-description")
    c.add_argument("--source", default=DEFAULT_SOURCE, help="package to scan for API paths (default: gh_safe_repo)")
    c.add_argument("--no-source", action="store_true", help="skip the source scan")
    c.add_argument("--json", action="store_true")

    s = sub.add_parser("snapshot", help="accept the spec's current state as the baseline")
    s.add_argument("--spec", required=True)
    s.add_argument("--source-ref", help="where the spec came from, e.g. the rest-api-description commit SHA")

    r = sub.add_parser("probe", help="live, read-only GET probe of a repo you admin")
    r.add_argument("repo", metavar="OWNER/REPO")
    r.add_argument("--spec", required=True)
    r.add_argument("--json", action="store_true")
    return p


def main(argv=None, runner=subprocess.run):
    args = build_parser().parse_args(argv)
    try:
        catalog = load_json(args.catalog)
        spec = Spec(load_json(args.spec))
        if args.cmd == "check":
            report = check(catalog, spec, None if args.no_source else args.source)
            print(json.dumps(report, indent=2) if args.json else format_check(report))
            return 1 if report["drift"] else 0
        if args.cmd == "snapshot":
            new = snapshot(catalog, spec, source_ref=args.source_ref)
            with open(args.catalog, "w", encoding="utf-8") as f:
                f.write(dump_catalog(new))
            print(f"snapshot updated to spec {spec.version} in {args.catalog}")
            return 0
        report = probe(catalog, spec, args.repo, runner)
        print(json.dumps(report, indent=2) if args.json else format_probe(report))
        return 1 if report["drift"] else 0
    except DriftError as e:
        print(f"api_drift: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
