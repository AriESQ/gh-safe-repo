"""Tests for tools/api_drift/api_drift.py (#27).

Everything runs against a tiny hand-written OpenAPI fixture; the real 13MB
REST description is never loaded and nothing touches the network. The live
`probe` subcommand gets a fake `runner` in place of subprocess.run.
"""

import copy
import importlib.util
import json
import os
import re
import textwrap
from datetime import date

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "tools", "api_drift", "api_drift.py")

_spec = importlib.util.spec_from_file_location("api_drift", SCRIPT)
api_drift = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api_drift)


def make_spec_doc():
    """A miniature REST description covering every shape the checks read."""
    return {
        "openapi": "3.0.3",
        "info": {"version": "9.9.9"},
        "paths": {
            "/repos/{owner}/{repo}": {
                "get": {"responses": {
                    "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/repo"}}}},
                    "404": {"description": "Not Found"},
                }},
                "patch": {
                    "description": "Update a repository.",
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "security_and_analysis": {
                                "type": "object",
                                "properties": {
                                    "secret_scanning": {"type": "object", "description": "Toggle scanning."},
                                },
                            },
                        },
                    }}}},
                    "responses": {"200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/repo"}}}}},
                },
            },
            "/repos/{owner}/{repo}/toggle": {
                "get": {"responses": {
                    "200": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {"enabled": {"type": "boolean"}, "paused": {"type": "boolean"}},
                        "required": ["enabled"],
                    }}}},
                    "404": {"description": "Not Found if not enabled"},
                }},
                "put": {
                    "requestBody": {"content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "enabled": {"type": "boolean"},
                            "patterns": {"type": "array", "description": "Wildcards and tags allowed."},
                        },
                        "required": ["enabled"],
                    }}}},
                    "responses": {"204": {"description": "No Content"}},
                },
            },
            "/repos/{owner}/{repo}/items": {
                "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "array", "items": {"$ref": "#/components/schemas/item"},
                }}}}}},
            },
            "/repos/{owner}/{repo}/items/{item_id}": {
                "get": {"responses": {"200": {"content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/item",
                }}}}}},
                "put": {"responses": {"200": {"description": "OK"}}},
            },
            "/repos/{owner}/{repo}/contents/{path}": {
                "get": {"responses": {"200": {"description": "OK"}, "404": {"description": "Not Found"}}},
            },
            "/repos/{owner}/{repo}/branches/{branch}/protection": {
                "get": {"responses": {"200": {"description": "OK"}}},
            },
            "/repos/{owner}/{repo}/branches/{branch}/{other}": {
                "get": {"responses": {"200": {"description": "OK"}}},
            },
            "/repos/{owner}/{repo}/old": {
                "get": {"deprecated": True, "responses": {"200": {"description": "OK"}}},
            },
            "/user": {"get": {"responses": {"200": {"description": "OK"}}}},
        },
        "components": {"schemas": {
            "repo": {"allOf": [
                {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
                {"type": "object", "properties": {"name": {"type": "string"}, "default_branch": {"type": "string"}}},
            ]},
            "item": {"oneOf": [
                {"type": "object", "properties": {"id": {}, "a": {}}, "required": ["id", "a"]},
                {"type": "object", "properties": {"id": {}, "b": {}}, "required": ["id"]},
            ]},
        }},
    }


@pytest.fixture
def spec():
    return api_drift.Spec(make_spec_doc())


def make_catalog(spec, endpoints, quirks=()):
    eps = []
    for method, path, extra in endpoints:
        ep = {"method": method, "path": path, **extra}
        if "spec" not in ep:
            ep["spec"] = api_drift.spec_block(spec, method, path)
        eps.append(ep)
    return {"snapshot": {"info_version": "9.9.9"}, "endpoints": eps, "quirks": list(quirks)}


def quirk(qid, method, path, check):
    return {"id": qid, "method": method, "path": path, "summary": qid, "check": check}


# --------------------------------------------------------------------------


