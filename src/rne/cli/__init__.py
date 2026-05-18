from __future__ import annotations

import argparse


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rne",
        description="Rip-n-Encode pipeline CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_p = sub.add_parser("ingest", help="Rip a disc and queue the rips for encoding.")
    ingest_p.add_argument(
        "-m", "--minlength",
        type=int,
        default=900,
        metavar="SECONDS",
        help="Minimum title length in seconds (default: 900)",
    )

    queue_p = sub.add_parser(
        "queue", help="Queue already-ripped .mkv files for encoding."
    )
    queue_p.add_argument(
        "path",
        help="Path to a .mkv file or a directory containing .mkv files",
    )

    ls_p = sub.add_parser("ls", help="List jobs")
    ls_p.add_argument("--all", action="store_true", help="Show full history")
    ls_p.add_argument(
        "--status",
        metavar="STATUS",
        help="Filter by status (comma-separated)",
    )

    edit_p = sub.add_parser("edit", help="Edit a job's HandBrake args in $EDITOR")
    edit_p.add_argument("id", type=int, help="Job ID")

    cancel_p = sub.add_parser("cancel", help="Cancel a queued job (terminal)")
    cancel_p.add_argument("id", type=int, help="Job ID")

    retry_p = sub.add_parser("retry", help="Retry a terminal-state job")
    retry_p.add_argument("id", type=int, help="Job ID")

    sub.add_parser("pause", help="Pause the global queue")
    sub.add_parser("resume", help="Resume the global queue")

    service_p = sub.add_parser("service", help="Manage systemd unit files")
    service_sub = service_p.add_subparsers(dest="service_action", required=True)
    service_sub.add_parser("install", help="Install systemd user services")
    service_sub.add_parser("uninstall", help="Remove systemd user services")

    return parser


def main() -> None:
    args = _build_parser().parse_args()

    if args.command == "ingest":
        from rne.cli.ingest import run

        run(args)
    elif args.command == "queue":
        from rne.cli.queue import run

        run(args)
    elif args.command == "ls":
        from rne.cli.ls import run

        run(args)
    elif args.command == "edit":
        from rne.cli.edit import run

        run(args)
    elif args.command == "cancel":
        from rne.cli.manage import run_cancel

        run_cancel(args)
    elif args.command == "retry":
        from rne.cli.manage import run_retry

        run_retry(args)
    elif args.command == "pause":
        from rne.cli.manage import run_pause

        run_pause(args)
    elif args.command == "resume":
        from rne.cli.manage import run_resume

        run_resume(args)
    elif args.command == "service":
        from rne.cli.service import install, uninstall

        if args.service_action == "install":
            install()
        elif args.service_action == "uninstall":
            uninstall()
