"""Offline regressions: public source, excluded from the installable ZIP."""

import ast
import copy
import importlib
import importlib.util
import json
import multiprocessing
import socket
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "usd_v2_test"
spec = importlib.util.spec_from_file_location(
    PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
package = importlib.util.module_from_spec(spec)
sys.modules[PACKAGE] = package
config = importlib.import_module(PACKAGE + ".universal_skills.local_config")
# Import registration without ever reading the operator's real secret file.
with patch.object(config, "load_local_config", return_value=config.LocalConfig()):
    spec.loader.exec_module(package)
DIRECTOR_LOADED_BY_DEFAULT = any(
    name.startswith(PACKAGE + ".universal_skills.director") for name in sys.modules
)


def module(name):
    return importlib.import_module(PACKAGE + "." + name)


nodes = module("nodes")
composer = module("universal_skills.composer")
contracts = module("universal_skills.contracts")
errors = module("universal_skills.errors")
specification = module("universal_skills.specification")
transport = module("universal_skills.openai_client")
config = module("universal_skills.local_config")
core = module("universal_skills.director.core")
models = module("universal_skills.director.models")
state = module("universal_skills.director.state")
planner = module("universal_skills.director.planner")
assets = module("universal_skills.director.assets")
sessions = module("universal_skills.director.sessions")
director_nodes = module("director_nodes")
DomainError = errors.UniversalSkillHostError

SKILL = specification.load_specification_content(
    "pop.md", "# POP Skill\nDesign a retail display. Keep explicit label locks."
)
IMAGE = np.full((1, 8, 8, 3), 0.25, dtype=np.float32)
FINAL = "店頭POP什器をデザイン。image1の商品ラベルの字体・相対サイズは変更禁止。見出しは「新登場」。"


class MockResponses:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def create(self, **payload):
        self.calls.append(copy.deepcopy(payload))
        value = self.response
        if isinstance(value, dict):
            value = SimpleNamespace(
                status="completed", output=[], output_text=json.dumps(value, ensure_ascii=False)
            )
        return value


def mock_client(response):
    responses = MockResponses(response)
    return transport.OpenAIResponsesClient(
        client=SimpleNamespace(responses=responses), sdk_module=SimpleNamespace()
    ), responses


def stage(stage_id="new-art", order=1, kind="image", bindings=None):
    return {
        "stage_id": stage_id,
        "order": order,
        "title": "Stage " + str(order),
        "media_kind": kind,
        "target_profile": "gpt_image_2" if kind == "image" else "text_generation",
        "prompt": "Design a retail POP display."
        if kind == "image"
        else "Write one product headline.",
        "input_bindings": bindings or [],
        "parameters": [],
    }


def draft(stages=None):
    return {
        "title": "POP design",
        "summary": "A reusable production plan.",
        "stages": stages or [stage()],
        "warnings": [],
    }


def hydrate(value=None, previous=None, input_assets=None):
    return core.hydrate_plan(
        value or draft(),
        assets=input_assets or [],
        specification_fingerprint="1" * 64,
        brief_fingerprint="2" * 64,
        previous_plan=previous,
    )


def as_draft(plan):
    return copy.deepcopy({key: plan[key] for key in models.DirectorDraft.model_fields})


def start(plan):
    return state.select_work_item(plan, state.create_ledger(plan))


def text_plan():
    return hydrate(draft([stage(kind="text")]))


def process_session_update(base, plan, ledger, barrier, queue):
    with patch.object(socket.socket, "connect", side_effect=AssertionError("Network disabled")):
        store = sessions.DirectorSessionStore(Path(base) / "sessions", trusted_base=Path(base))
        barrier.wait(timeout=10)
        try:
            store.save("race", plan, ledger, expected_session_revision=1)
            queue.put("saved")
        except sessions.DirectorSessionConflictError:
            queue.put("conflict")


class OfflineTest(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(
            socket.socket, "connect", side_effect=AssertionError("Network disabled in v2 tests")
        )
        self.network.start()
        self.addCleanup(self.network.stop)


class ComposerTests(OfflineTest):
    def test_default_registration_is_small_and_director_is_lazy(self):
        self.assertFalse(DIRECTOR_LOADED_BY_DEFAULT)
        self.assertEqual(
            set(package.NODE_CLASS_MAPPINGS),
            {"USH_LoadSpecification", "USH_PromptComposer", "USH_PromptPlanner"},
        )
        self.assertTrue(nodes.PromptPlannerNode.DEPRECATED)

    def test_model_widgets_default_to_luna(self):
        for node in (
            nodes.PromptComposerNode,
            nodes.PromptPlannerNode,
            director_nodes.PlanProjectNode,
        ):
            with self.subTest(node=node.__name__):
                widget_type, options = node.INPUT_TYPES()["required"]["model"]
                self.assertEqual(widget_type, "STRING")
                self.assertEqual(options["default"], "gpt-5.6-luna")

    def test_node_model_defaults_and_explicit_sol_overrides_reach_api(self):
        for owner, node, response in (
            (nodes, nodes.PromptComposerNode, {"final_prompt": FINAL, "warnings": []}),
            (nodes, nodes.PromptPlannerNode, {"final_prompt": FINAL, "warnings": []}),
            (director_nodes, director_nodes.PlanProjectNode, draft()),
        ):
            for selected in (None, "gpt-5.6", "gpt-5.6-sol"):
                with self.subTest(node=node.__name__, model=selected):
                    client, sdk = mock_client(response)
                    settings = {} if selected is None else {"model": selected}
                    with patch.object(owner, "_SHARED_CLIENT", client):
                        getattr(node(), node.FUNCTION)(
                            specification=SKILL,
                            request="Design POP",
                            target_profile="gpt_image_2",
                            **settings,
                        )
                    self.assertEqual(len(sdk.calls), 1)
                    self.assertEqual(sdk.calls[0]["model"], selected or "gpt-5.6-luna")

    def test_python_source_syntax(self):
        for path in ROOT.rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"))

    def test_response_schema_has_exactly_two_fields(self):
        schema = contracts.ComposerResponse.model_json_schema()
        self.assertEqual(set(schema["properties"]), {"final_prompt", "warnings"})
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])

    def test_prompt_passes_unchanged_and_preserves_explicit_locks(self):
        client, sdk = mock_client({"final_prompt": FINAL, "warnings": ["Review dimensions."]})
        result = composer.PromptComposer(client=client).compose(
            request="Design a POP display.", specification=SKILL, images=[("image1", IMAGE)]
        )
        self.assertEqual(result.final_prompt, FINAL)
        self.assertNotIn("Review dimensions", result.final_prompt)
        self.assertEqual(len(sdk.calls), 1)
        payload = sdk.calls[0]
        self.assertFalse(payload["store"])
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(payload["input"][0]["content"][1]["text"], "Reference image1:")
        self.assertIn("Preserve explicit", payload["instructions"])
        self.assertNotIn("source_path", payload["instructions"])

    def test_budget_is_independent_of_reasoning_and_nonce_is_sent(self):
        client, sdk = mock_client({"final_prompt": FINAL, "warnings": []})
        for effort, nonce in (("none", 0), ("high", 1)):
            composer.PromptComposer(client=client).compose(
                request="POP",
                specification=SKILL,
                reasoning_effort=effort,
                max_output_tokens=4096,
                generation_id=nonce,
            )
        self.assertEqual([p["max_output_tokens"] for p in sdk.calls], [4096, 4096])
        self.assertNotEqual(sdk.calls[0]["input"], sdk.calls[1]["input"])
        self.assertNotIn("IS_CHANGED", nodes.PromptComposerNode.__dict__)

    def test_node_contract_connects_to_standard_string(self):
        client, _ = mock_client({"final_prompt": FINAL, "warnings": []})
        with patch.object(nodes, "_SHARED_CLIENT", client):
            output = nodes.PromptComposerNode().compose(SKILL, "Design POP", image1=IMAGE)
        self.assertEqual(nodes.PromptComposerNode.RETURN_TYPES, ("STRING", "STRING"))
        self.assertIs(type(output[0]), str)
        self.assertEqual(output[0], FINAL)

    def test_legacy_widget_order_and_outputs(self):
        required = list(nodes.PromptPlannerNode.INPUT_TYPES()["required"])
        self.assertEqual(
            required,
            [
                "request",
                "specification",
                "target_profile",
                "target_notes",
                "model",
                "reasoning_effort",
                "image_detail",
                "additional_context",
            ],
        )
        client, sdk = mock_client({"final_prompt": FINAL, "warnings": []})
        with patch.object(nodes, "_SHARED_CLIENT", client):
            output = nodes.PromptPlannerNode().plan(
                "Design POP", SKILL, "gpt_image_2", "target note", additional_context="more"
            )
        self.assertEqual(len(output), 5)
        self.assertTrue(all(type(value) is str for value in output))
        self.assertEqual(output[0], FINAL)
        self.assertEqual(json.loads(output[1])["schema_version"], "2.0")
        self.assertIn("target note\\n\\nmore", sdk.calls[0]["input"][0]["content"][0]["text"])

    def test_invalid_output_is_rejected_without_echo(self):
        for raw in [
            '{"final_prompt":"secret","final_prompt":"x","warnings":[]}',
            '{"final_prompt":" ","warnings":[]}',
            '{"final_prompt":NaN,"warnings":[]}',
            '{"final_prompt":"secret","warnings":[],"extra":"secret"}',
            "```json\n{}\n```",
            "[]",
            '{"final_prompt":"secret"}',
        ]:
            with self.subTest(raw=raw):
                with self.assertRaises(DomainError) as error:
                    contracts.parse_response(contracts.ComposerResponse, raw)
                self.assertNotIn("secret", str(error.exception))

    def test_incomplete_and_refusal_are_rejected(self):
        for response in [
            SimpleNamespace(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            ),
            SimpleNamespace(
                status="completed", output_text="secret", output=[{"type": "refusal"}]
            ),
        ]:
            with self.assertRaises(DomainError):
                transport.extract_completed_output_text(response)

    def test_bad_settings_fail_before_call(self):
        for settings in [
            {"max_output_tokens": True},
            {"max_output_tokens": 0},
            {"generation_id": -1},
            {"model": ""},
            {"image_detail": "invalid"},
            {"target_profile": "wrong"},
        ]:
            client, sdk = mock_client({"final_prompt": FINAL, "warnings": []})
            with self.subTest(settings=settings), self.assertRaises(DomainError):
                composer.PromptComposer(client=client).compose(
                    request="POP", specification=SKILL, **settings
                )
            self.assertEqual(len(sdk.calls), 0)

    def test_embedded_skill_round_trip(self):
        loaded = nodes.LoadSpecificationNode().load("", "pop.md", "# Rules\nKeep exact copy.")[
            0
        ]
        self.assertIsNone(loaded["source_path"])
        self.assertEqual(loaded["source_file"], "pop.md")
        self.assertEqual(specification.resolve_specification(loaded), loaded)
        first = nodes.LoadSpecificationNode.IS_CHANGED("", "pop.md", "# A")
        self.assertNotEqual(first, nodes.LoadSpecificationNode.IS_CHANGED("", "pop.md", "# B"))

    def test_embedded_invalid_or_secret_files_rejected(self):
        for name, text in [
            ("ush_config.json", "{}"),
            ("file.exe", "hi"),
            ("bad.json", '{"x":1,"x":2}'),
            ("empty.md", "  "),
            ("big.md", "x" * (256 * 1024 + 1)),
        ]:
            with self.subTest(name=name), self.assertRaises(DomainError):
                specification.load_specification_content(name, text)

    def test_config_opt_in_and_secret_are_external(self):
        # Mock reads, so test credentials never reach a file or API.
        with patch.object(
            Path,
            "read_text",
            return_value='{"openai_api_key":"test-only","enable_director":true}',
        ):
            value = config.load_local_config()
        self.assertTrue(value.enable_director)
        with (
            patch.object(config, "load_local_config", return_value=value),
            patch.dict("os.environ", {"OPENAI_API_KEY": ""}),
        ):
            self.assertEqual(transport._resolve_api_key(), "test-only")
        with (
            patch.object(config, "load_local_config", return_value=value),
            patch.dict("os.environ", {"OPENAI_API_KEY": "environment"}),
        ):
            self.assertEqual(transport._resolve_api_key(), "environment")
        with (
            patch.object(Path, "read_text", return_value='{"enable_director":"true"}'),
            self.assertRaises(DomainError),
        ):
            config.load_local_config()

    def test_client_rejects_nonfinite_timeout(self):
        for value in [float("nan"), float("inf"), -1, True]:
            with self.assertRaises(DomainError):
                transport.OpenAIResponsesClient(timeout_seconds=value)

    def test_surrogate_output_is_rejected(self):
        with self.assertRaises(DomainError):
            contracts.parse_response(
                contracts.ComposerResponse, '{"final_prompt":"\\ud800","warnings":[]}'
            )

    def test_opt_in_registers_director_nodes(self):
        with patch.object(
            config, "load_local_config", return_value=config.LocalConfig(enable_director=True)
        ):
            spec.loader.exec_module(package)
        self.assertEqual(len(package.NODE_CLASS_MAPPINGS), 12)
        with patch.object(config, "load_local_config", return_value=config.LocalConfig()):
            spec.loader.exec_module(package)
        self.assertEqual(len(package.NODE_CLASS_MAPPINGS), 3)


