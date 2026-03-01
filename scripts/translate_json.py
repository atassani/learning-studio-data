#!/usr/bin/env python3
# README
# Setup:
#   1) Install dependencies from pyproject.toml:
#      pip install -e ".[translate]"
#   2) Create a local env file in this folder (gitignored), for example:
#      echo 'DEPL_API_KEY="your-key:fx"' > .env.translate
#      (DEEPL_API_KEY is also supported)
#
# Example usage:
#   python translate_json.py --in ../data/questions-ipc.json --out ../data/questions-ipc-en.json --source es --target en
#   python translate_json.py --in ../data/questions-ipc.json --out ../data/questions-ipc-ca.json --source es --target ca
#   python translate_json.py --in ../data/questions-ipc.json --out ../data/questions-ipc-en.partial.json --source es --target en --max-items 50
#   python translate_json.py --in ../data/questions-ipc.json --out ../data/questions-ipc-en.json --source es --target en --env-file .env.translate

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

try:
    import requests  # type: ignore
except ModuleNotFoundError:
    requests = None  # type: ignore

JsonPath = Tuple[Any, ...]

DEFAULT_SKIP_FIELDS = ["id", "uuid", "slug", "language", "type"]
DEFAULT_MAX_CHARS_PER_BATCH = 20000
DEFAULT_MAX_TEXTS_PER_BATCH = 50
DEFAULT_CACHE_FLUSH_EVERY = 100
DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 1.2

SPANISH_STOPWORDS = {
    "que",
    "de",
    "la",
    "el",
    "los",
    "las",
    "un",
    "una",
    "en",
    "por",
    "con",
    "para",
    "es",
    "se",
    "del",
    "al",
    "¿",
    "¡",
}

# Placeholders we preserve exactly during translation.
_PLACEHOLDER_RE = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"  # ${var}
    r"|\{[^{}\n]+\}"  # {0}, {name}
    r"|%%|%[a-zA-Z]"  # %% and %s-like tokens
)


class TranslationError(RuntimeError):
    """Raised for translation-specific failures."""


@dataclasses.dataclass
class ProgressStats:
    total_strings: int = 0
    skipped_strings: int = 0
    selected_for_translation: int = 0
    translated_strings: int = 0
    cache_hits: int = 0
    api_calls: int = 0


@dataclasses.dataclass
class SpanishHeuristicReport:
    scanned_strings: int
    suspicious_strings: int
    stopword_hits: Dict[str, int]
    examples: List[Dict[str, Any]]


@dataclasses.dataclass
class TranslationConfig:
    in_path: Path
    out_path: Path
    source_lang: str
    target_lang: str
    skip_fields: set[str]
    max_chars_per_batch: int
    max_texts_per_batch: int
    cache_path: Path
    cache_flush_every: int
    timeout_seconds: int
    retries: int
    backoff_seconds: float
    max_items: int | None
    start_index: int
    end_index: int | None
    report_path: Path
    deepl_api_url: str


@dataclasses.dataclass
class StringEntry:
    path: JsonPath
    text: str


def _print(msg: str) -> None:
    print(msg, file=sys.stderr)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate JSON string values with DeepL while preserving structure."
    )
    parser.add_argument("--in", dest="in_path", required=True, help="Input JSON file path")
    parser.add_argument("--out", dest="out_path", required=True, help="Output JSON file path")
    parser.add_argument("--source", required=True, help="Source language, e.g. es")
    parser.add_argument("--target", required=True, choices=["en", "ca"], help="Target language")
    parser.add_argument(
        "--skip-fields",
        default=",".join(DEFAULT_SKIP_FIELDS),
        help="Comma-separated field names to skip translating values for",
    )
    parser.add_argument(
        "--max-chars-per-batch",
        type=int,
        default=DEFAULT_MAX_CHARS_PER_BATCH,
        help=f"Max total chars per API call (default: {DEFAULT_MAX_CHARS_PER_BATCH})",
    )
    parser.add_argument(
        "--max-texts-per-batch",
        type=int,
        default=DEFAULT_MAX_TEXTS_PER_BATCH,
        help=f"Max number of text entries per API call (default: {DEFAULT_MAX_TEXTS_PER_BATCH})",
    )
    parser.add_argument(
        "--cache-path",
        default=None,
        help="Optional cache path (default: <out_stem>.<target>.cache.json next to output)",
    )
    parser.add_argument(
        "--cache-flush-every",
        type=int,
        default=DEFAULT_CACHE_FLUSH_EVERY,
        help=f"Persist cache every N newly translated strings (default: {DEFAULT_CACHE_FLUSH_EVERY})",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="If questions array exists, translate only first N items from selected range",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Start index (inclusive) for questions array subset",
    )
    parser.add_argument(
        "--end-index",
        type=int,
        default=None,
        help="End index (exclusive) for questions array subset",
    )
    parser.add_argument(
        "--report-path",
        default=None,
        help="Optional Spanish heuristic report output path (default: <out>.spanish-report.json)",
    )
    parser.add_argument(
        "--deepl-api-url",
        default=None,
        help="Override DeepL endpoint, e.g. https://api-free.deepl.com/v2/translate",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional env file with KEY=VALUE lines (supports DEEPL_API_KEY and DEPL_API_KEY)",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout seconds (default: {DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help=f"Retries on network/429/5xx (default: {DEFAULT_RETRIES})",
    )
    parser.add_argument(
        "--backoff-seconds",
        type=float,
        default=DEFAULT_BACKOFF_SECONDS,
        help=f"Exponential backoff base (default: {DEFAULT_BACKOFF_SECONDS})",
    )
    return parser.parse_args(argv)


