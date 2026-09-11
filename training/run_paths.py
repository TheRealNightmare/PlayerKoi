"""Locating trained-model artifacts under runs/classify/.

Ultralytics auto-increments run directories (train, train-2, train-3, ...),
so no fixed path stays correct for long. A hardcoded default like
`runs/classify/train/weights/best.pt` is worse than useless once a second
run exists: it silently exports the *previous* model rather than erroring.

So the scripts resolve weights through here instead, in this order:

    --weights/--model-dir <path>   explicit, always wins
    --run <name>                   a run directory under runs/classify/
    (neither)                      the newest run, after confirming

The confirmation is the point of the middle ground: inferring a path is
convenient but shouldn't be silent, since acting on the wrong model
produces a wrong artifact rather than a visible error. --yes skips it.

Pure pathlib -- no ultralytics or torch import here, so this stays cheap to
import and testable without a GPU.
"""

import datetime as dt
import sys
from pathlib import Path

RUNS_DIR = Path("runs/classify")

# The docs used to print this as a placeholder inside copy-pasteable command
# blocks, so it got pasted verbatim -- and the resulting "file not found" gave
# no hint that the path was never meant to be real. Detect it by name and say
# so outright.
PLACEHOLDER_HINT = "train-N"


def _artifact(run_dir, kind):
    """The artifact inside one run directory, or None if it isn't there.

    A run directory only counts if it actually holds the thing being looked
    for -- which is what keeps ultralytics' own `val/`, `val-2/` scratch
    directories (created by each test-split scoring pass, and containing no
    weights) out of the listings.
    """
    weights = run_dir / "weights"
    if kind == "pt":
        best = weights / "best.pt"
        return best if best.is_file() else None
    if kind == "ncnn":
        # Either the name ultralytics exports as, or the name it gets renamed
        # to for the Pi (models/square_classifier_ncnn_model).
        candidates = sorted(weights.glob("*_ncnn_model"))
        for candidate in candidates:
            if candidate.is_dir():
                return candidate
        return None
    raise ValueError(f"unknown artifact kind: {kind!r}")


def list_runs(kind="pt", runs_dir=RUNS_DIR):
    """[(name, path, mtime)] for runs holding this artifact, newest first."""
    runs = []
    if not runs_dir.is_dir():
        return runs
    for run_dir in runs_dir.iterdir():
        if not run_dir.is_dir():
            continue
        artifact = _artifact(run_dir, kind)
        if artifact is not None:
            runs.append((run_dir.name, artifact, artifact.stat().st_mtime))
    runs.sort(key=lambda item: item[2], reverse=True)
    return runs


def _stamp(mtime):
    return dt.datetime.fromtimestamp(mtime).strftime("%b %d %H:%M")


def format_runs(runs):
    """Aligned listing for error messages. Empty string if there are none."""
    if not runs:
        return ""
    width = max(len(name) for name, _, _ in runs)
    lines = [f"  {name:<{width}}  {_stamp(mtime)}  {path}" for name, path, mtime in runs]
    return "\n".join(lines)


def _missing(path, kind, runs_dir):
    """SystemExit for a path that isn't there, naming what *is*."""
    lines = []
    if PLACEHOLDER_HINT in str(path):
        lines.append(
            f"'{PLACEHOLDER_HINT}' is a placeholder from the docs, not a real path."
        )
        lines.append("Omit the flag entirely to use the newest run.")
    else:
        lines.append(f"not found: {path}")

    runs = list_runs(kind, runs_dir)
    if runs:
        lines.append("\nRuns you actually have:")
        lines.append(format_runs(runs))
        lines.append(f"\nPick one with --run {runs[0][0]}, or omit the flag for the newest.")
    else:
        lines.append(f"\nNo runs with weights under {runs_dir}/ -- train first.")
    return SystemExit("\n".join(lines))


def resolve(explicit=None, run_name=None, kind="pt", assume_yes=False, runs_dir=RUNS_DIR):
    """The artifact to act on. Raises SystemExit with guidance on failure."""
    if explicit is not None:
        path = Path(explicit)
        exists = path.is_file() if kind == "pt" else path.is_dir()
        if not exists:
            raise _missing(path, kind, runs_dir)
        return path

    if run_name is not None:
        artifact = _artifact(runs_dir / run_name, kind)
        if artifact is None:
            raise _missing(runs_dir / run_name, kind, runs_dir)
        return artifact

    runs = list_runs(kind, runs_dir)
    if not runs:
        raise _missing(runs_dir / "<run>", kind, runs_dir)

    name, path, mtime = runs[0]
    print(f"Using newest run: {name} ({_stamp(mtime)})\n  {path}")
    if len(runs) > 1:
        print(f"({len(runs) - 1} older run(s) available -- pass --run NAME to pick one.)")

    if assume_yes:
        return path
    if not sys.stdin.isatty():
        # Nothing can answer the prompt, so asking would hang a script or a
        # CI job forever. Fail with the two ways out instead.
        raise SystemExit(
            "Refusing to guess without confirmation and stdin is not a terminal.\n"
            "Pass --yes to accept the newest run, or --run NAME to choose explicitly."
        )
    answer = input("Use it? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        raise SystemExit("Aborted. Pass --run NAME to choose a different run.")
    return path


def add_arguments(parser, flag, kind_help):
    """The three flags every resolving script shares, so they stay identical."""
    parser.add_argument(flag, type=Path, default=None, help=kind_help)
    parser.add_argument("--run", default=None,
                        help="run directory under runs/classify/ (e.g. --run train-2). "
                             "Shorter than the full path; omit both to use the newest run")
    parser.add_argument("--yes", action="store_true",
                        help="accept the newest run without the confirmation prompt")