class TestNormAndTemplate:
    def test_norm_drops_param_names_query_and_trailing_slash(self):
        assert api_drift.norm("/repos/{owner}/{repo}/x/?per_page=100") == "/repos/{}/{}/x"
        assert api_drift.norm("/") == "/"

    def test_template_matches_other_param_names(self, spec):
        assert spec.template("/repos/{o}/{r}/toggle") == "/repos/{owner}/{repo}/toggle"

    def test_concrete_segment_matches_a_parameter(self, spec):
        assert spec.template("/repos/{}/{}/contents/README.md") == "/repos/{owner}/{repo}/contents/{path}"

    def test_most_literal_template_wins(self, spec):
        assert spec.template("/repos/{}/{}/branches/{}/protection") == \
            "/repos/{owner}/{repo}/branches/{branch}/protection"

    def test_unknown_path(self, spec):
        assert spec.template("/repos/{}/{}/nope") is None

    def test_not_openapi(self):
        with pytest.raises(api_drift.DriftError):
            api_drift.Spec({"info": {}})


class TestFields:
    def test_ref_and_allof_merge(self, spec):
        op = spec.operation("GET", "/repos/{owner}/{repo}")
        assert spec.fields(spec.response_schema(op)) == (["default_branch", "id", "name"], ["id"])

    def test_array_items_and_oneof_required_intersection(self, spec):
        op = spec.operation("GET", "/repos/{owner}/{repo}/items")
        assert spec.fields(spec.response_schema(op)) == (["a", "b", "id"], ["id"])

    def test_descend_into_nested_object(self, spec):
        op = spec.operation("PATCH", "/repos/{owner}/{repo}")
        props, _ = spec.fields(spec.request_schema(op), ("security_and_analysis",))
        assert props == ["secret_scanning"]

    def test_no_json_response(self, spec):
        op = spec.operation("PUT", "/repos/{owner}/{repo}/toggle")
        assert spec.response_schema(op) is None
        assert spec.fields(None) == ([], [])

    def test_spec_block(self, spec):
        block = api_drift.spec_block(spec, "PUT", "/repos/{owner}/{repo}/toggle")
        assert block == {
            "status_codes": ["204"], "response_fields": [], "response_required": [],
            "request_fields": ["enabled", "patterns"], "request_required": ["enabled"],
            "deprecated": False,
        }
        assert api_drift.spec_block(spec, "DELETE", "/repos/{owner}/{repo}/toggle") is None


class TestConditions:
    T = "/repos/{owner}/{repo}/toggle"

    def holds(self, spec, method, path, check):
        return api_drift.condition_holds(spec, quirk("q", method, path, check))[0]

    def test_method_absent(self, spec):
        assert self.holds(spec, "PATCH", "/repos/{owner}/{repo}/items/{item_id}", {"type": "method_absent"}) is True
        assert self.holds(spec, "PUT", "/repos/{owner}/{repo}/items/{item_id}", {"type": "method_absent"}) is False

    def test_status_undocumented(self, spec):
        assert self.holds(spec, "GET", self.T, {"type": "status_undocumented", "status": "403"}) is True
        assert self.holds(spec, "GET", self.T, {"type": "status_undocumented", "status": 404}) is False

    def test_status_documented(self, spec):
        assert self.holds(spec, "GET", self.T, {"type": "status_documented", "status": "404"}) is True
        assert self.holds(spec, "GET", self.T, {"type": "status_documented", "status": "409"}) is False

    def test_field_optional_and_required(self, spec):
        assert self.holds(spec, "GET", self.T, {"type": "field_optional", "field": "paused"}) is True
        assert self.holds(spec, "GET", self.T, {"type": "field_optional", "field": "enabled"}) is False
        assert self.holds(spec, "PUT", self.T, {"type": "field_required", "field": "enabled", "where": "request"}) is True
        # A field that vanished entirely no longer matches either way.
        assert self.holds(spec, "GET", self.T, {"type": "field_required", "field": "gone"}) is False

    def test_field_absent_with_descend(self, spec):
        check = {"type": "field_absent", "where": "request", "descend": ["security_and_analysis"]}
        assert self.holds(spec, "PATCH", "/repos/{owner}/{repo}", dict(check, field="autosubmit")) is True
        assert self.holds(spec, "PATCH", "/repos/{owner}/{repo}", dict(check, field="secret_scanning")) is False

    def test_text_lacks(self, spec):
        op_check = {"type": "text_lacks", "pattern": "eventual"}
        assert self.holds(spec, "PATCH", "/repos/{owner}/{repo}", op_check) is True
        assert self.holds(spec, "PATCH", "/repos/{owner}/{repo}", dict(op_check, pattern="update")) is False
        field_check = {"type": "text_lacks", "field": "patterns", "where": "request"}
        assert self.holds(spec, "PUT", self.T, dict(field_check, pattern="docker")) is True
        assert self.holds(spec, "PUT", self.T, dict(field_check, pattern="WILDCARD")) is False  # case-insensitive
        nested = {"type": "text_lacks", "field": "secret_scanning", "where": "request",
                  "descend": ["security_and_analysis"], "pattern": "toggle"}
        assert self.holds(spec, "PATCH", "/repos/{owner}/{repo}", nested) is False

    def test_none_type_always_holds(self, spec):
        assert self.holds(spec, "GET", "/nowhere", {"type": "none"}) is True

    def test_missing_endpoint_is_none(self, spec):
        assert self.holds(spec, "GET", "/nowhere", {"type": "status_undocumented", "status": "403"}) is None

    def test_unknown_type(self, spec):
        with pytest.raises(api_drift.DriftError):
            self.holds(spec, "GET", self.T, {"type": "bogus"})


