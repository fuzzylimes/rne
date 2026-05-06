from __future__ import annotations

import argparse


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rne",
        description="Rip-n-Encode pipeline CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ingest", help="Interactive disc ingest flow")

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

        run()
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