class DirectorTests(OfflineTest):
    def test_director_schema_all_fields_required(self):
        def visit(value):
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertFalse(value["additionalProperties"])
                    self.assertEqual(set(value["required"]), set(value["properties"]))
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(models.DirectorDraft.model_json_schema())

    def test_ids_are_host_assigned_and_order_independent(self):
        original = hydrate(draft([stage(), stage("new-copy", 2, "text")]))
        revision = as_draft(original)
        revision["stages"][0]["order"], revision["stages"][1]["order"] = 2, 1
        revision["stages"].append(stage("new-extra", 3))
        revised = hydrate(revision, original)
        self.assertEqual(original["plan_id"], revised["plan_id"])
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(original["stages"][0]["stage_id"], revised["stages"][1]["stage_id"])
        self.assertTrue(all(s["stage_id"].startswith("stage-") for s in revised["stages"]))

    def test_replan_sends_previous_plan_and_accepts_edited_request(self):
        original = hydrate()
        value = as_draft(original)
        value["stages"][0]["prompt"] = "Change only the camera angle."
        client, sdk = mock_client(value)
        plan, _ = planner.DirectorPlanner(client=client).plan(
            request="Change the camera angle.",
            specification=SKILL,
            previous_plan=original,
            previous_ledger=state.create_ledger(original),
        )
        self.assertEqual(plan["revision"], 2)
        data = json.loads(sdk.calls[0]["input"][0]["content"][0]["text"])
        self.assertEqual(data["previous_plan"], as_draft(original))
        self.assertNotIn("ledger", data)

    def test_stage_prompt_is_not_shared_across_media(self):
        plan = hydrate(draft([stage(), stage("new-copy", 2, "text")]))
        self.assertNotEqual(plan["stages"][0]["prompt"], plan["stages"][1]["prompt"])

    def test_bad_dependencies_and_profiles_fail(self):
        values = []
        duplicate = draft([stage(), stage()])
        values.append(duplicate)
        mismatch = draft()
        mismatch["stages"][0]["target_profile"] = "text_generation"
        values.append(mismatch)
        cycle = draft(
            [
                stage(
                    bindings=[
                        {"slot": "image1", "source_type": "stage", "source_id": "new-art"}
                    ]
                )
            ]
        )
        values.append(cycle)
        unknown = draft(
            [
                stage(
                    bindings=[
                        {"slot": "image1", "source_type": "stage", "source_id": "missing"}
                    ]
                )
            ]
        )
        values.append(unknown)
        wrong_kind = draft(
            [stage(bindings=[{"slot": "text1", "source_type": "input", "source_id": "image1"}])]
        )
        values.append(wrong_kind)
        video = draft()
        video["stages"][0]["media_kind"] = "video"
        values.append(video)
        for value in values:
            with self.subTest(value=value), self.assertRaises(DomainError):
                hydrate(value)

    def test_metadata_revision_reuses_completed_result(self):
        original = text_plan()
        _, ticket, ledger = start(original)
        completed = state.record_result(
            original, ledger, ticket, status="succeeded", result=assets.text_artifact("新登場")
        )
        revision = as_draft(original)
        revision.update(title="New review title", summary="Metadata only.")
        revision["stages"][0].update(title="Changed display label", order=3)
        revised = hydrate(revision, original)
        reused = state.reconcile_ledger(original, completed, revised)
        self.assertEqual(state.current(reused["entries"][0])["status"], "succeeded")
        self.assertEqual(core.definition_hashes(original), core.definition_hashes(revised))

    def test_execution_change_invalidates_it_and_dependents(self):
        first = stage(kind="text")
        second = stage(
            "new-layout",
            2,
            bindings=[{"slot": "text1", "source_type": "stage", "source_id": "new-art"}],
        )
        original = hydrate(draft([first, second]))
        _, ticket, ledger = start(original)
        ledger = state.record_result(
            original, ledger, ticket, status="succeeded", result=assets.text_artifact("新登場")
        )
        revision = as_draft(original)
        revision["stages"][0]["prompt"] += " Use a softer tone."
        revised = hydrate(revision, original)
        hashes_before, hashes_after = (
            core.definition_hashes(original),
            core.definition_hashes(revised),
        )
        self.assertTrue(all(hashes_before[key] != hashes_after[key] for key in hashes_before))
        reused = state.reconcile_ledger(original, ledger, revised)
        self.assertTrue(all(not entry["attempts"] for entry in reused["entries"]))

    def test_failed_duplicate_and_delayed_duplicate_are_idempotent(self):
        plan = text_plan()
        _, ticket, running = start(plan)
        failed = state.record_result(
            plan, running, ticket, status="failed", error="failed once"
        )
        self.assertEqual(
            state.record_result(plan, failed, ticket, status="failed", error="failed once"),
            failed,
        )
        _, retry_ticket, retry = state.select_work_item(
            plan, failed, stage_id=ticket["stage_id"], mode="retry"
        )
        self.assertEqual(retry_ticket["attempt"], 2)
        self.assertNotEqual(ticket["ticket_id"], retry_ticket["ticket_id"])
        self.assertEqual(
            state.record_result(plan, retry, ticket, status="failed", error="failed once"),
            retry,
        )

    def test_cancelled_requires_explicit_retry(self):
        plan = text_plan()
        _, ticket, running = start(plan)
        cancelled = state.record_result(
            plan, running, ticket, status="cancelled", error="operator cancelled"
        )
        with self.assertRaises(DomainError):
            state.select_work_item(plan, cancelled)
        _, retry_ticket, retry = state.select_work_item(
            plan, cancelled, stage_id=ticket["stage_id"], mode="retry"
        )
        result = state.record_result(
            plan, retry, retry_ticket, status="succeeded", result=assets.text_artifact("OK")
        )
        self.assertEqual(state.current(result["entries"][0])["status"], "succeeded")

    def test_running_requires_resume_and_is_cancelled_on_revision(self):
        plan = text_plan()
        _, ticket, running = start(plan)
        _, resumed, unchanged = state.select_work_item(
            plan, running, stage_id=ticket["stage_id"], mode="resume"
        )
        self.assertEqual(resumed, ticket)
        self.assertEqual(unchanged, running)
        revised = hydrate(as_draft(plan), plan)
        reconciled = state.reconcile_ledger(plan, running, revised)
        self.assertEqual(state.current(reconciled["entries"][0])["status"], "cancelled")
        with self.assertRaises(DomainError):
            state.record_result(revised, reconciled, ticket, status="failed", error="late")

    def test_success_duplicate_is_idempotent_and_conflict_rejected(self):
        plan = text_plan()
        _, ticket, running = start(plan)
        result = assets.text_artifact("OK")
        done = state.record_result(plan, running, ticket, status="succeeded", result=result)
        self.assertEqual(
            state.record_result(plan, done, ticket, status="succeeded", result=result), done
        )
        with self.assertRaises(DomainError):
            state.record_result(
                plan, done, ticket, status="succeeded", result=assets.text_artifact("different")
            )

    def test_no_success_without_started_attempt_or_result(self):
        plan = text_plan()
        _, ticket, running = start(plan)
        with self.assertRaises(DomainError):
            state.record_result(
                plan,
                state.create_ledger(plan),
                ticket,
                status="succeeded",
                result=assets.text_artifact("x"),
            )
        with self.assertRaises(DomainError):
            state.record_result(plan, running, ticket, status="succeeded")

    def test_dependencies_resolve_actual_text(self):
        plan = hydrate(
            draft(
                [
                    stage(kind="text"),
                    stage(
                        "new-layout",
                        2,
                        bindings=[
                            {"slot": "text1", "source_type": "stage", "source_id": "new-art"}
                        ],
                    ),
                ]
            )
        )
        _, ticket, running = start(plan)
        with self.assertRaises(DomainError):
            state.select_work_item(plan, running, stage_id=plan["stages"][1]["stage_id"])
        done = state.record_result(
            plan, running, ticket, status="succeeded", result=assets.text_artifact("新登場")
        )
        _, next_ticket, next_ledger = state.select_work_item(plan, done)
        value = director_nodes.ResolveTextNode().resolve(plan, next_ledger, next_ticket)
        self.assertEqual(value, ("新登場",))

    def test_plan_and_ledger_tampering_rejected(self):
        plan = text_plan()
        bad = copy.deepcopy(plan)
        bad["stages"][0]["prompt"] = "tampered"
        with self.assertRaises(DomainError):
            core.validate_director_plan(bad)
        _, ticket, ledger = start(plan)
        ledger["entries"][0]["attempts"][0]["status"] = "stale"
        with self.assertRaises(DomainError):
            state.select_work_item(plan, ledger)

    def test_v1_plan_is_rejected_with_migration_message(self):
        with self.assertRaisesRegex(DomainError, "v1 sessions"):
            core.validate_director_plan({"schema_version": "1.0"})

    def test_director_unsupported_target_fails_before_api(self):
        client, sdk = mock_client(draft())
        with self.assertRaises(DomainError):
            planner.DirectorPlanner(client=client).plan(
                request="Video", specification=SKILL, target_profile="video_generation"
            )
        self.assertEqual(sdk.calls, [])
        self.assertNotIn(
            "video_generation",
            director_nodes.PlanProjectNode.INPUT_TYPES()["required"]["target_profile"][0],
        )

    def test_upstream_result_substitution_invalidates_started_dependency(self):
        plan = hydrate(
            draft(
                [
                    stage(kind="text"),
                    stage(
                        "new-layout",
                        2,
                        bindings=[
                            {"slot": "text1", "source_type": "stage", "source_id": "new-art"}
                        ],
                    ),
                ]
            )
        )
        _, ticket, running = start(plan)
        done = state.record_result(
            plan, running, ticket, status="succeeded", result=assets.text_artifact("Original")
        )
        _, _, dependent = state.select_work_item(plan, done)
        dependent["entries"][0]["attempts"][0]["result"] = assets.text_artifact("Changed")
        with self.assertRaises(DomainError):
            state.validate_ledger_for_plan(plan, dependent)

    def test_director_reference_batch_rejected_before_api(self):
        client, sdk = mock_client(draft())
        with self.assertRaises(DomainError):
            planner.DirectorPlanner(client=client).plan(
                request="Design",
                specification=SKILL,
                images=[("image1", np.repeat(IMAGE, 2, axis=0))],
            )
        self.assertEqual(sdk.calls, [])