class TestCheck:
    T = "/repos/{owner}/{repo}/toggle"

    def test_clean(self, spec):
        cat = make_catalog(spec, [("GET", self.T, {}), ("PUT", self.T, {})])
        report = api_drift.check(cat, spec)
        assert report["drift"] is False
        assert report["spec_changes"] == [] and report["notices"] == []

    def test_status_code_change_is_drift(self, spec):
        cat = make_catalog(spec, [("GET", self.T, {})])
        cat["endpoints"][0]["spec"]["status_codes"] = ["200"]
        report = api_drift.check(cat, spec)
        assert report["drift"] is True
        assert report["spec_changes"] == [
            {"endpoint": f"GET {self.T}", "change": "status_codes", "added": ["404"], "removed": []}
        ]

    def test_new_response_field_is_a_notice_not_drift(self, spec):
        cat = make_catalog(spec, [("GET", self.T, {})])
        cat["endpoints"][0]["spec"]["response_fields"] = ["enabled"]
        report = api_drift.check(cat, spec)
        assert report["drift"] is False
        assert report["notices"][0]["added"] == ["paused"]

    def test_removed_response_field_is_drift(self, spec):
        cat = make_catalog(spec, [("GET", self.T, {})])
        cat["endpoints"][0]["spec"]["response_fields"] = ["enabled", "paused", "gone"]
        assert api_drift.check(cat, spec)["drift"] is True

    def test_path_missing_suggests_similar(self, spec):
        cat = make_catalog(spec, [("GET", "/repos/{owner}/{repo}/v2/toggle", {"spec": {}})])
        change = api_drift.check(cat, spec)["spec_changes"][0]
        assert change["change"] == "path_missing"
        assert "/repos/{owner}/{repo}/toggle" in change["detail"]

    def test_method_missing(self, spec):
        cat = make_catalog(spec, [("DELETE", self.T, {"spec": {}})])
        assert api_drift.check(cat, spec)["spec_changes"][0]["change"] == "method_missing"

    def test_newly_deprecated(self, spec):
        cat = make_catalog(spec, [("GET", "/repos/{owner}/{repo}/old", {})])
        cat["endpoints"][0]["spec"]["deprecated"] = False
        changes = api_drift.check(cat, spec)["spec_changes"]
        assert [c["change"] for c in changes] == ["deprecated"]

    def test_quirk_states(self, spec):
        cat = make_catalog(spec, [("GET", self.T, {})], [
            quirk("open", "GET", self.T, {"type": "status_undocumented", "status": "403"}),
            quirk("fixed", "GET", self.T, {"type": "status_undocumented", "status": "404"}),
            quirk("gone", "GET", "/repos/{owner}/{repo}/gone", {"type": "status_documented", "status": "200"}),
        ])
        report = api_drift.check(cat, spec)
        states = {q["id"]: q["state"] for q in report["quirks"]}
        assert states == {"open": "still_open", "fixed": "spec_changed", "gone": "endpoint_missing"}
        assert report["drift"] is True

    def test_open_quirks_alone_are_not_drift(self, spec):
        cat = make_catalog(spec, [("GET", self.T, {"observed": {"status_codes": ["200", "403"]}})], [
            quirk("open", "GET", self.T, {"type": "status_undocumented", "status": "403"}),
        ])
        report = api_drift.check(cat, spec)
        assert report["drift"] is False
        assert report["undocumented"] == [
            {"endpoint": f"GET {self.T}", "status_codes": ["403"], "evidence": None}
        ]

    def test_observed_undocumented_status_without_quirk(self, spec):
        cat = make_catalog(spec, [("GET", self.T, {"observed": {"status_codes": ["200", "409"]}})])
        report = api_drift.check(cat, spec)
        assert report["drift"] is True
        assert "409" in report["uncataloged_gaps"][0]["detail"]

    def test_observed_undocumented_field(self, spec):
        cat = make_catalog(spec, [("GET", self.T, {"observed": {"response_fields": ["enabled", "extra"]}})])
        assert api_drift.check(cat, spec)["undocumented"][0]["response_fields"] == ["extra"]

    @pytest.mark.parametrize("mutate", [
        lambda c: c.pop("quirks"),
        lambda c: c["endpoints"].append(copy.deepcopy(c["endpoints"][0])),
        lambda c: c["endpoints"].append({"path": "/x"}),
        lambda c: c["quirks"].extend([quirk("a", "GET", "/x", {}), quirk("a", "GET", "/y", {})]),
        lambda c: c["quirks"].append({"id": "nocheck", "method": "GET", "path": "/x"}),
    ])
    def test_invalid_catalog(self, spec, mutate):
        cat = make_catalog(spec, [("GET", self.T, {})])
        mutate(cat)
        with pytest.raises(api_drift.DriftError):
            api_drift.check(cat, spec)

    def test_format_check_mentions_each_section(self, spec):
        cat = make_catalog(spec, [("GET", self.T, {})], [
            quirk("fixed", "GET", self.T, {"type": "status_undocumented", "status": "404"}),
        ])
        text = api_drift.format_check(api_drift.check(cat, spec))
        assert "[fixed]" in text and "DRIFT" in text


