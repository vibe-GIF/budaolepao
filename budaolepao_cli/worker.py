"""Internal child process. Preview performs no emulator or Android operations."""

import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

from .control import EVENT_PREFIX, runner_module, validate_configuration


def emit(event):
    print(EVENT_PREFIX + json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)


def preview(config, duration=8.0, *, emit_event=emit, sleep=time.sleep, clock=time.monotonic):
    """Show the configured path in a bounded accelerated offline rehearsal."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("preview duration must be positive and finite")
    runner = runner_module()
    cfg = runner.Config(**validate_configuration(config))
    route = cfg.walk_path
    lengths = [runner.geo_dist_m(a[1], a[0], b[1], b[0]) for a, b in zip(route, route[1:] + route[:1])]
    loop_length = sum(lengths)
    speed = (cfg.min_speed_mps / 2 + cfg.max_speed_mps / 2) if cfg.min_speed_mps is not None else cfg.base_speed_mps
    started = clock()
    while True:
        fraction = min(1.0, max(0.0, (clock() - started) / duration))
        distance = cfg.dist_limit_m * fraction
        along = distance % loop_length
        segment = 0
        while segment < len(lengths) - 1 and along >= lengths[segment]:
            along -= lengths[segment]
            segment += 1
        ratio = along / lengths[segment]
        a, b = route[segment], route[(segment + 1) % len(route)]
        elapsed = distance / speed
        emit_event({"type": "progress", "progress": {
            "distance_m": distance, "speed_mps": speed, "elapsed_s": elapsed,
            "steps": int(elapsed * 2), "lon": a[0] + (b[0] - a[0]) * ratio,
            "lat": a[1] + (b[1] - a[1]) * ratio,
        }})
        if fraction >= 1:
            break
        sleep(min(0.05, duration))
    emit_event({"type": "completed", "platform_accepted": None})


def main():
    parser = argparse.ArgumentParser(description="Internal local controller worker")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--mode", choices=("preview", "emulator"), required=True)
    parser.add_argument("--preview-duration", type=float, default=8.0)
    args = parser.parse_args()
    try:
        config = validate_configuration(json.loads(args.config.read_text(encoding="utf-8")))
        if args.mode == "preview":
            print("Offline preview: accelerated local route only; no emulator or network requests.", flush=True)
            preview(config, args.preview_duration)
        else:
            os.chdir(args.root / "poc/emulator_run")
            runner = runner_module()
            runner.run(runner.Config(**config), event_callback=emit, quiet=True)
            emit({"type": "completed", "platform_accepted": None})
    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
