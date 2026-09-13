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


# Graph extraction is materially harder than chat: it must find relationships,
# quote them exactly and name both entities. Small fast models produce thin or
# rejected results, so the settings page recommends a stronger one for that task.
GRAPH_GENERATION_ADVICE = (
    "Graph generation needs a strong reasoning model. Claude Sonnet or a GPT-5.6 model "
    "extracts far more usable relationships here than a small fast model such as Haiku, "
    "which tends to return few relationships or ones that fail quote verification. "
    "Chat can stay on a cheaper model."
)
RECOMMENDED_GRAPH_MODEL = "claude:claude-sonnet-5"