class TestSourceScan:
    def write_pkg(self, tmp_path, code):
        pkg = tmp_path / "pkg"
        pkg.mkdir()
        (pkg / "mod.py").write_text(textwrap.dedent(code))
        return str(pkg)

    def test_finds_paths(self, tmp_path, spec):
        pkg = self.write_pkg(tmp_path, '''
            TOGGLES = {"x": ("toggle", "a-b")}
            def f(client, o, r, item, sep):
                client.repo_path(o, r, "toggle")
                client.repo_path(o, r, f"items/{item}")
                client.get_json(client.repo_path(o, r, "contents/README.md"))
                client.call_api("GET", "/user")
                client.call_json("PUT", f"{base}/items")
                client.repo_path(o, r)
                client.repo_path(o, r, "missing/endpoint")
                x = "branches/main/protection"  # implicit, spec-gated
                y = "not-an-endpoint"          # implicit, not in spec: ignored
                z = "/"                        # never a path
        ''')
        found = api_drift.scan_source(pkg, spec)
        assert set(found) == {
            "/repos/{}/{}/toggle", "/repos/{}/{}/items/{}", "/repos/{}/{}/contents/{}",
            "/user", "/repos/{}/{}/items", "/repos/{}/{}", "/repos/{}/{}/missing/endpoint",
            "/repos/{}/{}/branches/{}/protection",
        }
        assert found["/repos/{}/{}/missing/endpoint"]["in_spec"] is False
        assert found["/repos/{}/{}/toggle"]["in_spec"] is True

    def test_fstring_pieces_are_not_scanned_alone(self, tmp_path, spec):
        # "/repos/" and "/items" are fragments of the f-string, not paths.
        pkg = self.write_pkg(tmp_path, '''
            def f(o, r):
                return f"/repos/{o}/{r}/items"
        ''')
        assert set(api_drift.scan_source(pkg, spec)) == {"/repos/{}/{}/items"}

    def test_check_reports_untracked_and_unknown(self, tmp_path, spec):
        pkg = self.write_pkg(tmp_path, '''
            def f(client, o, r):
                client.repo_path(o, r, "toggle")
                client.repo_path(o, r, "items")
                client.repo_path(o, r, "missing")
        ''')
        cat = make_catalog(spec, [("GET", "/repos/{owner}/{repo}/toggle", {})])
        report = api_drift.check(cat, spec, pkg)
        assert [u["path"] for u in report["untracked_code_paths"]] == ["/repos/{owner}/{repo}/items"]
        assert [u["path"] for u in report["code_paths_not_in_spec"]] == ["/repos/{}/{}/missing"]
        assert report["drift"] is True

    def test_syntax_error(self, tmp_path, spec):
        pkg = self.write_pkg(tmp_path, "def (:\n")
        with pytest.raises(api_drift.DriftError):
            api_drift.scan_source(pkg, spec)


