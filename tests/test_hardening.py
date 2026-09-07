"""Review regressions. All API behavior here is mocked; no credentials needed."""

import copy
import hashlib
import json
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
from pydantic import model_validator

import test_v2 as base

packaging = base.module("tools.package_release")


class ContractTests(base.OfflineTest):
    def test_wire_projection_is_non_mutating_and_preserves_fields_and_constraints(self):
        for contract in (base.contracts.ComposerResponse, base.models.DirectorDraft):
            with self.subTest(contract=contract.__name__):
                before = contract.model_json_schema()
                wire = base.contracts.api_schema(contract)
                self.assertEqual(before, contract.model_json_schema())
                base.contracts.assert_strict_objects(wire)

                def compare(host, sent):
                    for key in base.contracts.API_OMITTED_KEYWORDS:
                        self.assertNotIn(key, sent)
                    for key, value in host.items():
                        if key in base.contracts.API_OMITTED_KEYWORDS:
                            continue
                        if key in ("properties", "$defs"):
                            self.assertEqual(set(value), set(sent[key]))
                            for name, child in value.items():
                                compare(child, sent[key][name])
                        elif key == "items":
                            compare(value, sent[key])
                        elif key == "anyOf":
                            self.assertEqual(len(value), len(sent[key]))
                            for left, right in zip(value, sent[key]):
                                compare(left, right)
                        else:
                            self.assertEqual(value, sent[key])

                compare(before, wire)
        self.assertIn(
            "title", base.contracts.api_schema(base.models.DirectorDraft)["properties"]
        )

    def test_strict_object_checks_reject_root_union_missing_required_and_extra_properties(self):
        valid = base.contracts.api_schema(base.models.DirectorDraft)
        missing = copy.deepcopy(valid)
        missing["$defs"]["Stage"]["required"].remove("prompt")
        extra = copy.deepcopy(valid)
        extra["$defs"]["Binding"]["additionalProperties"] = True
        for schema in (missing, extra, {"type": "array"}, {**valid, "anyOf": []}):
            with self.subTest(schema=schema), self.assertRaises(base.DomainError):
                base.contracts.assert_strict_objects(schema)

    def test_host_string_and_array_limits_remain_enforced(self):
        for value in (
            {"final_prompt": "x" * 32001, "warnings": []},
            {"final_prompt": "", "warnings": []},
            {"final_prompt": " ", "warnings": []},
            {"final_prompt": "ok", "warnings": ["x" * 2001]},
            {"final_prompt": "ok", "warnings": ["x"] * 65},
        ):
            with self.assertRaises(base.DomainError):
                base.contracts.parse_response(
                    base.contracts.ComposerResponse, json.dumps(value)
                )

    def test_composer_and_director_send_projected_schema(self):
        prepared = base.composer.prepare_request(request="POP", specification=base.SKILL)
        for contract in (base.contracts.ComposerResponse, base.models.DirectorDraft):
            payload = base.composer.structured_payload(prepared, contract, "Rules")
            self.assertEqual(
                payload["text"]["format"]["schema"], base.contracts.api_schema(contract)
            )
            self.assertTrue(payload["text"]["format"]["strict"])

    def test_stage_profiles_are_enum_and_media_mismatch_is_still_rejected(self):
        schema = base.contracts.api_schema(base.models.DirectorDraft)
        choices = schema["$defs"]["Stage"]["properties"]["target_profile"]["enum"]
        self.assertEqual(set(choices), base.core.IMAGE_TARGETS | base.core.TEXT_TARGETS)
        bad = base.draft()
        bad["stages"][0]["target_profile"] = "nonexistent_profile"
        with self.assertRaises(base.DomainError):
            base.contracts.validate(base.models.DirectorDraft, bad)
        bad["stages"][0]["target_profile"] = "text_generation"
        with self.assertRaises(base.DomainError):
            base.core.validate_draft(bad)

    def test_diagnostics_show_known_field_and_error_type_not_unknown_key_or_values(self):
        value = {
            "final_prompt": "ok",
            "warnings": [42],
            "PRIVATE_KEY_SENTINEL": "PRIVATE_VALUE",
        }
        with self.assertRaises(base.DomainError) as caught:
            base.contracts.parse_response(base.contracts.ComposerResponse, json.dumps(value))
        message = str(caught.exception)
        self.assertIn("$.warnings[0]: string_type", message)
        self.assertIn("<unknown>: extra_forbidden", message)
        self.assertNotIn("PRIVATE", message)
        self.assertIsNone(caught.exception.__context__)

    def test_diagnostics_do_not_echo_custom_validator_context(self):
        class BadContract(base.contracts.Contract):
            value: str

            @model_validator(mode="after")
            def reject(self):
                raise ValueError("PRIVATE_VALIDATOR_SENTINEL")

        with self.assertRaises(base.DomainError) as caught:
            base.contracts.parse_response(BadContract, '{"value":"PRIVATE_INPUT"}')
        self.assertIn("value_error", str(caught.exception))
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_diagnostics_are_bounded(self):
        value = {
            "final_prompt": "ok",
            "warnings": [],
            **{f"PRIVATE_{i}": i for i in range(100)},
        }
        with self.assertRaises(base.DomainError) as caught:
            base.contracts.validate(base.contracts.ComposerResponse, value)
        self.assertLess(len(str(caught.exception)), 500)
        self.assertNotIn("PRIVATE", str(caught.exception))


