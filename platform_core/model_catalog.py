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

#: Search embeddings are OpenAI's: Anthropic has no embeddings endpoint. They
#: use the application's own OpenAI credential, like any OpenAI purpose.
EMBEDDING_CHOICES = [
    (
        "OpenAI",
        [
            ("openai:text-embedding-3-small", "text-embedding-3-small"),
            ("openai:text-embedding-3-large", "text-embedding-3-large"),
        ],
    ),
    ("Custom", [("custom", "Custom / existing model ID")]),
]
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


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
    "reranking": (
        "Reorders the twenty best verified citations so the most useful come first, for graph "
        "answers, Code Factory evidence and keyword search. A paid call on every question - "
        "switch it on only after `manage.py benchmark --rerank` shows it helps. It can only "
        "reorder evidence that was already verified, never add to it."
    ),
    "document_typing": (
        "Names the type of a document the rules leave as Other, when they are not confident. "
        "The model picks one type from the registry and quotes the lines that show it; any "
        "quote not found in the document exactly leaves it Other. A paid call per new or "
        "changed document the rules cannot type, never repeated for the same text. A type a "
        "person sets always wins."
    ),
    "document_review": (
        "Reviews a document for clarity, testability, contradictions and obvious gaps, as the "
        "type it is - only when an owner asks on its page. Every finding quotes the passage it "
        "is about and is dropped if that quote is not in the document. Advisory: the rule-based "
        "content check and its score are unchanged."
    ),
    "code_review": (
        "Reviews a change in Code Graph - a folder's working changes, a branch or a pull "
        "request - only when someone presses Ask AI on the review. Every finding quotes the "
        "changed lines, the code around them or the requirement it rests on, and is dropped "
        "if a quote is not found exactly. The same change is never paid for twice."
    ),
    "code_docs": (
        "Describes each component of a repository's generated documentation in plain words, "
        "only when someone asks on the Documentation tab. Every paragraph quotes the code it "
        "rests on and is dropped if a quote is not in the file; it is labelled as AI-written."
    ),
    "embeddings": (
        "Finds where to start looking by meaning, so a question worded differently from the "
        "source still reaches it. Relationships and passages are embedded when an AI graph run "
        "is requested; each question is embedded when it is asked. A vector only chooses "
        "starting points - every citation is still verified against its source. Uses this "
        "application's OpenAI credential; enter your contracted input price (output is not "
        "billed)."
    ),
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