class StorageTests(OfflineTest):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory(prefix="usd-v2-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = assets.ArtifactStore(self.base / "results", trusted_base=self.base)

    def test_image_result_is_saved_and_resolved_as_actual_image(self):
        plan = hydrate(
            draft(
                [
                    stage(),
                    stage(
                        "new-copy",
                        2,
                        "text",
                        [{"slot": "image1", "source_type": "stage", "source_id": "new-art"}],
                    ),
                ]
            )
        )
        _, ticket, running = start(plan)
        with patch.object(director_nodes, "_artifacts", return_value=self.store):
            done, image = director_nodes.RecordImageNode().record(plan, running, ticket, IMAGE)
            _, next_ticket, next_ledger = state.select_work_item(plan, done)
            with patch.dict(sys.modules, {"torch": SimpleNamespace(from_numpy=lambda x: x)}):
                resolved = director_nodes.ResolveImageNode().resolve(
                    plan, next_ledger, next_ticket
                )[0]
        self.assertIs(image, IMAGE)
        np.testing.assert_allclose(resolved, IMAGE, atol=1 / 255)
        self.assertEqual(len(list((self.base / "results").glob("*.png"))), 1)

    def test_image_batch_is_not_silently_discarded(self):
        with self.assertRaises(DomainError):
            self.store.save_image(np.repeat(IMAGE, 2, axis=0))

    def test_duplicate_image_save_is_content_addressed(self):
        self.assertEqual(self.store.save_image(IMAGE), self.store.save_image(IMAGE.copy()))
        self.assertEqual(len(list((self.base / "results").glob("*.png"))), 1)

    def test_fake_path_missing_asset_and_traversal_rejected(self):
        with self.assertRaises(DomainError):
            state.validate_artifact(
                {"kind": "image", "value": "never-generated.png", "fingerprint": "1" * 64}
            )
        with self.assertRaises(DomainError):
            self.store.load_image(
                {"kind": "image", "value": "1" * 64 + ".png", "fingerprint": "1" * 64}
            )
        with self.assertRaises(DomainError):
            self.store.load_image(
                {"kind": "image", "value": "../outside.png", "fingerprint": "1" * 64}
            )

    def test_original_image_binding_checks_full_reference(self):
        plan = hydrate(
            draft(
                [
                    stage(
                        bindings=[
                            {"slot": "image1", "source_type": "input", "source_id": "image1"}
                        ]
                    )
                ]
            ),
            input_assets=[
                {"input_name": "image1", "fingerprint": assets.image_fingerprint(IMAGE)}
            ],
        )
        _, ticket, ledger = start(plan)
        result = director_nodes.ResolveImageNode().resolve(plan, ledger, ticket, image1=IMAGE)
        self.assertIs(result[0], IMAGE)
        with self.assertRaises(DomainError):
            director_nodes.ResolveImageNode().resolve(plan, ledger, ticket, image1=IMAGE + 0.1)

    def test_session_atomic_round_trip_and_revision_conflict(self):
        store = sessions.DirectorSessionStore(self.base / "sessions", trusted_base=self.base)
        plan = text_plan()
        ledger = state.create_ledger(plan)
        first = store.save("demo", plan, ledger)
        self.assertEqual(first["session_revision"], 1)
        self.assertEqual(store.load("demo.json")["plan"], plan)
        with self.assertRaises(sessions.DirectorSessionConflictError):
            store.save("demo", plan, ledger)
        second = store.save("demo", plan, ledger, expected_session_revision=1)
        self.assertEqual(second["session_revision"], 2)
        with self.assertRaises(sessions.DirectorSessionConflictError):
            store.save("demo", plan, ledger, expected_session_revision=1)
        self.assertEqual(store.load("demo")["session_revision"], 2)

    def test_concurrent_session_updates_only_one_wins(self):
        store = sessions.DirectorSessionStore(self.base / "sessions", trusted_base=self.base)
        plan = text_plan()
        ledger = state.create_ledger(plan)
        store.save("race", plan, ledger)

        def update(_):
            try:
                store.save("race", plan, ledger, expected_session_revision=1)
                return "saved"
            except sessions.DirectorSessionConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(update, range(2))), ["saved", "conflict"])

    def test_session_unsafe_names_and_external_root_rejected(self):
        store = sessions.DirectorSessionStore(self.base / "sessions", trusted_base=self.base)
        plan = text_plan()
        ledger = state.create_ledger(plan)
        for name in ["../outside", "CON", "a/b", "C:\\outside"]:
            with self.subTest(name=name), self.assertRaises(DomainError):
                store.save(name, plan, ledger)
        with self.assertRaises(DomainError):
            assets.ArtifactStore(self.base.parent / "outside", trusted_base=self.base)

    def test_conflicting_image_result_does_not_leave_orphan(self):
        plan = hydrate()
        _, ticket, ledger = start(plan)
        with patch.object(director_nodes, "_artifacts", return_value=self.store):
            done, _ = director_nodes.RecordImageNode().record(plan, ledger, ticket, IMAGE)
            with self.assertRaises(DomainError):
                director_nodes.RecordImageNode().record(plan, done, ticket, IMAGE + 0.25)
        self.assertEqual(len(list((self.base / "results").glob("*.png"))), 1)

    def test_failed_atomic_replace_preserves_previous_snapshot(self):
        store = sessions.DirectorSessionStore(self.base / "sessions", trusted_base=self.base)
        plan = text_plan()
        ledger = state.create_ledger(plan)
        first = store.save("atomic", plan, ledger)
        with (
            patch.object(sessions.os, "replace", side_effect=OSError("mock storage failure")),
            self.assertRaises(DomainError),
        ):
            store.save("atomic", plan, ledger, expected_session_revision=1)
        self.assertEqual(store.load("atomic"), first)
        self.assertEqual(list((self.base / "sessions").glob("*.tmp")), [])

    def test_v1_session_rejected_without_writing(self):
        store = sessions.DirectorSessionStore(self.base / "sessions", trusted_base=self.base)
        plan = text_plan()
        snapshot = store.save("old", plan, state.create_ledger(plan))
        snapshot["schema_version"] = "1.0"
        with self.assertRaisesRegex(DomainError, "v1 files are not modified"):
            store._decode_snapshot(json.dumps(snapshot).encode(), expected_file="old.json")
        self.assertEqual(store.load("old")["schema_version"], "2.0")

    def test_separate_processes_compare_and_swap(self):
        store = sessions.DirectorSessionStore(self.base / "sessions", trusted_base=self.base)
        plan = text_plan()
        ledger = state.create_ledger(plan)
        store.save("race", plan, ledger)
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(2), context.Queue()
        processes = [
            context.Process(
                target=process_session_update,
                args=(str(self.base), plan, ledger, barrier, queue),
            )
            for _ in range(2)
        ]
        try:
            for process in processes:
                process.start()
            outcomes = [queue.get(timeout=15) for _ in processes]
            self.assertCountEqual(outcomes, ["saved", "conflict"])
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            queue.close()


