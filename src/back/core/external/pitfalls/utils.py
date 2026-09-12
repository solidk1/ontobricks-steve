"""Utility functions for ontology pitfall detection.

Vendored from https://github.com/D2KLab/Ontology-Pitfalls-Detector (Apache-2.0).
"""
from __future__ import annotations

from functools import reduce
from operator import concat
from typing import Callable, Iterable, List, Sequence, TypeVar

T = TypeVar("T")


def camel_case_split(text: str) -> List[str]:
    if not text:
        return []

    words = [[text[0]]]
    for char in text[1:]:
        if words[-1][-1].islower() and char.isupper():
            words.append([char])
        else:
            words[-1].append(char)

    return ["".join(word) for word in words]


def flatten(values: Iterable[Iterable[T]]) -> List[T]:
    values_list = [list(v) for v in values]
    if not values_list:
        return []
    return reduce(concat, values_list)


def extract_label(uri: object, clean: bool = False) -> str:
    label = str(uri).split("#")[-1]
    if clean:
        return " ".join(camel_case_split(label))
    return label


def normalize_name(name: str) -> str:
    return "".join(ch.lower() for ch in str(name) if ch.isalnum())


def _assert_safe_nltk_resource(resource_path: str) -> None:
    """Reject the attack shapes behind GHSA-p4gq-832x-fm9v (CVE-2026-54293).

    ``nltk.data.find()`` in nltk<=3.9.4 runs its unsafe-path regex against the
    still-encoded string and only decodes ``%xx`` afterwards, so percent-encoded
    separators/traversal (``%2f``, ``%2e%2e``) bypass the check and read
    arbitrary files. All our callers pass hardcoded resource names, so this
    guard is defense-in-depth that makes the flaw unreachable until we can pin
    nltk>=3.10.0.
    """
    if "%" in resource_path or ".." in resource_path or resource_path.startswith(("/", "\\")):
        raise ValueError(f"Unsafe NLTK resource path rejected: {resource_path!r}")


def ensure_nltk_resource(resource_path: str, download_name: str) -> None:
    """Make *resource_path* available, or raise saying why it is not.

    The corpora are **data**, downloaded from ``raw.githubusercontent.com`` on
    first use — ``pip install nltk`` does not bring them. Any environment with
    restricted egress (a hardened container, CI, a sandbox) therefore cannot
    fetch them at runtime.

    This used to call ``nltk.download(..., quiet=True)`` and ignore the returned
    bool, so a failed download looked like success and the caller blew up one
    line later with a twenty-line ``LookupError`` traceback about search paths —
    a function whose entire purpose is to *ensure* a resource, not verifying
    that it had. It now re-checks and raises something that names the corpus and
    how to pre-seed it.
    """
    from back.core.errors import InfrastructureError

    try:
        import nltk
    except ImportError as exc:  # the WordNet checks only
        raise InfrastructureError(
            "The WordNet-based pitfall checks need the 'pitfalls' extra",
            detail="uv sync --extra pitfalls",
        ) from exc

    _assert_safe_nltk_resource(resource_path)
    try:
        nltk.data.find(resource_path)
        return
    except LookupError:
        pass

    try:
        nltk.download(download_name, quiet=True)
    except Exception as exc:  # noqa: BLE001 — vendor/network surface
        raise InfrastructureError(
            f"NLTK corpus {download_name!r} is not installed and could not be "
            "downloaded",
            detail=(
                f"{exc}. Pre-seed it in the image or environment with: "
                f"python -m nltk.downloader {download_name}"
            ),
        ) from exc

    # Verify rather than assume: nltk.download returns False on failure.
    try:
        nltk.data.find(resource_path)
    except LookupError as exc:
        raise InfrastructureError(
            f"NLTK corpus {download_name!r} is unavailable",
            detail=(
                "The download reported no usable data, which normally means "
                "egress to raw.githubusercontent.com is blocked. Pre-seed it "
                f"with: python -m nltk.downloader {download_name}"
            ),
        ) from exc


def normalize_pattern_id(raw_id: str) -> str:
    token = str(raw_id).strip().upper().rstrip(".")
    if not token:
        raise ValueError("Pattern identifier cannot be empty.")

    if token == "ALL":
        return token

    if token.startswith("P"):
        token = token[1:]

    if not token.isdigit():
        raise ValueError(f"Invalid pattern identifier: {raw_id}")

    return f"P{int(token)}"


def _pattern_sort_key(pattern_id: str) -> tuple:
    token = str(pattern_id).strip().upper().rstrip(".")
    if token.startswith("P"):
        token = token[1:]

    parts = token.split(".")
    if any(not part.isdigit() for part in parts):
        raise ValueError(f"Invalid pattern identifier: {pattern_id}")

    return tuple(int(part) for part in parts)


def sort_pattern_ids(pattern_ids: Sequence[str]) -> List[str]:
    return sorted(pattern_ids, key=_pattern_sort_key)


def parse_pattern_selection(
    patterns: Sequence[str] | None,
    available_patterns: Sequence[str],
    normalizer: Callable[[str], str] = normalize_pattern_id,
) -> List[str]:
    available_normalized = [normalizer(pattern) for pattern in available_patterns]
    available_set = set(available_normalized)

    if not patterns:
        return sort_pattern_ids(available_normalized)

    raw_tokens: List[str] = []
    for pattern in patterns:
        raw_tokens.extend(token.strip() for token in str(pattern).split(",") if token.strip())

    if not raw_tokens:
        return sort_pattern_ids(available_normalized)

    normalized = [normalizer(token) for token in raw_tokens]
    if "ALL" in normalized:
        return sort_pattern_ids(available_normalized)

    selected: List[str] = []
    for pattern_id in normalized:
        if pattern_id not in available_set:
            options = ", ".join(sort_pattern_ids(available_normalized))
            raise ValueError(f"Unknown pattern '{pattern_id}'. Available: {options}")

        if pattern_id not in selected:
            selected.append(pattern_id)

    return selected
