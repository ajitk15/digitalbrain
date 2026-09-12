"""Provider documentation model suggestions verified 2026-09-12; account access varies."""

MODEL_CHOICES = [
    (
        "OpenAI",
        [
            ("openai:gpt-6-astra", "GPT-6 Astra"),
            ("openai:gpt-5.6-sol", "GPT-5.6 Sol"),
            ("openai:gpt-5.6-terra", "GPT-5.6 Terra"),
            ("openai:gpt-5.6-luna", "GPT-5.6 Luna"),
        ],
    ),
    (
        "Claude",
        [
            ("claude:claude-fable-5-1", "Claude Fable 5.1"),
            ("claude:claude-opus-5", "Claude Opus 5"),
            ("claude:claude-sonnet-5", "Claude Sonnet 5"),
            ("claude:claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
        ],
    ),
    ("Custom", [("custom", "Custom / existing model ID")]),
]