def resolve_api_key() -> str:
    key = (os.getenv("DEEPL_API_KEY") or "").strip()
    if not key:
        # Compatibility with user's requested env var alias.
        key = (os.getenv("DEPL_API_KEY") or "").strip()
    if key:
        return key

    raise TranslationError(
        "Missing DeepL API key. Set DEEPL_API_KEY (preferred) or DEPL_API_KEY (compatibility), "
        "or create scripts/.env.translate with one of those keys, "
        "for example:\n"
        "  export DEEPL_API_KEY=\"your-key\"\n"
        "Then re-run the command."
    )


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise TranslationError(f"--env-file not found: {path}")
    if not path.is_file():
        raise TranslationError(f"--env-file is not a file: {path}")

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TranslationError(f"Unable to read env file {path}: {exc}") from exc

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def maybe_load_default_env_file(script_path: Path) -> Path | None:
    candidates = [
        script_path.parent / ".env.translate",
        script_path.parent / ".env",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            load_env_file(path)
            return path
    return None


def resolve_deepl_url(api_key: str, override_url: str | None) -> str:
    if override_url:
        return override_url
    if api_key.endswith(":fx"):
        return "https://api-free.deepl.com/v2/translate"
    return "https://api.deepl.com/v2/translate"


def default_cache_path(out_path: Path, target_lang: str) -> Path:
    base = out_path.with_suffix("")
    return base.with_name(f"{base.name}.{target_lang}.cache.json")


def default_report_path(out_path: Path) -> Path:
    return out_path.with_name(f"{out_path.name}.spanish-report.json")


def build_config(args: argparse.Namespace, api_key: str) -> TranslationConfig:
    in_path = Path(args.in_path).expanduser().resolve()
    out_path = Path(args.out_path).expanduser().resolve()

    if args.max_chars_per_batch <= 0:
        raise TranslationError("--max-chars-per-batch must be > 0")
    if args.max_texts_per_batch <= 0:
        raise TranslationError("--max-texts-per-batch must be > 0")
    if args.start_index < 0:
        raise TranslationError("--start-index must be >= 0")
    if args.end_index is not None and args.end_index < args.start_index:
        raise TranslationError("--end-index must be >= --start-index")
    if args.max_items is not None and args.max_items < 0:
        raise TranslationError("--max-items must be >= 0")

    skip_fields = {part.strip() for part in args.skip_fields.split(",") if part.strip()}
    cache_path = (
        Path(args.cache_path).expanduser().resolve()
        if args.cache_path
        else default_cache_path(out_path, args.target)
    )
    report_path = (
        Path(args.report_path).expanduser().resolve()
        if args.report_path
        else default_report_path(out_path)
    )

    return TranslationConfig(
        in_path=in_path,
        out_path=out_path,
        source_lang=args.source.upper(),
        target_lang=args.target.upper(),
        skip_fields=skip_fields,
        max_chars_per_batch=args.max_chars_per_batch,
        max_texts_per_batch=args.max_texts_per_batch,
        cache_path=cache_path,
        cache_flush_every=args.cache_flush_every,
        timeout_seconds=args.timeout_seconds,
        retries=args.retries,
        backoff_seconds=args.backoff_seconds,
        max_items=args.max_items,
        start_index=args.start_index,
        end_index=args.end_index,
        report_path=report_path,
        deepl_api_url=resolve_deepl_url(api_key, args.deepl_api_url),
    )


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as exc:
        raise TranslationError(f"Input file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TranslationError(f"Invalid input JSON at {path}: {exc}") from exc


def load_cache(cache_path: Path) -> Dict[str, Dict[str, str]]:
    if not cache_path.exists():
        return {}
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"Cache file is not valid JSON: {cache_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TranslationError(f"Cache file must contain a JSON object: {cache_path}")
    return data


def save_cache(cache: Mapping[str, Dict[str, str]], cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    tmp_path.replace(cache_path)


def make_cache_key(source_lang: str, target_lang: str, text: str) -> str:
    raw = f"{source_lang}\u0001{target_lang}\u0001{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def cache_get(
    cache: Mapping[str, Dict[str, str]], source_lang: str, target_lang: str, text: str
) -> str | None:
    k = make_cache_key(source_lang, target_lang, text)
    row = cache.get(k)
    if not isinstance(row, dict):
        return None
    if (
        row.get("source") == source_lang
        and row.get("target") == target_lang
        and row.get("text") == text
        and isinstance(row.get("translation"), str)
    ):
        return row["translation"]
    return None


def cache_set(
    cache: Dict[str, Dict[str, str]],
    source_lang: str,
    target_lang: str,
    text: str,
    translation: str,
) -> None:
    k = make_cache_key(source_lang, target_lang, text)
    cache[k] = {
        "source": source_lang,
        "target": target_lang,
        "text": text,
        "translation": translation,
    }


def find_main_questions_path(obj: Any) -> JsonPath | None:
    queue: List[Tuple[JsonPath, Any]] = [((), obj)]
    while queue:
        path, current = queue.pop(0)
        if isinstance(current, dict):
            for key, value in current.items():
                child_path = path + (key,)
                if key == "questions" and isinstance(value, list):
                    return child_path
                queue.append((child_path, value))
        elif isinstance(current, list):
            for idx, item in enumerate(current):
                queue.append((path + (idx,), item))
    return None


def build_selected_question_indexes(total: int, cfg: TranslationConfig) -> set[int] | None:
    if total <= 0:
        return set()

    start = min(cfg.start_index, total)
    end = total if cfg.end_index is None else min(cfg.end_index, total)
    if end < start:
        end = start

    indexes = list(range(start, end))
    if cfg.max_items is not None:
        indexes = indexes[: cfg.max_items]
    return set(indexes)


def _path_is_under_question_item(
    path: JsonPath, questions_path: JsonPath, selected_indexes: set[int]
) -> bool:
    if len(path) <= len(questions_path):
        return False
    if path[: len(questions_path)] != questions_path:
        return False
    idx = path[len(questions_path)]
    return isinstance(idx, int) and idx in selected_indexes


def _is_text_translatable(text: str) -> bool:
    return text.strip() != ""


def collect_strings(
    json_obj: Any,
    skip_fields: set[str],
    questions_path: JsonPath | None = None,
    selected_question_indexes: set[int] | None = None,
) -> Tuple[List[StringEntry], ProgressStats]:
    entries: List[StringEntry] = []
    stats = ProgressStats()

    def walk(node: Any, path: JsonPath) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                child_path = path + (k,)
                if isinstance(v, str):
                    stats.total_strings += 1
                    if k in skip_fields or not _is_text_translatable(v):
                        stats.skipped_strings += 1
                        continue

                    if questions_path is not None and selected_question_indexes is not None:
                        if not _path_is_under_question_item(
                            child_path, questions_path, selected_question_indexes
                        ):
                            stats.skipped_strings += 1
                            continue

                    entries.append(StringEntry(path=child_path, text=v))
                    stats.selected_for_translation += 1
                else:
                    walk(v, child_path)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                child_path = path + (idx,)
                if isinstance(item, str):
                    stats.total_strings += 1
                    if not _is_text_translatable(item):
                        stats.skipped_strings += 1
                        continue

                    if questions_path is not None and selected_question_indexes is not None:
                        if not _path_is_under_question_item(
                            child_path, questions_path, selected_question_indexes
                        ):
                            stats.skipped_strings += 1
                            continue

                    entries.append(StringEntry(path=child_path, text=item))
                    stats.selected_for_translation += 1
                else:
                    walk(item, child_path)

    walk(json_obj, ())
    return entries, stats


def apply_translations(json_obj: Any, translations_by_path: Mapping[JsonPath, str]) -> Any:
    out = copy.deepcopy(json_obj)
    for path, translated in translations_by_path.items():
        ref = out
        for segment in path[:-1]:
            ref = ref[segment]
        ref[path[-1]] = translated
    return out


def _mask_placeholders(text: str) -> Tuple[str, Dict[str, str]]:
    mapping: Dict[str, str] = {}

    def repl(match: re.Match[str]) -> str:
        token = f"__DEEPL_PH_{len(mapping)}__"
        mapping[token] = match.group(0)
        return token

    masked = _PLACEHOLDER_RE.sub(repl, text)
    return masked, mapping


def _unmask_placeholders(text: str, mapping: Mapping[str, str]) -> str:
    out = text
    for token, original in mapping.items():
        out = out.replace(token, original)
    return out


class DeepLClient:
    def __init__(
        self,
        api_key: str,
        api_url: str,
        timeout_seconds: int,
        retries: int,
        backoff_seconds: float,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.session = requests.Session() if requests is not None else None

    def translate_batch(
        self,
        texts: Sequence[str],
        source_lang: str,
        target_lang: str,
    ) -> List[str]:
        if not texts:
            return []

        payload = {
            "text": list(texts),
            "source_lang": source_lang,
            "target_lang": target_lang,
            "preserve_formatting": "1",
            "split_sentences": "nonewlines",
        }
        headers = {
            "Authorization": f"DeepL-Auth-Key {self.api_key}",
        }

        for attempt in range(self.retries + 1):
            try:
                if self.session is not None:
                    status_code, body_text = self._post_with_requests(payload, headers)
                else:
                    status_code, body_text = self._post_with_urllib(payload, headers)
            except (Exception,) as exc:
                if attempt >= self.retries:
                    raise TranslationError(
                        f"DeepL network error after {self.retries + 1} attempts: {exc}"
                    ) from exc
                self._sleep_before_retry(attempt)
                continue

            if status_code in {429, 500, 502, 503, 504}:
                if attempt >= self.retries:
                    raise TranslationError(
                        f"DeepL request failed with status {status_code}: {body_text[:500]}"
                    )
                self._sleep_before_retry(attempt)
                continue

            if status_code >= 400:
                raise TranslationError(
                    f"DeepL request failed with status {status_code}: {body_text[:500]}"
                )

            try:
                body = json.loads(body_text)
            except json.JSONDecodeError as exc:
                raise TranslationError(
                    f"DeepL returned non-JSON response: {body_text[:500]}"
                ) from exc

            translations = body.get("translations")
            if not isinstance(translations, list) or len(translations) != len(texts):
                raise TranslationError(
                    "DeepL response does not match request size. "
                    f"Expected {len(texts)} translations, got {len(translations) if isinstance(translations, list) else 'invalid'}."
                )

            out: List[str] = []
            for item in translations:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    raise TranslationError(f"Unexpected DeepL response item: {item!r}")
                out.append(item["text"])
            return out

        raise AssertionError("unreachable")

    def _post_with_requests(
        self, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> Tuple[int, str]:
        assert self.session is not None
        resp = self.session.post(
            self.api_url,
            data=payload,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        return resp.status_code, resp.text

    def _post_with_urllib(
        self, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> Tuple[int, str]:
        encoded = urlencode(payload, doseq=True).encode("utf-8")
        req = Request(self.api_url, data=encoded, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        try:
            with urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                return int(getattr(resp, "status", 200)), body
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, body
        except URLError as exc:
            raise TranslationError(f"DeepL URL error: {exc}") from exc

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self.backoff_seconds * (2**attempt)
        time.sleep(delay)


def build_batches(
    entries: Sequence[StringEntry],
    max_chars_per_batch: int,
    max_texts_per_batch: int,
) -> List[List[StringEntry]]:
    batches: List[List[StringEntry]] = []
    current: List[StringEntry] = []
    current_chars = 0

    for entry in entries:
        text_len = len(entry.text)

        if text_len > max_chars_per_batch:
            if current:
                batches.append(current)
                current = []
                current_chars = 0
            batches.append([entry])
            continue

        would_overflow_chars = current_chars + text_len > max_chars_per_batch
        would_overflow_size = len(current) >= max_texts_per_batch
        if current and (would_overflow_chars or would_overflow_size):
            batches.append(current)
            current = []
            current_chars = 0

        current.append(entry)
        current_chars += text_len

    if current:
        batches.append(current)

    return batches


def collect_named_array_lengths(obj: Any, keys: set[str], path: JsonPath = ()) -> Dict[str, int]:
    result: Dict[str, int] = {}

    if isinstance(obj, dict):
        for key, value in obj.items():
            child_path = path + (key,)
            if key in keys and isinstance(value, list):
                result[path_to_string(child_path)] = len(value)
            result.update(collect_named_array_lengths(value, keys, child_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            result.update(collect_named_array_lengths(item, keys, path + (i,)))

    return result


def path_to_string(path: JsonPath) -> str:
    return "/".join(str(p) for p in path)


def detect_spanish_leftovers(
    obj: Any,
    max_examples: int = 50,
) -> SpanishHeuristicReport:
    entries, _ = collect_strings(obj, skip_fields=set())

    stopword_hits = {w: 0 for w in SPANISH_STOPWORDS}
    suspicious_examples: List[Dict[str, Any]] = []
    suspicious_count = 0

    for entry in entries:
        low = entry.text.lower()
        hit_count = 0

        for word in SPANISH_STOPWORDS:
            if word in {"¿", "¡"}:
                count = low.count(word)
            else:
                count = len(re.findall(rf"\b{re.escape(word)}\b", low))
            if count:
                stopword_hits[word] += count
                hit_count += count

        if hit_count > 0:
            suspicious_count += 1
            if len(suspicious_examples) < max_examples:
                suspicious_examples.append(
                    {
                        "path": path_to_string(entry.path),
                        "hits": hit_count,
                        "preview": entry.text[:200],
                    }
                )

    return SpanishHeuristicReport(
        scanned_strings=len(entries),
        suspicious_strings=suspicious_count,
        stopword_hits=stopword_hits,
        examples=suspicious_examples,
    )


def translate_entries(
    entries: Sequence[StringEntry],
    cache: Dict[str, Dict[str, str]],
    client: DeepLClient,
    cfg: TranslationConfig,
    stats: ProgressStats,
) -> Dict[JsonPath, str]:
    translations_by_path: Dict[JsonPath, str] = {}
    pending: List[StringEntry] = []

    for entry in entries:
        cached = cache_get(cache, cfg.source_lang, cfg.target_lang, entry.text)
        if cached is not None:
            translations_by_path[entry.path] = cached
            stats.cache_hits += 1
        else:
            pending.append(entry)

    if not pending:
        return translations_by_path

    new_since_flush = 0
    batches = build_batches(
        pending,
        max_chars_per_batch=cfg.max_chars_per_batch,
        max_texts_per_batch=cfg.max_texts_per_batch,
    )

    for batch_index, batch in enumerate(batches, start=1):
        masked_texts: List[str] = []
        mappings: List[Dict[str, str]] = []
        for entry in batch:
            masked, mapping = _mask_placeholders(entry.text)
            masked_texts.append(masked)
            mappings.append(mapping)

        translated_masked = client.translate_batch(
            masked_texts,
            source_lang=cfg.source_lang,
            target_lang=cfg.target_lang,
        )
        stats.api_calls += 1

        for entry, translated_text, mapping in zip(batch, translated_masked, mappings):
            restored = _unmask_placeholders(translated_text, mapping)
            translations_by_path[entry.path] = restored
            cache_set(cache, cfg.source_lang, cfg.target_lang, entry.text, restored)
            stats.translated_strings += 1
            new_since_flush += 1

        _print(
            f"Progress: batch {batch_index}/{len(batches)} | "
            f"translated={stats.translated_strings} cache_hits={stats.cache_hits} api_calls={stats.api_calls}"
        )

        if cfg.cache_flush_every > 0 and new_since_flush >= cfg.cache_flush_every:
            save_cache(cache, cfg.cache_path)
            new_since_flush = 0

    return translations_by_path


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def validate_output_json(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8") as f:
            json.load(f)
    except json.JSONDecodeError as exc:
        raise TranslationError(f"Output JSON is invalid: {path}: {exc}") from exc


def validate_structure_invariants(input_obj: Any, output_obj: Any) -> Dict[str, Any]:
    keys = {"questions", "options", "answers"}
    in_counts = collect_named_array_lengths(input_obj, keys)
    out_counts = collect_named_array_lengths(output_obj, keys)

    mismatches: List[Dict[str, Any]] = []
    all_paths = sorted(set(in_counts.keys()) | set(out_counts.keys()))
    for p in all_paths:
        if in_counts.get(p) != out_counts.get(p):
            mismatches.append(
                {
                    "path": p,
                    "input": in_counts.get(p),
                    "output": out_counts.get(p),
                }
            )

    return {
        "checked_keys": sorted(keys),
        "input_counts": in_counts,
        "output_counts": out_counts,
        "mismatches": mismatches,
    }


def write_report(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def run(argv: Sequence[str]) -> int:
    args = parse_args(argv)

    try:
        loaded_env_file: Path | None = None
        if args.env_file:
            loaded_env_file = Path(args.env_file).expanduser().resolve()
            load_env_file(loaded_env_file)
        else:
            loaded_env_file = maybe_load_default_env_file(Path(__file__).resolve())
        api_key = resolve_api_key()
        cfg = build_config(args, api_key)
    except TranslationError as exc:
        _print(f"Error: {exc}")
        return 2

    _print(f"Input: {cfg.in_path}")
    _print(f"Output: {cfg.out_path}")
    _print(f"Cache: {cfg.cache_path}")
    _print(f"DeepL URL: {cfg.deepl_api_url}")
    if loaded_env_file:
        _print(f"Env file: {loaded_env_file}")

    try:
        input_obj = load_json(cfg.in_path)
        cache = load_cache(cfg.cache_path)
    except TranslationError as exc:
        _print(f"Error: {exc}")
        return 2

    questions_path = find_main_questions_path(input_obj)
    selected_question_indexes: set[int] | None = None
    if questions_path is not None and (
        cfg.max_items is not None or cfg.start_index != 0 or cfg.end_index is not None
    ):
        questions = input_obj
        for segment in questions_path:
            questions = questions[segment]
        selected_question_indexes = build_selected_question_indexes(len(questions), cfg)
        _print(
            "Subset mode active on questions array: "
            f"path={path_to_string(questions_path)} selected_items={len(selected_question_indexes)}"
        )
    elif questions_path is None and (
        cfg.max_items is not None or cfg.start_index != 0 or cfg.end_index is not None
    ):
        _print("Questions array not found; subset options ignored and full document will be translated.")

    entries, stats = collect_strings(
        input_obj,
        skip_fields=cfg.skip_fields,
        questions_path=questions_path if selected_question_indexes is not None else None,
        selected_question_indexes=selected_question_indexes,
    )

    _print(
        "Collection summary: "
        f"total_strings={stats.total_strings} selected={stats.selected_for_translation} skipped={stats.skipped_strings}"
    )

    client = DeepLClient(
        api_key=api_key,
        api_url=cfg.deepl_api_url,
        timeout_seconds=cfg.timeout_seconds,
        retries=cfg.retries,
        backoff_seconds=cfg.backoff_seconds,
    )

    try:
        translations_by_path = translate_entries(entries, cache, client, cfg, stats)
        translated_obj = apply_translations(input_obj, translations_by_path)

        write_json(cfg.out_path, translated_obj)
        validate_output_json(cfg.out_path)

        invariants = validate_structure_invariants(input_obj, translated_obj)
        if invariants["mismatches"]:
            mismatch_count = len(invariants["mismatches"])
            raise TranslationError(
                f"Structure invariant mismatch detected in {mismatch_count} array(s). "
                f"First mismatch: {invariants['mismatches'][0]}"
            )

        spanish_report = detect_spanish_leftovers(translated_obj)
        report_payload = {
            "summary": {
                "input": str(cfg.in_path),
                "output": str(cfg.out_path),
                "source_lang": cfg.source_lang,
                "target_lang": cfg.target_lang,
                "total_strings": stats.total_strings,
                "selected_for_translation": stats.selected_for_translation,
                "translated_strings": stats.translated_strings,
                "cache_hits": stats.cache_hits,
                "api_calls": stats.api_calls,
                "suspicious_spanish_strings": spanish_report.suspicious_strings,
            },
            "structure_invariants": invariants,
            "spanish_heuristic": dataclasses.asdict(spanish_report),
        }
        write_report(cfg.report_path, report_payload)
        save_cache(cache, cfg.cache_path)

    except TranslationError as exc:
        _print(f"Error: {exc}")
        try:
            save_cache(cache, cfg.cache_path)
        except Exception:
            pass
        return 1

    _print(
        "Done: "
        f"translated={stats.translated_strings} cache_hits={stats.cache_hits} "
        f"api_calls={stats.api_calls} report={cfg.report_path}"
    )
    return 0


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
