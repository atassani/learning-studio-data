from __future__ import annotations

import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PDF_PATH = Path(
    "/Users/toni.tassani/code/humblyproud-multiproject/learning-studio-data/PREGUNTAS TEST DF LÓGICA II AMPLIADO.pdf"
)
OUTPUT_PATH = Path(
    "/Users/toni.tassani/code/humblyproud-multiproject/learning-studio-data/data/questions-log2.json"
)
SECTION_NAME = "Preguntas Test Lógica II"

QUESTION_START_RE = re.compile(r"^\s*(\d+)-\s*(.*)$")
ANSWER_LINE_RE = re.compile(r"^(Verdadero|Falso)\.\s*(.*)$")


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            "pdftotext is required but was not found in PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr.strip() or "pdftotext failed.") from exc

    return result.stdout.replace("\f", "\n")


def normalize_logic_notation(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = text.replace("Λ", "∀")
    text = text.replace("Vx", "∃x")
    text = re.sub(r"\bV([a-z])\b", r"∃\1", text)
    text = text.replace("<->", "↔")
    text = text.replace("->", "→")
    text = re.sub(r"(?<=[A-Za-z0-9)\]])\s+\^\s+(?=[A-Za-z0-9(¬∀∃])", " ∧ ", text)
    text = re.sub(r"(?<=[A-Za-z0-9)\]])\s+v\s+(?=[A-Za-z0-9(¬∀∃])", " ∨ ", text)
    text = re.sub(r"\s*→\s*", " → ", text)
    text = re.sub(r"\s*↔\s*", " ↔ ", text)
    text = re.sub(r"\s*∧\s*", " ∧ ", text)
    text = re.sub(r"\s*∨\s*", " ∨ ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_block_lines(lines: list[str]) -> list[str]:
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "∧ ∨ ↔ →¬αβ":
            continue
        if not stripped:
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        cleaned.append(stripped)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return cleaned


def split_blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in text.splitlines():
        if QUESTION_START_RE.match(line):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)

    if current:
        blocks.append(current)

    return blocks


def is_formula_line(line: str) -> bool:
    normalized = normalize_logic_notation(line)
    return any(symbol in normalized for symbol in ("∀", "∃", "→", "∧", "∨", "↔", "¬"))


def split_paragraphs(lines: list[str]) -> list[list[str]]:
    paragraphs: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if not line:
            if current:
                paragraphs.append(current)
                current = []
            continue
        current.append(line)

    if current:
        paragraphs.append(current)

    return paragraphs


def extract_numbered_items(lines: list[str]) -> list[str]:
    text = " ".join(lines).strip()
    matches = list(re.finditer(r"(?<!\S)(\d+)\)\s*", text))
    if len(matches) < 2 or matches[0].start() != 0:
        return []

    items: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        item = normalize_logic_notation(text[start:end])
        if item:
            items.append(item)
    return items


def collapse_paragraph_lines(lines: list[str]) -> list[str]:
    collapsed: list[str] = []

    for raw_line in lines:
        line = normalize_logic_notation(raw_line)
        if not collapsed:
            collapsed.append(line)
            continue

        previous = collapsed[-1]
        if previous.endswith(":") or is_formula_line(previous) or is_formula_line(line):
            collapsed.append(line)
        else:
            collapsed[-1] = f"{previous} {line}"

    return collapsed


def render_explanation_html(lines: list[str]) -> str:
    paragraphs = split_paragraphs(lines)
    blocks: list[tuple[str, str]] = []

    for paragraph in paragraphs:
        items = extract_numbered_items(paragraph)
        if items:
            blocks.append((
                "list",
                "<ol>"
                + "".join(f"<li>{html.escape(item)}</li>" for item in items)
                + "</ol>"
            ))
            continue

        collapsed_lines = collapse_paragraph_lines(paragraph)
        body = "<br>".join(html.escape(line) for line in collapsed_lines)
        blocks.append(("text", body))

    if not blocks:
        return ""

    if len(blocks) == 1:
        return blocks[0][1]

    rendered: list[str] = []
    for kind, body in blocks:
        if kind == "list":
            rendered.append(body)
        else:
            rendered.append(f"<p>{body}</p>")
    return "".join(rendered)


def parse_block(lines: list[str]) -> dict[str, object]:
    cleaned = clean_block_lines(lines)
    if not cleaned:
        raise ValueError("Encountered an empty question block.")

    match = QUESTION_START_RE.match(cleaned[0])
    if not match:
        raise ValueError(f"Invalid question header: {cleaned[0]!r}")

    number = int(match.group(1))
    question_lines = [match.group(2).strip()]
    answer_entries: list[tuple[str, str]] = []
    post_answer_lines: list[str] = []

    state = "question"
    for line in cleaned[1:]:
        answer_match = ANSWER_LINE_RE.match(line)
        if answer_match:
            answer_entries.append((answer_match.group(1), answer_match.group(2).strip()))
            post_answer_lines.append(line)
            state = "explanation"
            continue

        if state == "question":
            question_lines.append(line)
        else:
            post_answer_lines.append(line)

    question = normalize_logic_notation(" ".join(question_lines))

    anomaly = False
    if len(answer_entries) == 1:
        answer = answer_entries[0][0]
        first_answer_line = post_answer_lines[0]
        answer_match = ANSWER_LINE_RE.match(first_answer_line)
        explanation_parts: list[str] = []
        if answer_match and answer_match.group(2).strip():
            explanation_parts.append(answer_match.group(2).strip())
        explanation_parts.extend(post_answer_lines[1:])
        explanation = render_explanation_html(explanation_parts)
    elif len(answer_entries) > 1:
        anomaly = True
        answer = " / ".join(entry[0] for entry in answer_entries)
        explanation = render_explanation_html(post_answer_lines)
    else:
        anomaly = True
        answer = ""
        explanation = render_explanation_html(post_answer_lines)

    if anomaly:
        question = f"ANOMALY {question}"

    return {
        "section": SECTION_NAME,
        "number": number,
        "question": question,
        "answer": answer,
        "explanation": explanation,
    }


def build_payload(pdf_path: Path) -> dict[str, object]:
    text = extract_pdf_text(pdf_path)
    questions = [parse_block(block) for block in split_blocks(text)]

    return {
        "schemaVersion": 1,
        "updatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "area": "Lógica II",
        "type": "True False",
        "shortName": "log2",
        "language": "es",
        "questions": questions,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert the Lógica II PDF questions into JSON."
    )
    parser.add_argument("--pdf", type=Path, default=PDF_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = build_payload(args.pdf)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