class ActualSDKTests(OfflineTest):
    def setUp(self):
        super().setUp()
        try:
            import openai
            import httpx
        except ImportError:
            self.skipTest(
                "Install OpenAI/httpx into the private verification environment for SDK-level tests."
            )
        self.openai, self.httpx = openai, httpx

    def sdk_client(self, handler):
        http = self.httpx.Client(transport=self.httpx.MockTransport(handler))
        self.addCleanup(http.close)
        sdk = self.openai.OpenAI(
            api_key="mock-key-not-a-secret", http_client=http, max_retries=0
        )
        self.addCleanup(sdk.close)
        return transport.OpenAIResponsesClient(client=sdk, sdk_module=self.openai)

    def test_real_sdk_composer_request_with_mock_http(self):
        calls = []

        def handler(request):
            calls.append(json.loads(request.content))
            self.assertEqual(request.url.path, "/v1/responses")
            return self.httpx.Response(
                200,
                json={
                    "id": "resp_mock",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": calls[-1]["model"],
                    "output": [
                        {
                            "id": "msg_mock",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "annotations": [],
                                    "text": json.dumps(
                                        {"final_prompt": FINAL, "warnings": []},
                                        ensure_ascii=False,
                                    ),
                                }
                            ],
                        }
                    ],
                },
            )

        client = self.sdk_client(handler)
        result = composer.PromptComposer(client=client).compose(
            request="Design POP", specification=SKILL, images=[("image1", IMAGE)]
        )
        self.assertEqual(result.final_prompt, FINAL)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "gpt-5.6-luna")
        self.assertFalse(calls[0]["store"])
        self.assertNotIn("tools", calls[0])
        self.assertEqual(
            calls[0]["text"]["format"]["schema"],
            contracts.api_schema(contracts.ComposerResponse),
        )

    def test_real_sdk_director_request_with_mock_http(self):
        calls = []

        def handler(request):
            calls.append(json.loads(request.content))
            return self.httpx.Response(
                200,
                json={
                    "id": "resp_mock",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": calls[-1]["model"],
                    "output": [
                        {
                            "id": "msg_mock",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "annotations": [],
                                    "text": json.dumps(draft()),
                                }
                            ],
                        }
                    ],
                },
            )

        plan, ledger = planner.DirectorPlanner(client=self.sdk_client(handler)).plan(
            request="Design one POP image stage",
            specification=SKILL,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "gpt-5.6-luna")
        self.assertEqual(
            calls[0]["text"]["format"]["schema"], contracts.api_schema(models.DirectorDraft)
        )
        self.assertEqual(len(plan["stages"]), 1)
        self.assertEqual(state.validate_ledger_for_plan(plan, ledger), ledger)

    def test_real_sdk_error_messages_do_not_leak_body(self):
        for status in (400, 401, 403, 429, 500):

            def handler(request, status=status):
                return self.httpx.Response(
                    status,
                    json={
                        "error": {
                            "message": "PRIVATE_BODY_SENTINEL",
                            "type": "api_error",
                            "code": "bad_request",
                        }
                    },
                )

            client = self.sdk_client(handler)
            with self.subTest(status=status), self.assertRaises(DomainError) as error:
                composer.PromptComposer(client=client).compose(
                    request="PRIVATE_REQUEST", specification=SKILL
                )
            self.assertNotIn("PRIVATE", str(error.exception))
            self.assertNotIn("mock-key", str(error.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
