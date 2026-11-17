"""Name resolution: speech-to-text manglings, ambiguity, no match, spelling."""

from __future__ import annotations

import pytest

from shipcheck.names import load_top_projects, parse_spelling, resolve


def test_local_list_is_a_few_thousand_real_projects() -> None:
    projects = load_top_projects()
    assert 3000 <= len(projects) <= 10000
    assert {"requests", "numpy", "torch", "scikit-learn"} <= set(projects)


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("pie torch", "torch"),
        ("pytorch", "torch"),
        ("sci kit learn", "scikit-learn"),
        ("scikit learn", "scikit-learn"),
        ("num pie", "numpy"),
        ("requests", "requests"),
        ("Requests", "requests"),
        ("jango", "django"),
        ("beautiful soup four", "beautifulsoup4"),
        ("beautiful soup", "beautifulsoup4"),
        ("open cv", "opencv-python"),
        ("fast api", "fastapi"),
        ("fast a p i", "fastapi"),
        ("pie yaml", "pyyaml"),
        ("boto three", "boto3"),
        ("pie dantic", "pydantic"),
        ("sequel alchemy", "sqlalchemy"),
        ("the flask package", "flask"),
        ("tensor flow", "tensorflow"),
        ("url lib 3", "urllib3"),
        ("http x", "httpx"),
        ("pie test", "pytest"),
        ("hugging face hub", "huggingface-hub"),
    ],
)
def test_mangled_names_resolve(heard: str, expected: str) -> None:
    res = resolve(heard)
    assert res.resolved, res
    assert res.name == expected


@pytest.mark.parametrize("heard", ["polers", "you're a lib three"])
def test_near_misses_are_ambiguous_not_guessed(heard: str) -> None:
    res = resolve(heard)
    assert res.status == "ambiguous"
    assert res.name is None
    assert res.candidates


@pytest.mark.parametrize("heard", ["blorptastic", "zzqx", "salary", "", "   "])
def test_unknown_names_do_not_match(heard: str) -> None:
    res = resolve(heard)
    assert res.status == "no_match"
    assert res.name is None


def test_every_resolved_name_is_a_real_project_or_confirmed() -> None:
    projects = set(load_top_projects())
    for heard in ["pie torch", "sci kit learn", "polers", "blorptastic", "open cv"]:
        res = resolve(heard)
        for name in [res.name, *[c.name for c in res.candidates]]:
            assert name is None or name in projects


def test_confirmed_name_outside_the_list_resolves() -> None:
    assert resolve("blorptastic").status == "no_match"
    res = resolve("blorptastic", confirmed={"blorptastic"})
    assert res.status == "exact" and res.name == "blorptastic"


@pytest.mark.parametrize(
    ("spelled", "expected"),
    [
        ("t o r c h", "torch"),
        ("T-O-R-C-H", "torch"),
        ("T, O, R, C, H", "torch"),
        ("tee oh are see aitch", "torch"),
        ("f a s t a p i", "fastapi"),
        ("p y dash y a m l", "py-yaml"),
        ("b o t o three", "boto3"),
        ("scikit-learn", "scikit-learn"),
    ],
)
def test_spelling(spelled: str, expected: str) -> None:
    assert parse_spelling(spelled) == expected


@pytest.mark.parametrize("spelled", ["hello world!", "", "- - -"])
def test_bad_spelling_is_rejected(spelled: str) -> None:
    assert parse_spelling(spelled) is None
