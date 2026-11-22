"""What Anthropic's prompt cache costs, and how a receipt says so.

A cache read or write is a prompt token, so `input_tokens` stays every prompt
token, and the receipt says how many of them the cache served and wrote, priced
apart from the entered input rate: a write at a quarter more (twice for the hour
lifetime), a read at the model's share - a tenth on Claude, less on the newest.
Admission reserves a Claude prompt as if all of it were written, the most it can
cost; the runtime's breakpoints are in `test_messages_runtime.PromptCachingTests`.
"""

from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from platform_core import ai, gateway
from platform_core.agent_runtime.usage import claude_cache, claude_turn, claude_usage
from platform_core.models import AIUsage
from platform_core.services import record_ai_usage

from . import test_documents


def config(model="claude-sonnet-5", provider="claude"):
    return SimpleNamespace(model=model, provider=provider, input_rate=Decimal("3"),
                           output_rate=Decimal("15"))


class PricingTests(SimpleTestCase):
    def test_a_read_costs_the_models_share_of_the_input_rate(self):
        self.assertEqual(ai.cache_read_share("claude-sonnet-5"), Decimal("0.1"))
        self.assertEqual(ai.cache_read_share("claude-opus-5-5"), Decimal("0.05"))
        self.assertEqual(ai.cache_read_share("claude-opus-5-5[1m]"), Decimal("0.05"))
        self.assertEqual(ai.cache_read_share("claude-fable-5-2"), Decimal("0.025"))
        # A name that only starts like a listed one is not it.
        self.assertEqual(ai.cache_read_share("claude-opus-5"), Decimal("0.1"))
        # Not Claude: never priced below the whole input rate.
        self.assertEqual(ai.cache_read_share("gpt-5.6-sol"), Decimal("1"))

    def test_writes_cost_a_quarter_more_and_an_hour_twice(self):
        # $3/M: 100 plain, 1,000 read at 0.1, 200 written (50 for an hour), 10 out at $15/M.
        self.assertEqual(
            ai.cost(config(), 1300, 10, cache_read=1000, cache_write=200, cache_write_long=50),
            ((Decimal(100) * 3 + Decimal(1000) * 3 * Decimal("0.1")
              + Decimal(150) * 3 * Decimal("1.25") + Decimal(50) * 3 * 2 + 10 * 15)
             / Decimal(1000000)).quantize(Decimal("0.00000001")),
        )
        # With nothing cached it is the price it always was.
        self.assertEqual(ai.cost(config(), 1300, 10), Decimal("0.00405000"))

    def test_more_cached_than_the_prompt_is_refused(self):
        with self.assertRaises(ValidationError):
            ai.cost(config(), 100, 10, cache_read=80, cache_write=40)
        with self.assertRaises(ValidationError):
            ai.cost(config(), 100, 10, cache_write=10, cache_write_long=20)

    def test_admission_reserves_a_claude_prompt_as_written_to_the_cache(self):
        claude, openai = config(), config("gpt-5.6-sol", "openai")
        self.assertEqual(gateway.prompt_rate(claude), Decimal("3.75"))
        self.assertEqual(gateway.prompt_rate(openai), Decimal("3"))
        # The reservation is never below what a whole written prompt costs.
        prompt = 3000 // gateway.CHARACTERS_PER_TOKEN + 1
        self.assertGreaterEqual(
            gateway.estimate(claude, 3000, 100),
            ai.cost(claude, prompt, 100, cache_write=prompt),
        )


class UsageTests(SimpleTestCase):
    def test_every_prompt_token_counts_and_the_cache_says_which(self):
        raw = {"input_tokens": 100, "output_tokens": 5, "cache_read_input_tokens": 900,
               "cache_creation_input_tokens": 50,
               "cache_creation": {"ephemeral_5m_input_tokens": 30,
                                  "ephemeral_1h_input_tokens": 20}}
        self.assertEqual(claude_usage(raw), (1050, 5))
        self.assertEqual(claude_cache(raw), (900, 50, 20))
        turn = claude_turn("msg_01", raw)
        self.assertEqual((turn.prompt_tokens, turn.cache_read, turn.cache_write,
                          turn.cache_write_long), (1050, 900, 50, 20))

    def test_unreported_is_none_and_nonsense_is_an_error(self):
        self.assertEqual(claude_cache({"input_tokens": 1, "output_tokens": 1}), (0, 0, 0))
        self.assertEqual(claude_cache({"cache_read_input_tokens": None}), (0, 0, 0))
        for raw in (
            {"cache_read_input_tokens": -1},
            {"cache_read_input_tokens": True},
            {"cache_creation_input_tokens": 5, "cache_creation": {"ephemeral_1h_input_tokens": 9}},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                claude_cache(raw)


class ReceiptTests(TestCase):
    def setUp(self):
        test_documents.DocumentTests.setUp(self)

    def receipt(self, **counts):
        return record_ai_usage(
            actor=self.owner, application_id=self.app.pk, provider="Anthropic",
            model="claude-sonnet-5", request_id="msg_01", input_tokens=1000, output_tokens=10,
            amount=Decimal("0.001"), purpose="chat", **counts,
        )

    def test_a_receipt_keeps_its_cache_counts(self):
        self.receipt(cache_read_tokens=600, cache_write_tokens=300)
        row = AIUsage.objects.get()
        self.assertEqual((row.input_tokens, row.cache_read_tokens, row.cache_write_tokens),
                         (1000, 600, 300))
        # Replayed with other counts, it is a conflict, not a second bill.
        with self.assertRaises(ValidationError):
            self.receipt(cache_read_tokens=0, cache_write_tokens=0)

    def test_cache_counts_are_part_of_the_prompt(self):
        with self.assertRaises(ValidationError):
            self.receipt(cache_read_tokens=900, cache_write_tokens=200)
        with self.assertRaises(ValidationError):
            self.receipt(cache_read_tokens=-1)
        self.assertFalse(AIUsage.objects.exists())
