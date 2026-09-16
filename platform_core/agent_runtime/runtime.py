"""Shared contract between Django and the provider adapters.

`HistoryTurn` is the seam that lets conversation history stop being a Django
model. Both adapters only ever read `.question` and `.answer`, so the view layer
can hand them plain values instead of ORM instances bound to a request.
"""

from dataclasses import dataclass

HISTORY_TURNS = 8
HISTORY_ANSWER_LIMIT = 4000

#: Model families that take the Responses API rather than Chat Completions, which
#: decides the client class, where the token cap goes and how usage is read back.
#: Defined once: both adapters had their own copy, so a new family would have had
#: to be added twice and would have looked like it worked after the first.
RESPONSES_MODELS = ("gpt-6", "gpt-5.6")


@dataclass(frozen=True)
class HistoryTurn:
    question: str
    answer: str


def recent_turns(history):
    """The last few exchanges, with prior answers truncated to bound the prompt."""
    return [
        HistoryTurn(question=turn.question, answer=turn.answer[:HISTORY_ANSWER_LIMIT])
        for turn in (history or [])[-HISTORY_TURNS:]
    ]


def evidence_payload(citations):
    """Numbered evidence for the prompt.

    `digest` is deliberately excluded: it is a server-side integrity token used to
    verify citations against the database, not something the model needs or should
    be able to echo back.
    """
    return [
        {
            "source": index + 1,
            "id": item.get("id"),
            "title": item["title"],
            "text": item["excerpt"],
        }
        for index, item in enumerate(citations or [])
    ]
