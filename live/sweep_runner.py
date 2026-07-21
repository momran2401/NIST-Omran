#!/usr/bin/env python3
"""Supervised striqt sweep child used by the web recording controller."""
import argparse
import json
import signal
import time


def emit(kind, **fields):
    print(json.dumps({"event": kind, **fields}), flush=True)


def run_sweep(spec_path, output, duration=None, should_stop=lambda: False,
              progress=emit, source=None):
    """Run a repeating sweep with cooperative Stop and progress callbacks."""
    import striqt.sensor as sensor
    spec = sensor.read_yaml_spec(spec_path)
    # This striqt release chooses its ZIP wrapper from the suffix while the
    # intermediate Zarr store itself must remain a directory.
    spec = spec.replace(
        sink=spec.sink.replace(path=str(output), store="directory"))
    started = time.monotonic()
    steps = 0
    progress("opened")
    if source is None:
        resource_context = sensor.open_resources(spec, spec_path)
    else:
        # AIR-T keeps one initialized device singleton per process. Build the
        # remaining lightweight resources around the live source object rather
        # than trying to construct a second radio controller.
        import contextlib
        from striqt.sensor.lib import bindings, peripherals
        from striqt.sensor.lib.resources import ConnectionManager, _open_sink

        # The source registry keys by the exact immutable source spec. Use the
        # already-open live source's spec so sink path formatting and capture
        # expansion can resolve its radio ID without constructing a new device.
        spec = spec.replace(source=source.setup_spec)

        stack = contextlib.ExitStack()
        if hasattr(bindings, "get_binding"):
            sink_cls = bindings.get_binding(spec).sink
        else:
            sink_cls = bindings.get_controller(spec).sensor.sink_cls
        sink = stack.enter_context(
            _open_sink(spec, sink_cls, None))
        peripheral = stack.enter_context(peripherals.NoPeripherals(spec))
        connection = ConnectionManager(spec)
        connection._resources.update(
            source=source, sink=sink, peripherals=peripheral,
            calibration=None, alias_func=None)
        resources = connection.resources
        captures = sensor.specs.helpers.loop_captures(
            spec, source_id=source.id)
        if captures:
            source.arm_spec(captures[0])
            # arm_spec skips stream recreation when the capture recipe is
            # unchanged; live handoff deliberately closed that stream.
            from core.shims import open_stream
            open_stream(source)

        @contextlib.contextmanager
        def existing_resources():
            with stack:
                yield resources

        resource_context = existing_resources()

    with resource_context as resources:
        sweep = sensor.iterate_sweep(
            resources, yield_values=False, always_yield=True, loop=True)
        try:
            for _ in sweep:
                steps += 1
                # iterate_sweep is a three-stage acquire/analyze/sink pipeline.
                # The first durable capture appears after the third yielded
                # step; stopping earlier discards in-flight analysis.
                count = max(0, steps - 2)
                elapsed = time.monotonic() - started
                progress("progress", captures=count,
                         elapsed_s=round(elapsed, 3))
                limit_hit = should_stop() or (duration and elapsed >= duration)
                if limit_hit and steps >= 3:
                    break
        finally:
            sweep.close()
    result = {"captures": max(0, steps - 2),
              "elapsed_s": round(time.monotonic() - started, 3)}
    progress("stopped", **result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("spec")
    parser.add_argument("--output", required=True)
    parser.add_argument("--duration", type=float)
    args = parser.parse_args()
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    run_sweep(args.spec, args.output, args.duration,
              should_stop=lambda: stopping, progress=emit)


if __name__ == "__main__":
    main()
