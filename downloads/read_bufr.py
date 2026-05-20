#!/usr/bin/env python3

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _default_bufr_path() -> Path:
	here = Path(__file__).resolve().parent
	pacific_dir = here / "pacific-obs" / "2026" / "03" / "16"
	if not pacific_dir.exists():
		raise FileNotFoundError(f"Directory not found: {pacific_dir}")

	# Prefer .bufr files; fall back to .bufr4 if needed.
	candidates = sorted(pacific_dir.glob("*.bufr"))
	if not candidates:
		candidates = sorted(pacific_dir.glob("*.bufr4"))
	if not candidates:
		raise FileNotFoundError(f"No BUFR files found under: {pacific_dir}")
	return candidates[0]


def _eccodes_available() -> bool:
	try:
		import eccodes  # noqa: F401

		return True
	except Exception:
		return False


def _bufr_dump_available() -> bool:
	from shutil import which

	return which("bufr_dump") is not None


def _read_with_eccodes(path: Path, *, max_keys: int, max_values: int) -> int:
	import eccodes

	def safe_get(key: str):
		try:
			return eccodes.codes_get(h, key)
		except Exception:
			return None

	with path.open("rb") as f:
		msg_index = 0
		while True:
			h = eccodes.codes_bufr_new_from_file(f)
			if h is None:
				break
			msg_index += 1
			try:
				eccodes.codes_set(h, "unpack", 1)
				print(f"=== BUFR message #{msg_index} ===")
				print(f"file: {path}")

				header_keys = [
					"edition",
					"masterTableNumber",
					"bufrHeaderCentre",
					"bufrHeaderSubCentre",
					"dataCategory",
					"internationalDataSubCategory",
					"dataSubCategory",
					"typicalYear",
					"typicalMonth",
					"typicalDay",
					"typicalHour",
					"typicalMinute",
					"numberOfSubsets",
					"compressedData",
				]
				for k in header_keys:
					v = safe_get(k)
					if v is not None:
						print(f"{k}: {v}")

				print("\n--- sample keys/values (truncated) ---")
				it = eccodes.codes_keys_iterator_new(h)
				keys_shown = 0
				values_shown = 0
				try:
					while eccodes.codes_keys_iterator_next(it):
						key = eccodes.codes_keys_iterator_get_name(it)
						if not key or key.startswith("#"):
							continue

						# Skip very noisy iterator metadata keys.
						if key in {"unpack", "skipExtraKeyAttributes"}:
							continue

						# Show a bounded number of keys.
						keys_shown += 1
						if keys_shown > max_keys:
							break

						try:
							val = eccodes.codes_get(h, key)
							values_shown += 1
							if values_shown <= max_values:
								print(f"{key}: {val}")
						except Exception:
							# Some keys are not directly readable depending on message.
							continue
				finally:
					eccodes.codes_keys_iterator_delete(it)

				if keys_shown > max_keys or values_shown > max_values:
					print("(output truncated)")
			finally:
				eccodes.codes_release(h)

		if msg_index == 0:
			print(f"No BUFR messages found in: {path}", file=sys.stderr)
			return 2

	return 0


def _read_with_bufr_dump(path: Path, *, max_lines: int) -> int:
	# Try to keep output manageable: show a plain dump, truncated.
	cmd = ["bufr_dump", "-p", str(path)]
	try:
		proc = subprocess.Popen(
			cmd,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			text=True,
			bufsize=1,
		)
	except FileNotFoundError:
		print("bufr_dump not found in PATH", file=sys.stderr)
		return 2

	assert proc.stdout is not None
	for i, line in enumerate(proc.stdout, start=1):
		if i > max_lines:
			print("(output truncated)")
			proc.kill()
			break
		print(line.rstrip("\n"))

	return proc.wait()


def main(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(
		description="Read one BUFR file from pacific-obs (or a provided file)."
	)
	parser.add_argument(
		"path",
		nargs="?",
		default=None,
		help="Path to a .bufr/.bufr4 file. If omitted, picks the first file under pacific-obs/2026/03/16.",
	)
	parser.add_argument(
		"--max-keys",
		type=int,
		default=60,
		help="Max number of BUFR keys to scan (eccodes mode).",
	)
	parser.add_argument(
		"--max-values",
		type=int,
		default=40,
		help="Max number of key/value lines to print (eccodes mode).",
	)
	parser.add_argument(
		"--max-lines",
		type=int,
		default=250,
		help="Max number of lines to print (bufr_dump fallback).",
	)
	args = parser.parse_args(argv)

	try:
		path = Path(args.path).expanduser() if args.path else _default_bufr_path()
	except Exception as e:
		print(str(e), file=sys.stderr)
		return 2

	if not path.exists():
		print(f"File not found: {path}", file=sys.stderr)
		return 2

	if _eccodes_available():
		return _read_with_eccodes(path, max_keys=args.max_keys, max_values=args.max_values)

	if _bufr_dump_available():
		print("eccodes Python bindings not available; falling back to bufr_dump", file=sys.stderr)
		return _read_with_bufr_dump(path, max_lines=args.max_lines)

	print(
		"Neither Python eccodes nor bufr_dump was found.\n"
		"- Option A (recommended): install eccodes Python package in your venv: pip install eccodes\n"
		"- Option B: install ecCodes CLI tools and ensure bufr_dump is on PATH",
		file=sys.stderr,
	)
	return 2


if __name__ == "__main__":
	raise SystemExit(main(sys.argv[1:]))

