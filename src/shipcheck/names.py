"""Resolve a spoken package name to a real PyPI project.

Speech-to-text mangles package names ("pie torch", "sci kit learn", "num pie").
The resolver normalises what was heard, then fuzzy-matches it against a local
snapshot of the most-downloaded PyPI projects. It never invents a name: the
result is either a project from the list, a name the caller spelled out, or
"no match".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Literal

from rapidfuzz import fuzz, process

Status = Literal["exact", "confident", "ambiguous", "no_match"]

# Scores are rapidfuzz ratios (0-100) between separator-free keys.
CONFIDENT_SCORE = 90.0
AMBIGUOUS_SCORE = 72.0
# A confident match must also beat the runner-up by this much, unless it is exact.
CONFIDENT_MARGIN = 4.0

_VALID_NAME = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")

# Names people say that are not the PyPI project name.
ALIASES: dict[str, str] = {
    "pytorch": "torch",
    "sklearn": "scikit-learn",
    "scikit": "scikit-learn",
    "beautifulsoup": "beautifulsoup4",
    "bs4": "beautifulsoup4",
    "opencv": "opencv-python",
    "cv2": "opencv-python",
    "pil": "pillow",
    "yaml": "pyyaml",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "jose": "python-jose",
    "protobuffers": "protobuf",
    "psycopg": "psycopg2",
    "postgres": "psycopg2",
    "attr": "attrs",
    "mysqlclient": "mysqlclient",
    "tensor": "tensorflow",
    "huggingfacehub": "huggingface-hub",
    "transformer": "transformers",
    "langchaincore": "langchain-core",
}

# Whole-token rewrites applied to the transcript before matching.
_TOKEN_REWRITES: dict[str, str] = {
    "pie": "py",
    "pi": "py",
    "pye": "py",
    "sigh": "sci",
    "psy": "sci",
    "site": "sci",
    "dash": "-",
    "hyphen": "-",
    "minus": "-",
    "underscore": "-",
    "dot": ".",
    "zero": "0",
    "oh": "0",
    "one": "1",
    "two": "2",
    "to": "2",
    "too": "2",
    "three": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "jango": "django",
    "flasks": "flask",
    "panda": "pandas",
    "request": "requests",
    "kubernetes": "kubernetes",
    "sequel": "sql",
    "jason": "json",
    "jay": "j",
    "see": "c",
    "sea": "c",
    "are": "r",
    "you": "u",
    "why": "y",
    "bee": "b",
    "tea": "t",
    "tee": "t",
    "queue": "q",
    "ex": "x",
}

# Letter names for spelled-out input ("tee oh are see aitch").
_LETTER_NAMES: dict[str, str] = {
    "ay": "a", "bee": "b", "be": "b", "see": "c", "sea": "c", "cee": "c", "dee": "d",
    "ee": "e", "eff": "f", "gee": "g", "aitch": "h", "haitch": "h", "eye": "i",
    "jay": "j", "kay": "k", "el": "l", "ell": "l", "em": "m", "en": "n", "oh": "o",
    "pee": "p", "cue": "q", "queue": "q", "are": "r", "ar": "r", "ess": "s",
    "tee": "t", "tea": "t", "you": "u", "vee": "v", "double-u": "w", "doubleyou": "w",
    "ex": "x", "why": "y", "wye": "y", "zed": "z", "zee": "z",
    "alpha": "a", "bravo": "b", "charlie": "c", "delta": "d", "echo": "e",
    "foxtrot": "f", "golf": "g", "hotel": "h", "india": "i", "juliet": "j",
    "kilo": "k", "lima": "l", "mike": "m", "november": "n", "oscar": "o",
    "papa": "p", "quebec": "q", "romeo": "r", "sierra": "s", "tango": "t",
    "uniform": "u", "victor": "v", "whiskey": "w", "xray": "x", "x-ray": "x",
    "yankee": "y", "zulu": "z",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "dash": "-", "hyphen": "-", "minus": "-", "underscore": "_", "dot": ".",
    "period": ".", "point": ".",
}  # fmt: skip

_FILLER_WORDS = {"the", "a", "an", "package", "library", "module", "project", "python", "called"}


@dataclass(frozen=True)
class Candidate:
    name: str
    score: float

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "score": round(self.score, 1)}


@dataclass(frozen=True)
class Resolution:
    status: Status
    heard: str
    name: str | None = None
    score: float = 0.0
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.status in ("exact", "confident")


def normalize_name(name: str) -> str:
    """PEP 503 normalisation."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _key(name: str) -> str:
    """Separator-free key used for fuzzy comparison."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


@lru_cache(maxsize=1)
def load_top_projects() -> tuple[str, ...]:
    text = resources.files("shipcheck").joinpath("data/top_pypi.txt").read_text()
    return tuple(
        normalize_name(line) for line in text.splitlines() if line and not line.startswith("#")
    )


@lru_cache(maxsize=1)
def _index() -> tuple[dict[str, str], dict[str, int]]:
    """Map fuzzy key -> project name (most popular wins) and name -> rank."""
    by_key: dict[str, str] = {}
    rank: dict[str, int] = {}
    for i, name in enumerate(load_top_projects()):
        rank.setdefault(name, i)
        by_key.setdefault(_key(name), name)
    return by_key, rank


def _tokens(heard: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9.\-_]+", heard.lower()) if t]


def _variants(heard: str) -> list[str]:
    """Fuzzy keys to try for a transcript, most literal first."""
    raw = _tokens(heard)
    stripped = [t for t in raw if t not in _FILLER_WORDS] or raw
    rewritten = [_TOKEN_REWRITES.get(t, t) for t in stripped]
    variants: list[str] = []
    for toks in (raw, stripped, rewritten):
        key = _key("".join(toks))
        if key and key not in variants:
            variants.append(key)
    return variants


def resolve(heard: str, *, confirmed: frozenset[str] | set[str] = frozenset()) -> Resolution:
    """Resolve a transcript fragment to a PyPI project name.

    ``confirmed`` holds names the caller already spelled out or confirmed in this
    call; they resolve as exact even when absent from the local list.
    """
    heard = heard.strip()
    by_key, rank = _index()
    variants = _variants(heard)
    if not variants:
        return Resolution(status="no_match", heard=heard)

    direct = normalize_name(heard)
    if direct in confirmed:
        return Resolution(status="exact", heard=heard, name=direct, score=100.0)

    for key in variants:
        if key in by_key:
            return Resolution(status="exact", heard=heard, name=by_key[key], score=100.0)
    for key in variants:
        alias = ALIASES.get(key)
        if alias:
            return Resolution(status="confident", heard=heard, name=alias, score=99.0)

    best: dict[str, float] = {}
    keys = list(by_key)
    for key in variants:
        for match_key, score, _ in process.extract(key, keys, scorer=fuzz.ratio, limit=5):
            name = by_key[match_key]
            best[name] = max(best.get(name, 0.0), float(score))

    ranked = sorted(best.items(), key=lambda kv: (-kv[1], rank.get(kv[0], 10**9)))
    candidates = [Candidate(n, s) for n, s in ranked if s >= AMBIGUOUS_SCORE][:3]
    if not candidates:
        return Resolution(status="no_match", heard=heard)

    top = candidates[0]
    runner_up = candidates[1].score if len(candidates) > 1 else 0.0
    if top.score >= CONFIDENT_SCORE and top.score - runner_up >= CONFIDENT_MARGIN:
        return Resolution(
            status="confident", heard=heard, name=top.name, score=top.score, candidates=candidates
        )
    return Resolution(status="ambiguous", heard=heard, score=top.score, candidates=candidates)


def parse_spelling(spelled: str) -> str | None:
    """Turn spelled-out input ("t o r c h", "tee oh are see aitch", "T-O-R-C-H") into a name.

    Returns None when the result is not a syntactically valid project name.
    """
    text = spelled.strip().lower()
    text = re.sub(r"\bdouble[ -]?(?:u|you)\b", "w", text)
    parts = [p for p in re.split(r"[\s,]+", text) if p]
    # "t-o-r-c-h": hyphens between single letters are separators, not part of the name.
    if len(parts) == 1 and re.fullmatch(r"(?:[a-z0-9]-)+[a-z0-9]", parts[0]):
        parts = parts[0].split("-")
    out: list[str] = []
    for part in parts:
        if part in _LETTER_NAMES:
            out.append(_LETTER_NAMES[part])
        elif re.fullmatch(r"[a-z0-9._-]+", part):
            out.append(part)
        else:
            return None
    name = normalize_name("".join(out))
    return name if _VALID_NAME.match(name) else None
