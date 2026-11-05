from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType

import yaml


ROOT = Path(__file__).resolve().parents[1]
CODE = ROOT / "dsl" / "code"


def load_module(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkflowLogicTests(unittest.TestCase):
    def test_generated_dsl_is_reproducible(self) -> None:
        generator = load_module(ROOT / "scripts" / "build_dsl.py")
        expected = generator.build_dsl(
            provider=generator.DEFAULT_PROVIDER,
            model="gpt-4.1-mini",
            service_url="http://museum:8080",
            plugin_identifier=generator.DEFAULT_PLUGIN,
        )
        actual = yaml.safe_load(
            (ROOT / "dsl" / "memory-museum.yml").read_text(encoding="utf-8")
        )
        self.assertEqual(actual, expected)
        self.assertEqual(len(actual["workflow"]["graph"]["nodes"]), 20)
        self.assertEqual(len(actual["workflow"]["graph"]["edges"]), 27)

    def test_privacy_guard_accepts_and_blocks(self) -> None:
        main = load_module(CODE / "privacy_guard.py").main
        allowed = main("我把一片香菜忘在冰箱冷冻层，第二天它硬得像书签。")
        self.assertEqual(allowed["ok"], "yes")
        self.assertIn("香菜", allowed["memory_text"])
        for value in (
            "api_key: sk-this-should-not-be-stored",
            "someone@example.com",
            "手机号 13800138000",
            "",
            "长" * 1201,
        ):
            with self.subTest(value=value[:20]):
                result = main(value)
                self.assertEqual(result["ok"], "no")
                self.assertTrue(result["reason"])
                self.assertEqual(result["memory_text"], "")

    def test_prepare_check_parses_service_envelope(self) -> None:
        main = load_module(CODE / "prepare_check.py").main
        ready = main(
            200,
            {
                "ok": True,
                "status": "ready",
                "memory_context": "旧展品：《冰箱里的绿色书签》",
                "content_bytes": 24,
                "memory_refs": [
                    {
                        "family": "atomic-memory",
                        "artifact_id": "art_1",
                        "revision": 1,
                    }
                ],
            },
        )
        empty = main(
            200,
            {"ok": True, "status": "empty", "memory_context": "", "memory_refs": []},
        )
        unavailable = main(
            200,
            {
                "ok": False,
                "status": "unavailable",
                "error": {"message": "offline"},
            },
        )
        self.assertEqual(ready["ok"], "yes")
        self.assertEqual(ready["memory_status"], "ready")
        self.assertIn("绿色书签", ready["memory_context"])
        self.assertEqual(empty["ok"], "yes")
        self.assertEqual(empty["memory_status"], "empty")
        self.assertEqual(unavailable["ok"], "no")

    def test_parse_exhibit_builds_plain_backend_fields(self) -> None:
        main = load_module(CODE / "parse_exhibit.py").main
        model_output = json.dumps(
            {
                "exhibit_title": "《香菜的最后晚餐》",
                "era": "冰箱纪·第一次结霜",
                "artifacts": ["香菜", "冷冻层", "绿色书签"],
                "curator_note": "它没有坏，只是选择了另一种时间。",
                "visitor_echo": "它会在下一次结霜时认出你。",
                "echo_kind": "fresh",
                "echo_reference": "",
            },
            ensure_ascii=False,
        )
        result = main(model_output, "我把一片香菜忘在冰箱冷冻层。")
        self.assertEqual(result["memory_kind"], "memory_museum_exhibit")
        self.assertIn("香菜", result["memory_text"])
        self.assertLessEqual(len(result["memory_description"]), 190)
        self.assertIn("新入藏", result["answer_markdown"])

    def test_parse_exhibit_fallback_and_remember_body(self) -> None:
        parse = load_module(CODE / "parse_exhibit.py").main
        parsed = parse("这不是 JSON", "一件没写完的小事")
        self.assertIn("《一件还在路上的小事》", parsed["answer_markdown"])

        remember = load_module(CODE / "remember_body.py").main
        body = remember(
            "alice",
            "turn-1",
            parsed["memory_kind"],
            parsed["memory_text"],
            parsed["memory_description"],
            parsed["memory_reason"],
        )
        self.assertEqual(body["ok"], "yes")
        payload = json.loads(body["body"])
        self.assertEqual(payload["user_id"], "alice")
        self.assertEqual(payload["kind"], "memory_museum_exhibit")

    def test_receipt_check_reports_real_id_or_honest_failure(self) -> None:
        main = load_module(CODE / "receipt_check.py").main
        success = main(
            200,
            {
                "ok": True,
                "records": [
                    {
                        "artifact": {
                            "family": "atomic-memory",
                            "artifact_id": "art_test",
                            "revision": 1,
                        }
                    }
                ],
                "source": {"source_id": "museum-source"},
            },
            "展签",
        )
        failed = main(
            200,
            {"ok": False, "status": "unknown"},
            "展签",
        )
        self.assertEqual(success["ok"], "yes")
        self.assertIn("art_test", success["display"])
        self.assertEqual(failed["ok"], "no")
        self.assertIn("没有返回确定的写入结果", failed["display"])


if __name__ == "__main__":
    unittest.main()
