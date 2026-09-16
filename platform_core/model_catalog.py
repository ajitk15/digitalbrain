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


# One line per purpose, so the settings page can say what each model is actually
# for. These used to live as an if/elif chain inside the template, which covered
# three of the five purposes and fell through to the chat wording for the rest.
PURPOSE_NOTES = {
    "chat": "Answers conversational questions from documents and recent messages.",
    "graph_generation": (
        "Adds AI relationships when sources or these settings change. Disabled, the "
        "structural graph is still built locally at no cost. Enabled runs may incur charges."
    ),
    "graph_retrieval": (
        "Uses the saved graph and its source evidence to answer in Chat's Graph answer mode."
    ),
    "conversation_title": "Names each conversation from its first exchange. A short, cheap call.",
    "plan_drafting": "Drafts Code Factory change plans from a ticket and the knowledge graph.",
}


# Published list prices in USD per million tokens, for the models this catalog
# offers. Shown in AI settings as a reference only: rates are entered per
# application because a contracted price can differ from list, and what the
# platform records must be what the operator is actually billed.
#
# Anthropic first-party rates, verified 2026-09-13. Amazon Bedrock and Vertex AI
# are partner-operated and priced separately. OpenAI models are deliberately
# absent rather than guessed - check the provider's own pricing page.
LIST_PRICES = {
    "claude:claude-fable-5-1": ("10.00", "50.00"),
    "claude:claude-opus-5": ("5.00", "25.00"),
    "claude:claude-sonnet-5": ("2.00", "10.00"),
    "claude:claude-haiku-4-5-20251001": ("1.00", "5.00"),
}

PRICING_NOTE = (
    "Published list prices per million tokens, for reference. Enter the price you are "
    "actually billed: recorded costs are estimates calculated from these rates, so a "
    "wrong rate makes every cost report wrong by the same factor."
)


def price_reference():
    """Rows for the AI settings pricing table, in catalog order."""
    rows = []
    for _, options in MODEL_CHOICES:
        for value, label in options:
            if value in LIST_PRICES:
                inputs, outputs = LIST_PRICES[value]
                rows.append({"label": label, "input": inputs, "output": outputs})
    return rows
