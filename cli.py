#!/usr/bin/env python3
"""Dispatcher: discovers tasks, parses shared flags, builds the context, runs one.

    ./shopify.sh <task-name> [task flags]
    ./shopify.sh --help                    # lists every discovered task

Shared flags are defined once here rather than in each task, so ``--shop``,
``--commit`` and ``--json`` behave identically everywhere. A task adds its own
flags via a module-level ``add_arguments(parser)``.

Exit codes are distinct because these runs end up in cron, where the only
signal is the status:

    0  success
    1  the task failed for its own reasons (TaskError)
    2  usage or configuration problem -- nothing was attempted
    3  the Shopify API failed (auth, transport, GraphQL, userErrors)
    4  a mutation was attempted during a dry run (a bug in the task)
  130  interrupted
"""
from __future__ import annotations

import argparse
import importlib
import logging
import pkgutil
import sys

from shopifylib import output
from shopifylib.client import ShopifyClient
from shopifylib.config import load_config
from shopifylib.errors import (
    ConfigError,
    ShopifyTasksError,
    TaskError,
    TransportError,
    WriteBlocked,
)
from shopifylib.task import REGISTRY, TaskContext, TaskSpec

EXIT_OK = 0
EXIT_TASK_FAILED = 1
EXIT_USAGE = 2
EXIT_API = 3
EXIT_WRITE_BLOCKED = 4
EXIT_INTERRUPTED = 130


