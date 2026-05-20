#!/usr/bin/env python3
"""Machine-translate English AsciiDoc docs to FR/ES/AR/ZH/RU using Claude.

Usage:
    python docs/translate.py --all              # all non-EN languages
    python docs/translate.py --lang fr          # French only
    python docs/translate.py --lang fr --force  # overwrite existing translations

Requires ANTHROPIC_API_KEY environment variable.
"""

import argparse
import os
import sys
from pathlib import Path

import anthropic

LANGUAGES = {
    'fr': 'French',
    'es': 'Spanish',
    'ar': 'Arabic (formal Modern Standard Arabic)',
    'zh': 'Simplified Chinese',
    'ru': 'Russian',
}

DOCS_DIR = Path(__file__).parent
SOURCE_DIR = DOCS_DIR / 'en'

SYSTEM_PROMPT_TEMPLATE = """\
You are a technical translator. Translate the AsciiDoc document below to {language}.

Rules:
- Preserve ALL AsciiDoc markup exactly: heading markers (= ==), block delimiters \
(---- .... ++++ ====), attribute definitions (:name: value), block attributes \
([source,python]), cross-references (<<ref>>), include/image directives, etc.
- Do NOT translate content inside source/listing code blocks (between ---- delimiters).
- Do NOT translate technical terms or product names: WIS2, MQTT, Redis, Celery,
  WMO, BUFR, GRIB, GDC, WCMP2, Docker, and all acronyms/identifiers.
- Do NOT translate attribute placeholders such as {{varname}}.
- Do NOT translate URLs, hostnames, file paths, or command-line examples.
- For Arabic use formal Modern Standard Arabic (MSA).
- Return ONLY the translated AsciiDoc document. No preamble, no explanation.\
"""

DISCLAIMER_TEMPLATE = """\
[NOTE]
====
This document was machine-translated from English using Claude AI.
WMO/meteorological domain terms should be reviewed by a native speaker before
production use.  See the link:../en/[English original] for the authoritative version.
====

"""

# Rough token estimate: 1 token ≈ 4 characters.  Split files above this size.
SPLIT_THRESHOLD_CHARS = 48_000  # ~12 000 tokens


def _estimate_needs_split(text: str) -> bool:
    return len(text) > SPLIT_THRESHOLD_CHARS


def _split_on_top_level_sections(text: str) -> list[str]:
    """Split an AsciiDoc file on top-level section boundaries (^== )."""
    parts = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith('== ') and current:
            parts.append(''.join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        parts.append(''.join(current))
    return parts if len(parts) > 1 else [text]


def translate_text(client: anthropic.Anthropic, text: str, language: str) -> str:
    system = SYSTEM_PROMPT_TEMPLATE.format(language=language)

    if _estimate_needs_split(text):
        parts = _split_on_top_level_sections(text)
        translated_parts = []
        for i, part in enumerate(parts, 1):
            print(f'    chunk {i}/{len(parts)} ({len(part)} chars)…', end=' ', flush=True)
            resp = client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=16000,
                system=system,
                messages=[{'role': 'user', 'content': part}],
            )
            translated_parts.append(resp.content[0].text)
            print('ok')
        return ''.join(translated_parts)

    resp = client.messages.create(
        model='claude-sonnet-4-6',
        max_tokens=16000,
        system=system,
        messages=[{'role': 'user', 'content': text}],
    )
    return resp.content[0].text


def translate_file(
    client: anthropic.Anthropic,
    src: Path,
    lang_code: str,
    language_name: str,
    force: bool,
) -> None:
    dest_dir = DOCS_DIR / lang_code
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / src.name

    if dest.exists() and not force:
        print(f'  skip {dest} (already exists; use --force to overwrite)')
        return

    print(f'  translating {src.name} → {lang_code}/', end=' ', flush=True)
    source_text = src.read_text(encoding='utf-8')
    translated = translate_text(client, source_text, language_name)
    output = DISCLAIMER_TEMPLATE + translated
    dest.write_text(output, encoding='utf-8')
    print('done')


def main() -> None:
    parser = argparse.ArgumentParser(description='Translate docs/en/*.adoc to other languages.')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--all', action='store_true', help='Translate to all non-EN languages')
    group.add_argument('--lang', choices=list(LANGUAGES), help='Translate to a single language')
    parser.add_argument('--force', action='store_true', help='Overwrite existing translations')
    args = parser.parse_args()

    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        print('Error: ANTHROPIC_API_KEY environment variable is not set.', file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    source_files = sorted(SOURCE_DIR.glob('*.adoc'))
    if not source_files:
        print(f'No .adoc files found in {SOURCE_DIR}', file=sys.stderr)
        sys.exit(1)

    targets = LANGUAGES if args.all else {args.lang: LANGUAGES[args.lang]}

    for lang_code, language_name in targets.items():
        print(f'\n[{lang_code}] {language_name}')
        for src in source_files:
            translate_file(client, src, lang_code, language_name, args.force)

    print('\nTranslation complete.')


if __name__ == '__main__':
    main()
