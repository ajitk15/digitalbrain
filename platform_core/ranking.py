"""BM25: rank texts so rare words count more and long texts do not win by length.

Retrieval used to score a text by how many of the question's words it held, all
of them weighing the same. On a CarePath board "patient" is in nearly every
ticket and "consent" in two, yet a relationship mentioning "patient" and
"export" outranked the one mentioning "consent" - and the question was about
consent. BM25 weighs each word by how rare it is across what is being ranked
(IDF) and dampens repeats and long texts, which is the standard answer to that.

Deterministic and local: no model, no index service, nothing stored. The same
question over the same texts always ranks the same way.
"""

import math
import re
from collections import Counter
from functools import lru_cache

#: Standard BM25 constants: K1 caps how much repeating a word helps, B how
#: much a long text is discounted.
K1 = 1.2
B = 0.75

#: Question words and English filler. Not ServiceOps' list, which also drops
#: words every incident uses ("service", "error") and would cost a knowledge
#: question its subject.
STOP_WORDS = frozenset(
    """
    about after again against all also and any are because been before being both but can
    cannot could did does doing done each for from had has have having her here
    hers his how into its itself just more most not now off once only other our
    out over own same she should some such than that the their them then there
    these they this those through too under until very was were what when where
    which while who whom why will with would you your yours
    """.split()
)


@lru_cache(maxsize=65536)
def stem(word):
    """A light, predictable stem: plural and tense endings only.

    "exports" and "export" were two terms, as were "refused" and "refuses".
    Deliberately crude rather than clever: a stemmer that sometimes merges the
    wrong words would be worse than one that merges too few.
    """
    if "_" in word:
        return word
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    for ending in ("ing", "ed", "es"):
        if len(word) > len(ending) + 3 and word.endswith(ending):
            return word[: -len(ending)]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def words(text):
    """The searchable words of a text, unstemmed: three letters and up, no filler.

    An identifier is its words: `settings.max_page_size` answers a question
    about page size. Read as one word it matched nothing, and a ticket whose
    acceptance criteria named it lost first place to a postmortem that wrote
    "page size" out.
    """
    return [
        part
        for word in re.findall(r"\w+", text.casefold())
        for part in word.split("_")
        if len(part) >= 3 and part not in STOP_WORDS
    ]


@lru_cache(maxsize=65536)
def term(word):
    """`stem`, then "-ment" folded: requirement and require are one term.

    Kept out of `stem` itself, which incident fingerprints depend on and must
    not change. Only long words: "comment" and "document" stay whole.
    """
    word = stem(word)
    if len(word) >= 9 and word.endswith("ment"):
        return word[:-4]
    return word


def terms(text):
    """The terms BM25 compares. Cached per word: a library repeats its words."""
    return [term(word) for word in words(text)]


def query_terms(question, limit=20):
    """A question's distinct terms, in order, capped so a pasted essay stays cheap."""
    return list(dict.fromkeys(terms(question)))[:limit]


class BM25:
    """A BM25 index over a fixed list of texts, each already split into terms."""

    def __init__(self, documents=(), counts=None):
        """From texts' terms, or from `counts` already made (Counter, length) pairs."""
        if counts is None:
            counts = [(Counter(document), len(document)) for document in documents]
        self.counts = [count for count, _ in counts]
        self.lengths = [length for _, length in counts]
        self.size = len(self.counts)
        self.average = (sum(self.lengths) / self.size) if self.size else 0
        self.frequency = Counter(term for counts in self.counts for term in counts)

    def idf(self, term):
        """Rarer is higher; never negative, so a common word still counts a little."""
        seen = self.frequency.get(term, 0)
        return math.log(1 + (self.size - seen + 0.5) / (seen + 0.5))

    def score(self, index, query):
        counts, length = self.counts[index], self.lengths[index]
        if not length:
            return 0.0
        norm = K1 * (1 - B + B * length / (self.average or 1))
        total = 0.0
        for term in query:
            hits = counts.get(term, 0)
            if hits:
                total += self.idf(term) * hits * (K1 + 1) / (hits + norm)
        return total

    def rank(self, query):
        """(score, index) for every text that matches, best first; ties keep order."""
        scored = [(self.score(index, query), index) for index in range(self.size)]
        return sorted(
            (item for item in scored if item[0] > 0), key=lambda item: (-item[0], item[1])
        )

    def rank_covering(self, query):
        """Like `rank`, but a text holding more of the question's terms comes first.

        BM25 alone lets one rare word carry a text past another that holds the
        whole question; measured on the CarePath benchmark, putting coverage
        first and BM25 second found more expected sources and ranked them
        higher than either signal alone.
        """
        return sorted(
            self.rank(query),
            key=lambda item: (
                -sum(1 for term in query if self.counts[item[1]].get(term)),
                -item[0],
                item[1],
            ),
        )


def spread(items, key, limit):
    """Up to `limit` items, the first of each source before any source's second.

    Eight citations from one document are one source an answer was shown eight
    times. Measured on the CarePath benchmark, taking each source's best item
    first raised the questions whose expected source was cited from 11 of 16
    to 15 of 16, without dropping any source that was already found.
    """
    firsts, rest, seen = [], [], set()
    for item in items:
        (rest if key(item) in seen else firsts).append(item)
        seen.add(key(item))
        if len(firsts) >= limit:
            break
    return (firsts + rest)[:limit]
