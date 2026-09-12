import asyncio
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from agents import Runner
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from openai import DefaultAsyncHttpxClient

from platform_core.llm_agents import _run, completion


class AgentsSDKTests(SimpleTestCase):
    def response(self, text="The deadline is **thirty days** [1].", usage=True):
        data = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "configured-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
        }
        if usage:
            data["usage"] = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        return data

    def call(self, **kwargs):
        return completion(
            SimpleNamespace(model="configured-model"),
            "Explain it",
            [{"title": "Policy", "excerpt": "Thirty days"}],
            "application-key",
            **kwargs,
        )

    def http_client(self, handler):
        return DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(handler), follow_redirects=False, trust_env=False
        )

    def test_real_sdk_preserves_context_security_settings_and_usage(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(200, json=self.response(), headers={"x-request-id": "req-test"})

        with (
            patch(
                "platform_core.llm_agents._http_client",
                side_effect=lambda: self.http_client(handler),
            ),
            patch("platform_core.llm_agents.Runner.run", wraps=Runner.run) as run,
        ):
            result, answer = self.call(
                history=[SimpleNamespace(question="Refund deadline?", answer="Thirty days")]
            )
        self.assertEqual(answer, "The deadline is **thirty days** [1].")
        self.assertEqual(result["usage"]["prompt_tokens"], 100)
        self.assertEqual(result["id"], "req-test")
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(str(request.url), "https://api.openai.com/v1/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer application-key")
        body = json.loads(request.content)
        self.assertEqual(body["model"], "configured-model")
        self.assertFalse(body["store"])
        self.assertEqual(body["max_completion_tokens"], 1024)
        self.assertEqual(
            [m["role"] for m in body["messages"]], ["system", "user", "assistant", "user"]
        )
        self.assertEqual(body["messages"][1]["content"], "Refund deadline?")
        self.assertTrue(run.call_args.kwargs["run_config"].tracing_disabled)
        self.assertFalse(run.call_args.kwargs["run_config"].trace_include_sensitive_data)
        self.assertEqual(run.call_args.kwargs["max_turns"], 1)
        self.assertFalse(run.call_args.args[0].tools)
        self.assertFalse(run.call_args.args[0].handoffs)

    def test_provider_errors_are_sanitized_and_not_retried(self):
        for status in [401, 429, 500, 302]:
            with self.subTest(status=status):
                calls = []

                def handler(request, calls=calls, status=status):
                    calls.append(request)
                    return httpx2.Response(
                        status,
                        json={"error": {"message": "private-data"}},
                        headers={"location": "https://example.invalid"},
                    )

                with patch(
                    "platform_core.llm_agents._http_client",
                    side_effect=lambda: self.http_client(handler),
                ):
                    with self.assertRaises(ValidationError) as error:
                        self.call()
                self.assertNotIn("private-data", str(error.exception))
                self.assertEqual(len(calls), 1)

    def test_missing_usage_and_blank_answers_do_not_become_success(self):
        for data in [self.response(usage=False), self.response(text=" ")]:
            with self.subTest(data=data):
                with patch(
                    "platform_core.llm_agents._http_client",
                    side_effect=lambda data=data: self.http_client(
                        lambda request: httpx2.Response(200, json=data)
                    ),
                ):
                    with self.assertRaises(ValidationError):
                        self.call()

    def test_concurrent_applications_use_separate_credentials(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(200, json=self.response())

        async def both():
            return await asyncio.gather(
                *[_run("configured-model", f"Question-{i}", [], f"key-{i}", []) for i in range(2)]
            )

        with patch(
            "platform_core.llm_agents._http_client", side_effect=lambda: self.http_client(handler)
        ):
            asyncio.run(both())
        actual = {
            r.headers["authorization"]: json.loads(r.content)["messages"][-1]["content"]
            for r in requests
        }
        self.assertEqual(len(actual), 2)
        for i in range(2):
            self.assertEqual(json.loads(actual[f"Bearer key-{i}"])["question"], f"Question-{i}")

    def test_catalog_models_use_responses_api_and_normalize_usage(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx2.Response(
                200,
                json={
                    "id": "resp-test",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "gpt-5.6-luna",
                    "output": [
                        {
                            "id": "msg-test",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": "Answer [1].", "annotations": []}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 5,
                        "total_tokens": 17,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens_details": {"reasoning_tokens": 0},
                    },
                },
                headers={"x-request-id": "request-test"},
            )

        with patch(
            "platform_core.llm_agents._http_client", side_effect=lambda: self.http_client(handler)
        ):
            result, answer = completion(
                SimpleNamespace(model="gpt-5.6-luna"), "Question", [], "key"
            )
        self.assertEqual(str(requests[0].url), "https://api.openai.com/v1/responses")
        self.assertEqual(result["usage"]["prompt_tokens"], 12)
        self.assertEqual(answer, "Answer [1].")
        body = json.loads(requests[0].content)
        self.assertFalse(body["store"])
        self.assertEqual(body["max_output_tokens"], 1024)
