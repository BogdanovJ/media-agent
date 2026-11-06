"""A worker reads media and writes exclusively to its assigned staging folder."""
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from .media import convert, probe, validate
from .quarantine import fingerprint
from .safe_move import move_no_replace


def run(source, directory, action, candidate, threads):
    identity = fingerprint(source)
    # Bind crash-left output to the exact source used before starting FFmpeg.
    intent = directory / "input.json"
    expected = dict(source=str(source), original=list(identity), action=action, candidate=str(candidate or ""))
    if not intent.exists():
        if (directory / "output.mp4").exists():
            raise ValueError("Unattributed staged output")
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as handle:
            json.dump(expected, handle)
        try:
            move_no_replace(handle.name, intent)
        except FileExistsError:
            pass
        finally:
            Path(handle.name).unlink(missing_ok=True)
    if json.loads(intent.read_text()) != expected:
        raise ValueError("Source or assignment changed since staging began")
    output = candidate or directory / "output.mp4"
    if output.resolve() != output:
        raise ValueError("Unsafe output path")
    if candidate or output.exists():
        validate(probe(source), output)
    else:
        convert(source, output, action, threads)
    output_identity = fingerprint(output)
    if fingerprint(source) != identity:
        raise ValueError("Source changed during processing")
    receipt = dict(source=str(source), action=action, candidate=str(candidate or ""),
                   original=identity, verified=output_identity)
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as handle:
        json.dump(receipt, handle)
    os.replace(handle.name, directory / "validated.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("action", choices=("copy", "remux", "transcode"))
    parser.add_argument("candidate", nargs="?", type=Path)
    args = parser.parse_args()
    directory = Path("/work")
    try:
        run(args.source, directory, args.action, args.candidate, 2)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        # Persist terminal validation failures beyond the Kubernetes Job TTL.
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as handle:
            handle.write(str(exc))
        os.replace(handle.name, directory / "review.txt")
        raise