class WarningTests(base.OfflineTest):
    def test_japanese_uppercase_and_out_of_range_image_labels(self):
        for prompt in (
            "image4を参照",
            "参照image4",
            "Image4を使う",
            "Use IMAGE4.",
            "Use image5.",
            "image0",
        ):
            with self.subTest(prompt=prompt):
                result = base.composer.image_reference_warnings(prompt, ["image1"])
                self.assertEqual(len(result), 1)

    def test_connected_labels_and_embedded_ascii_names_do_not_warn(self):
        for prompt in (
            "image1の商品",
            "Image1",
            "myimage4",
            "image4x",
            "image4_backup",
            "image12x",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(base.composer.image_reference_warnings(prompt, ["image1"]), [])

    def test_image_warning_keeps_final_prompt_unchanged(self):
        prompt = "image1の商品で、image4を参考にPOP什器をデザイン。"
        client, _ = base.mock_client({"final_prompt": prompt, "warnings": []})
        result = base.composer.PromptComposer(client=client).compose(
            specification=base.SKILL, request="POP", images=[("image1", base.IMAGE)]
        )
        self.assertEqual(result.final_prompt, prompt)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("image4", result.warnings[0])

    def test_director_checks_local_binding_slots_not_source_names(self):
        stage = base.stage(
            bindings=[{"slot": "image1", "source_type": "input", "source_id": "image2"}]
        )
        stage["prompt"] = "image1を使い、image3は参考にする。"
        client, _ = base.mock_client(base.draft([stage]))
        plan, _ = base.planner.DirectorPlanner(client=client).plan(
            request="POP", specification=base.SKILL, images=[("image2", base.IMAGE)]
        )
        self.assertEqual(len(plan["warnings"]), 1)
        self.assertIn("image3", plan["warnings"][0])
        self.assertEqual(plan["stages"][0]["prompt"], stage["prompt"])

    def test_warning_merge_is_deduplicated_and_bounded(self):
        self.assertEqual(base.composer.merge_warnings(["a"], ["a", "b"]), ("a", "b"))
        warnings = base.composer.merge_warnings([str(i) for i in range(100)])
        self.assertEqual(len(warnings), 64)
        self.assertIn("omitted", warnings[-1])


class PixelIdentityTests(base.OfflineTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="usd-pixels-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = base.assets.ArtifactStore(self.root / "results", trusted_base=self.root)

    def test_png_encoding_changes_file_hash_but_not_pixel_hash(self):
        original = self.store.save_image(base.IMAGE)
        save = Image.Image.save

        def uncompressed(picture, fp, format=None, **kwargs):
            return save(picture, fp, format=format, **{**kwargs, "compress_level": 0})

        with patch.object(Image.Image, "save", uncompressed):
            second = self.store.save_image(base.IMAGE)
        self.assertEqual(original["fingerprint"], second["fingerprint"])
        self.assertNotEqual(original["file_sha256"], second["file_sha256"])
        np.testing.assert_array_equal(
            self.store.load_image(original), self.store.load_image(second)
        )

    def test_repeated_record_reuses_exact_artifact_without_encoding_or_new_file(self):
        plan = base.hydrate()
        _, ticket, running = base.start(plan)
        with patch.object(base.director_nodes, "_artifacts", return_value=self.store):
            done, _ = base.director_nodes.RecordImageNode().record(
                plan, running, ticket, base.IMAGE
            )
            with patch.object(
                Image.Image, "save", side_effect=AssertionError("Must not re-encode")
            ):
                repeated, _ = base.director_nodes.RecordImageNode().record(
                    plan, done, ticket, base.IMAGE
                )
        self.assertEqual(done, repeated)
        self.assertEqual(len(list(self.store.root.glob("*.png"))), 1)

    def test_legacy_record_and_session_keep_their_exact_shape_and_identity(self):
        current = self.store.save_image(base.IMAGE)
        legacy = {
            "kind": "image",
            "fingerprint": current["file_sha256"],
            "value": current["value"],
        }
        self.assertEqual(base.state.validate_artifact(legacy), legacy)
        plan = base.hydrate()
        _, ticket, running = base.start(plan)
        done = base.state.record_result(
            plan, running, ticket, status="succeeded", result=legacy
        )
        sessions = base.sessions.DirectorSessionStore(
            self.root / "sessions", trusted_base=self.root
        )
        snapshot = sessions.save("legacy", plan, done)
        self.assertEqual(sessions.load("legacy")["ledger"], done)
        with patch.object(base.director_nodes, "_artifacts", return_value=self.store):
            repeated, _ = base.director_nodes.RecordImageNode().record(
                plan, done, ticket, base.IMAGE
            )
        self.assertEqual(done, repeated)
        self.assertEqual(repeated["entries"][0]["attempts"][0]["result"], legacy)
        self.assertEqual(sessions.load("legacy"), snapshot)

    def test_file_tampering_and_pixel_hash_tampering_are_both_rejected(self):
        artifact = self.store.save_image(base.IMAGE)
        forged = {**artifact, "fingerprint": "1" * 64}
        with self.assertRaises(base.DomainError):
            self.store.load_image(forged)
        path = self.store.root / artifact["value"]
        path.write_bytes(path.read_bytes() + b"changed metadata")
        with self.assertRaises(base.DomainError):
            self.store.load_image(artifact)
        with self.assertRaises(base.DomainError):
            self.store.save_image(base.IMAGE, expected_artifact=artifact)

    def test_changed_pixels_rejected_before_writing_for_legacy_and_current(self):
        current = self.store.save_image(base.IMAGE)
        legacy = {
            "kind": "image",
            "fingerprint": current["file_sha256"],
            "value": current["value"],
        }
        for previous in (current, legacy):
            with self.assertRaises(base.DomainError):
                self.store.save_image(base.IMAGE + 0.1, expected_artifact=previous)
        self.assertEqual(len(list(self.store.root.glob("*.png"))), 1)

    def test_identity_uses_rgb8_and_dimensions_not_float_strides(self):
        pixels = np.arange(24, dtype=np.uint8).reshape(2, 4, 3)
        frame = pixels.astype(np.float32)[None] / 255
        first = self.store.save_image(frame)
        second = self.store.save_image(np.asfortranarray(frame))
        self.assertEqual(first, second)
        transposed = self.store.save_image(frame.transpose(0, 2, 1, 3))
        self.assertNotEqual(first["fingerprint"], transposed["fingerprint"])
        quantized_same = self.store.save_image(frame + 0.0001)
        self.assertEqual(first["fingerprint"], quantized_same["fingerprint"])

    def test_text_artifacts_do_not_gain_image_metadata(self):
        text = base.assets.text_artifact("hello")
        self.assertEqual(set(text), {"kind", "fingerprint", "value"})
        with self.assertRaises(base.DomainError):
            base.state.validate_artifact({**text, "file_sha256": "1" * 64})

    def test_filename_is_addressed_by_file_hash(self):
        artifact = self.store.save_image(base.IMAGE)
        data = (self.store.root / artifact["value"]).read_bytes()
        self.assertEqual(artifact["value"], hashlib.sha256(data).hexdigest() + ".png")
        self.assertEqual(artifact["file_sha256"], hashlib.sha256(data).hexdigest())


class PackagingTests(base.OfflineTest):
    def test_allowlist_excludes_secrets_and_development_files_without_reading_them(self):
        read = Path.read_bytes

        def guarded(path):
            self.assertNotEqual(path.name, "ush_config.json")
            return read(path)

        with patch.object(Path, "read_bytes", guarded):
            contents = packaging.release_contents(base.ROOT)
        names = "\n".join(contents)
        for excluded in (
            "ush_config.json",
            "/tests/",
            "/tools/",
            "/.github/",
            "AGENTS.md",
            "DEVELOPMENT.md",
            "requirements-dev.txt",
            "ruff.toml",
        ):
            self.assertNotIn(excluded, names)
        self.assertIn(packaging.PACKAGE_NAME + "/.gitignore", contents)

    def test_zip_round_trip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="usd-package-") as temporary:
            target = Path(temporary) / "release.zip"
            summary = packaging.build_release(base.ROOT, target)
            data = target.read_bytes()
            self.assertEqual(summary["sha256"], hashlib.sha256(data).hexdigest())
            with self.assertRaises(FileExistsError):
                packaging.build_release(base.ROOT, target)
            self.assertEqual(target.read_bytes(), data)

    def test_packaging_rejects_credential_in_runtime_source(self):
        read = Path.read_bytes

        def injected(path):
            if path.name == "README.md":
                return b"sk-" + b"SYNTHETIC" * 5
            return read(path)

        with patch.object(Path, "read_bytes", injected), self.assertRaises(ValueError):
            packaging.release_contents(base.ROOT)


class SmokeHarnessTests(base.OfflineTest):
    def test_no_live_flag_means_no_config_or_network_access(self):
        smoke = base.module("tools.live_smoke")
        with (
            patch.object(sys, "argv", ["live_smoke.py"]),
            patch.object(Path, "read_text", side_effect=AssertionError("No config reads")),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(smoke.main(), 0)
        self.assertIn("Not run", output.getvalue())

    def test_smoke_harness_is_two_bounded_multimodal_calls_with_mock_http(self):
        import httpx
        import openai

        smoke = base.module("tools.live_smoke")
        calls = []

        def handler(request):
            payload = json.loads(request.content)
            calls.append(payload)
            self.assertEqual(payload["max_output_tokens"], 2048)
            self.assertEqual(payload["reasoning"], {"effort": "low"})
            self.assertFalse(payload["store"])
            self.assertNotIn("tools", payload)
            self.assertEqual(
                sum(item["type"] == "input_image" for item in payload["input"][0]["content"]), 2
            )
            if payload["text"]["format"]["name"] == "ComposerResponse":
                value = {
                    "final_prompt": "image1を主色、image2をアクセントにする。",
                    "warnings": [],
                }
            else:
                value = base.draft(
                    [
                        base.stage(
                            bindings=[
                                {
                                    "slot": "image1",
                                    "source_type": "input",
                                    "source_id": "image1",
                                },
                                {
                                    "slot": "image2",
                                    "source_type": "input",
                                    "source_id": "image2",
                                },
                            ]
                        )
                    ]
                )
            return httpx.Response(
                200,
                json={
                    "id": "resp_mock",
                    "object": "response",
                    "created_at": 0,
                    "model": "gpt-5.6",
                    "status": "completed",
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
                                    "text": json.dumps(value),
                                }
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 100, "total_tokens": 200},
                },
            )

        with (
            patch.object(sys, "path", [str(base.ROOT), *sys.path]),
            httpx.Client(transport=httpx.MockTransport(handler), trust_env=False) as http,
            openai.OpenAI(
                api_key="mock-key", base_url=smoke.API_BASE, max_retries=0, http_client=http
            ) as client,
        ):
            result = smoke.run_checks(client)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(row["validated"] for row in result["results"]))
        self.assertNotIn("mock-key", json.dumps(result))

    def test_template_key_must_be_empty_even_without_sk_prefix(self):
        read = Path.read_bytes

        def injected(path):
            if path.name == "ush_config.example.json":
                return b'{"openai_api_key":"not-empty"}'
            return read(path)

        with patch.object(Path, "read_bytes", injected), self.assertRaises(ValueError):
            packaging.release_contents(base.ROOT)