def discover_tasks() -> dict[str, TaskSpec]:
    """Import every module in ``tasks/`` so its @task decorator registers.

    Eager import keeps ``--help`` honest -- it lists what actually exists rather
    than a hand-maintained table that drifts. These modules are small and pure
    Python, so importing all of them costs milliseconds; if that ever stops
    being true, the fix is lazy metadata, not a manual registry.
    """
    import tasks

    for module_info in pkgutil.iter_modules(tasks.__path__):
        if module_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"tasks.{module_info.name}")
        except Exception as exc:
            # One broken task must not take down the whole CLI -- you need the
            # dispatcher working in order to fix it.
            print(
                f"warning: could not load tasks/{module_info.name}.py: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        # Attach the module's optional argument hook to its registered spec.
        adder = getattr(module, "add_arguments", None)
        if adder:
            for spec in REGISTRY.values():
                if spec.module == module.__name__:
                    spec.add_arguments = adder
    return REGISTRY


def _add_shared_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Define the flags that apply to every task.

    These are added twice: once to the top-level parser and once to each
    subparser, so both orderings work --

        shopify --shop dev shop-info
        shopify shop-info --shop dev

    The second is what people actually type (the task is the subject; the flags
    qualify it), and argparse would otherwise reject it, since options defined
    before a subcommand may only appear before it.

    On the subparser copies, every default is SUPPRESS: an unsupplied flag then
    leaves the attribute unset rather than writing a default over the value the
    top-level parser already parsed. Without that, the subparser would clobber
    `--shop dev` back to None in the first form above.
    """
    default = argparse.SUPPRESS if suppress else None
    flag_default = argparse.SUPPRESS if suppress else False

    parser.add_argument("--shop", metavar="ID", default=default,
                        help="which configured store to use (default: config's default_shop)")
    parser.add_argument("--commit", action="store_true", default=flag_default,
                        help="actually write to the store (write tasks preview by default)")
    parser.add_argument("--dry-run", action="store_true", default=flag_default,
                        help="force preview mode; overrides --commit")
    parser.add_argument("--api-version", metavar="YYYY-MM", default=default,
                        help="override the configured API version (e.g. to try a release candidate)")
    parser.add_argument("--config", metavar="PATH", default=default,
                        help="use an alternate config file")
    parser.add_argument("--json", dest="json_output", action="store_true", default=flag_default,
                        help="emit machine-readable results on stdout")
    parser.add_argument("-v", "--verbose", action="store_true", default=flag_default,
                        help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true", default=flag_default,
                        help="warnings and errors only")


def build_parser(specs: dict[str, TaskSpec]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shopify",
        description="Run a Shopify admin task against a configured store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Tasks that write to the store run as a dry run by default; pass --commit\n"
            "to apply changes for real. Configure stores in config.toml (see\n"
            "config.toml.example)."
        ),
    )
    _add_shared_flags(parser, suppress=False)

    # Parent carrying the same flags for the subparsers, with suppressed
    # defaults so it augments the top-level namespace instead of overwriting it.
    shared = argparse.ArgumentParser(add_help=False)
    _add_shared_flags(shared, suppress=True)

    subparsers = parser.add_subparsers(dest="task_name", metavar="<task>")
    for name in sorted(specs):
        spec = specs[name]
        # The "[writes]" marker in --help is the at-a-glance answer to "is this
        # one safe to just run?", which is the first thing you want to know.
        sub = subparsers.add_parser(
            name,
            help=spec.help + (" [writes]" if spec.writes else ""),
            description=spec.description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            parents=[shared],
        )
        if spec.add_arguments:
            spec.add_arguments(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    specs = discover_tasks()
    parser = build_parser(specs)
    args = parser.parse_args(argv)

    if not args.task_name:
        parser.print_help()
        return EXIT_USAGE

    log = output.setup_logging(verbose=args.verbose, quiet=args.quiet)
    spec = specs[args.task_name]

    # --dry-run beats --commit: when both are present the user is more likely
    # to have appended a safety flag than to want the dangerous one to win.
    dry_run = spec.writes and (args.dry_run or not args.commit)
    if args.dry_run and args.commit:
        log.warning("--dry-run and --commit both given; running as a dry run.")
    if args.commit and not spec.writes:
        log.info("%s does not write to the store; --commit has no effect.", spec.name)

    try:
        config = load_config(args.config)
        shop = config.get(args.shop)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.api_version:
        from dataclasses import replace
        shop = replace(shop, api_version=args.api_version)
        log.info("Using API version %s (overridden)", shop.api_version)

    log.info(
        "%s on %s (%s)%s",
        spec.name, shop.id, shop.domain,
        "  [DRY RUN -- pass --commit to apply]" if dry_run else "",
    )

    try:
        with ShopifyClient(
            shop,
            # The backstop: a mutation cannot leave the process unless this is
            # True, regardless of what the task's own logic does.
            writes_enabled=spec.writes and not dry_run,
            required_scopes=spec.scopes,
        ) as client:
            ctx = TaskContext(
                client=client,
                shop=shop,
                dry_run=dry_run,
                log=logging.getLogger(f"task.{spec.name}"),
                json_output=args.json_output,
                spec=spec,
            )
            result = spec.run(ctx, args)
            _report(ctx, log, dry_run)
            return result if isinstance(result, int) else EXIT_OK

    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except WriteBlocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_WRITE_BLOCKED
    except TaskError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_TASK_FAILED
    except (TransportError, ShopifyTasksError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_API


def _report(ctx: TaskContext, log: logging.Logger, dry_run: bool) -> None:
    """End-of-run summary, to stderr so it never pollutes --json output."""
    summary = ctx.summary()
    if dry_run and summary["writes_skipped"]:
        log.info(
            "Dry run complete: %d change(s) would be made, %d API request(s). "
            "Re-run with --commit to apply.",
            summary["writes_skipped"], summary["api_requests"],
        )
    elif summary["writes_performed"] or summary.get("writes_failed"):
        log.info(
            "Done: %d change(s) applied, %d API request(s).",
            summary["writes_performed"], summary["api_requests"],
        )
        if summary.get("writes_failed"):
            log.warning("%d mutation(s) reported errors and changed nothing.",
                        summary["writes_failed"])
    else:
        log.info("Done: %d API request(s).", summary["api_requests"])

    slept = summary["throttle"]["total_slept_seconds"]
    if slept > 1.0:
        log.info("Spent %.1fs waiting on the rate limiter.", slept)


if __name__ == "__main__":
    sys.exit(main())