class TestSnapshot:
    def test_refreshes_blocks_and_metadata(self, spec):
        cat = make_catalog(spec, [("GET", "/repos/{owner}/{repo}/toggle", {"spec": {"status_codes": ["200"]}})])
        out = api_drift.snapshot(cat, spec, today=date(2026, 10, 27), source_ref="abc")
        assert out["endpoints"][0]["spec"]["status_codes"] == ["200", "404"]
        assert out["snapshot"] == {"info_version": "9.9.9", "taken": "2026-10-27", "source_ref": "abc"}
        assert cat["endpoints"][0]["spec"]["status_codes"] == ["200"]  # input untouched

    def test_missing_endpoint_refuses(self, spec):
        cat = make_catalog(spec, [("GET", "/repos/{owner}/{repo}/gone", {"spec": {}})])
        with pytest.raises(api_drift.DriftError, match="gone"):
            api_drift.snapshot(cat, spec)

    def test_dump_keeps_scalar_lists_on_one_line(self):
        data = {"a": ["x", "y, z"], "b": [{"c": [1, 2]}], "d": []}
        text = api_drift.dump_catalog(data)
        assert '"a": ["x", "y, z"]' in text
        assert '"c": [1, 2]' in text
        assert json.loads(text) == data


class FakeGh:
    """Stands in for subprocess.run; answers `gh api -i` GETs from a table."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        status, body = self.table.get(cmd[-1], (404, {"message": "Not Found"}))
        text = "" if body is None else json.dumps(body)

        class R:
            returncode = 0 if status < 400 else 1
            stdout = f"HTTP/2.0 {status} X\r\nContent-Type: application/json\r\n\r\n{text}"
            stderr = ""
        return R()


class TestProbe:
    T = "/repos/{owner}/{repo}/toggle"

    def table(self, **over):
        t = {
            "repos/o/r": (200, {"id": 1, "name": "r", "default_branch": "trunk"}),
            "repos/o/r/items": (200, [{"id": 7, "a": 1}]),
            "repos/o/r/items/7": (200, {"id": 7, "b": 2}),
            "repos/o/r/toggle": (200, {"enabled": True, "paused": False}),
            "repos/o/r/branches/trunk/protection": (200, {}),
        }
        t.update(over)
        return t

    def catalog(self, spec, **observed):
        return make_catalog(spec, [
            ("GET", "/repos/{owner}/{repo}", {}),
            ("GET", self.T, {"observed": observed} if observed else {}),
            ("PUT", self.T, {}),
            ("GET", "/repos/{owner}/{repo}/items/{item_id}", {}),
            ("GET", "/repos/{owner}/{repo}/branches/{branch}/protection", {}),
        ])

    def test_get_only_and_params_filled(self, spec):
        cat = self.catalog(spec)
        cat["endpoints"][3]["path"] = "/repos/{owner}/{repo}/items/{ruleset_id}"
        gh = FakeGh(self.table(**{"repos/o/r/rulesets": (200, [{"id": 7}])}))
        report = api_drift.probe(cat, spec, "o/r", runner=gh)
        for cmd in gh.calls:
            assert cmd[:4] == ["gh", "api", "--method", "GET"]
        paths = [c[-1] for c in gh.calls]
        assert "repos/o/r/branches/trunk/protection" in paths
        assert "repos/o/r/items/7" in paths
        assert not any(r["endpoint"].startswith("PUT") for r in report["results"])
        assert report["drift"] is False

    def test_unresolved_parameter_is_skipped(self, spec):
        report = api_drift.probe(self.catalog(spec), spec, "o/r", runner=FakeGh(self.table()))
        skipped = [r for r in report["results"] if "skipped" in r]
        assert [r["endpoint"] for r in skipped] == ["GET /repos/{owner}/{repo}/items/{item_id}"]

    def test_undocumented_status_is_new_unless_cataloged(self, spec):
        gh = FakeGh(self.table(**{"repos/o/r/toggle": (403, {"message": "Upgrade"})}))
        report = api_drift.probe(self.catalog(spec), spec, "o/r", runner=gh)
        toggle = next(r for r in report["results"] if r["endpoint"] == f"GET {self.T}")
        assert toggle["status"] == 403 and toggle["status_documented"] is False
        assert toggle["new"] is True and report["drift"] is True

        gh = FakeGh(self.table(**{"repos/o/r/toggle": (403, {"message": "Upgrade"})}))
        report = api_drift.probe(self.catalog(spec, status_codes=["403"]), spec, "o/r", runner=gh)
        assert report["drift"] is False

    def test_undocumented_and_missing_fields(self, spec):
        gh = FakeGh(self.table(**{"repos/o/r/toggle": (200, {"paused": False, "surprise": 1})}))
        report = api_drift.probe(self.catalog(spec), spec, "o/r", runner=gh)
        toggle = next(r for r in report["results"] if r["endpoint"] == f"GET {self.T}")
        assert toggle["undocumented_fields"] == ["surprise"]
        assert toggle["missing_required_fields"] == ["enabled"]
        assert "surprise" in api_drift.format_probe(report)

    def test_204_without_body(self, spec):
        gh = FakeGh(self.table(**{"repos/o/r/toggle": (204, None)}))
        report = api_drift.probe(self.catalog(spec), spec, "o/r", runner=gh)
        toggle = next(r for r in report["results"] if r["endpoint"] == f"GET {self.T}")
        assert toggle["status"] == 204 and "undocumented_fields" not in toggle

    def test_repo_unreadable(self, spec):
        with pytest.raises(api_drift.DriftError):
            api_drift.probe(self.catalog(spec), spec, "o/r", runner=FakeGh({}))

    @pytest.mark.parametrize("bad", ["o", "o/", "/r", "o/r/x"])
    def test_bad_repo_arg(self, spec, bad):
        with pytest.raises(api_drift.DriftError):
            api_drift.probe(self.catalog(spec), spec, bad, runner=FakeGh({}))


class TestMain:
    def files(self, tmp_path, spec, catalog):
        sp = tmp_path / "spec.json"
        sp.write_text(json.dumps(make_spec_doc()))
        cp = tmp_path / "catalog.json"
        cp.write_text(json.dumps(catalog))
        return str(sp), str(cp)

    def test_exit_0_and_json(self, tmp_path, spec, capsys):
        sp, cp = self.files(tmp_path, spec, make_catalog(spec, [("GET", "/user", {})]))
        assert api_drift.main(["--catalog", cp, "check", "--spec", sp, "--no-source", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["drift"] is False

    def test_exit_1_on_drift(self, tmp_path, spec, capsys):
        cat = make_catalog(spec, [("GET", "/user", {})])
        cat["endpoints"][0]["spec"]["status_codes"] = []
        sp, cp = self.files(tmp_path, spec, cat)
        assert api_drift.main(["--catalog", cp, "check", "--spec", sp, "--no-source"]) == 1
        assert "DRIFT" in capsys.readouterr().out

    def test_exit_2_on_bad_input(self, tmp_path, spec, capsys):
        sp, cp = self.files(tmp_path, spec, {"endpoints": []})
        assert api_drift.main(["--catalog", cp, "check", "--spec", sp]) == 2
        assert api_drift.main(["--catalog", cp, "check", "--spec", str(tmp_path / "nope.json")]) == 2
        assert "api_drift:" in capsys.readouterr().err

    def test_snapshot_writes_catalog(self, tmp_path, spec):
        cat = make_catalog(spec, [("GET", "/user", {"spec": {}})])
        sp, cp = self.files(tmp_path, spec, cat)
        assert api_drift.main(["--catalog", cp, "snapshot", "--spec", sp, "--source-ref", "sha1"]) == 0
        written = json.loads(open(cp).read())
        assert written["endpoints"][0]["spec"]["status_codes"] == ["200"]
        assert written["snapshot"]["source_ref"] == "sha1"

    def test_probe_wired(self, tmp_path, spec, capsys):
        cat = make_catalog(spec, [("GET", "/repos/{owner}/{repo}", {})])
        sp, cp = self.files(tmp_path, spec, cat)
        gh = FakeGh({"repos/o/r": (200, {"id": 1, "default_branch": "main"})})
        assert api_drift.main(["--catalog", cp, "probe", "o/r", "--spec", sp], runner=gh) == 0
        assert "Nothing new" in capsys.readouterr().out


class TestCheckedInCatalog:
    """The real catalog must stay well-formed; no spec needed."""

    @pytest.fixture(scope="class")
    def catalog(self):
        return api_drift.load_json(api_drift.DEFAULT_CATALOG)

    def test_validates(self, catalog):
        api_drift._validate_catalog(catalog)

    def test_every_endpoint_has_a_snapshot(self, catalog):
        assert catalog["snapshot"].get("info_version")
        for ep in catalog["endpoints"]:
            assert "status_codes" in ep.get("spec", {}), ep["path"]

    def test_quirks_point_at_tracked_endpoints(self, catalog):
        tracked = {(e["method"], api_drift.norm(e["path"])) for e in catalog["endpoints"]}
        for q in catalog["quirks"]:
            if q["check"]["type"] == "method_absent":
                assert any(p == api_drift.norm(q["path"]) for _, p in tracked), q["id"]
            else:
                assert (q["method"], api_drift.norm(q["path"])) in tracked, q["id"]

    def test_quirk_fields_and_check_types(self, catalog):
        types = {"method_absent", "status_undocumented", "status_documented", "field_optional",
                 "field_required", "field_absent", "text_lacks", "none"}
        kinds = {"spec_gap", "undocumented_semantics", "cross_endpoint_inconsistency",
                 "eventual_consistency"}
        for q in catalog["quirks"]:
            for f in ("summary", "spec_says", "observed", "workaround", "evidence", "observed_on", "kind"):
                assert q.get(f), (q["id"], f)
            assert q["check"]["type"] in types, q["id"]
            assert q["kind"] in kinds, q["id"]
            date.fromisoformat(q["observed_on"])
            if q["check"]["type"] == "text_lacks":
                re.compile(q["check"]["pattern"])

    def test_evidence_and_used_by_files_exist(self, catalog):
        for q in catalog["quirks"]:
            for ev in q["evidence"]:
                path = ev.split(" ", 1)[0]
                if path.startswith("docs/"):
                    assert os.path.exists(os.path.join(REPO_ROOT, path)), (q["id"], ev)
        for ep in catalog["endpoints"]:
            for f in ep.get("used_by", []):
                assert os.path.exists(os.path.join(REPO_ROOT, f)), (ep["path"], f)

    def test_catalog_file_is_in_canonical_form(self, catalog):
        with open(api_drift.DEFAULT_CATALOG, encoding="utf-8") as f:
            assert f.read() == api_drift.dump_catalog(catalog)
