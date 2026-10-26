"""Explicit owner commands; no automatic commit, push, or publication menu."""
import argparse
import json
from pathlib import Path
import sys

from .application import dispatch
from .common import ApiError
from .events import event_sink
from .processes import cancellation_scope
from .repository import repository_name


def main(argv):
    parser = argparse.ArgumentParser(prog="pr-review owner")
    actions = ["open", "import", "discover", "read", "reconcile", "refresh", "fix", "resume", "diff", "prepare-commit", "associate", "prepare", "publish", "recover", "triage", "reply", "settings", "resolve", "reopen", "cancel"]
    parser.add_argument("pr", nargs="?", help="PR number or URL (optional with --session)")
    parser.add_argument("action", nargs="?", default="read", choices=actions)
    parser.add_argument("--session")
    parser.add_argument("-R", "--repo", dest="repository")
    parser.add_argument("--item", dest="items", action="append")
    parser.add_argument("--batch")
    parser.add_argument("--task")
    parser.add_argument("--skip", action="append", default=[])
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--replan", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--current", action="store_true", help="inspect partial changes since the batch checkpoint")
    parser.add_argument("--operation-id", help="operation to cancel")
    parser.add_argument("--commit", dest="commits", action="append")
    parser.add_argument("--confirm", dest="token", help="exact token from prepare; required to publish")
    parser.add_argument("--trust-agent-config", dest="trusted_snapshot")
    parser.add_argument("--operation", choices=["acknowledge", "defer", "restore", "handled", "stage", "unstage", "acknowledge-reply", "edit"])
    parser.add_argument("--reply", dest="reply_id")
    parser.add_argument("--body-file")
    parser.add_argument("--settings-file")
    parser.add_argument("--review-session", help="source reviewer session for import")
    parser.add_argument("--comment", dest="comments", action="append", help="saved reviewer comment to import; repeat for a group")
    parser.add_argument("--generation", help="source reviewer generation for import")
    parser.add_argument("--revision", help="source reviewer revision for import")
    args = vars(parser.parse_args(argv))
    if args["action"] == "read" and args.get("pr") in actions:
        args["action"], args["pr"] = args["pr"], None
    def progress(kind, **fields):
        if kind == "progress":
            print(fields["message"], file=sys.stderr, flush=True)
        elif kind == "started":
            print("Operation: " + fields["operation_id"], file=sys.stderr, flush=True)
    def check_target(view):
        if args["pr"] and args["pr"].rstrip("/").lower() not in (str(view["pr"]), view["url"].lower()):
            raise ApiError("invalid_request", "The requested PR differs from the selected owner session.")
        if args["repository"] and repository_name(args["repository"]) != view["repository"]:
            raise ApiError("invalid_request", "The requested repository differs from the selected owner session.")
    try:
        with event_sink(progress), cancellation_scope():
            if args["action"] == "import":
                if not args["review_session"] or args["session"]:
                    raise ApiError("invalid_request", "Use --review-session for import; its PR determines the owner destination.")
                source = dispatch({"command": "read", "session": args["review_session"], "generation": args["generation"]})
                check_target(source)
                result = dispatch({"command": "owner-import", "session": source["session"], "generation": source["generation"],
                                   "revision": args["revision"] or source["revision"], "comments": args["comments"]})
                print(json.dumps(result, indent=2))
                return 0
            if args["action"] in ("discover", "cancel"):
                command = "cancel" if args["action"] == "cancel" else "owner-discover"
                print(json.dumps(dispatch({"command": command, **{k: v for k, v in args.items() if v is not None}}), indent=2))
                return 0
            if args["session"]:
                view = dispatch({"command": "owner-read", "session": args["session"]})
                check_target(view)
            else:
                view = dispatch({"command": "owner-open", "pr": args["pr"], "repository": args["repository"]})
            action = args.pop("action")
            request = {k: v for k, v in args.items() if v is not None}
            request.update(session=view["session"], revision=view["revision"], command="owner-" + action)
            if action == "open":
                request["command"] = "owner-read"
            if action in ("resolve", "reopen"):
                request.update(command="owner-thread-state", resolved=action == "resolve", item=(args.get("items") or [None])[0])
            if action == "resume":
                request.update(command="owner-fix", batch=args.get("batch") or (view["batches"][-1]["id"] if view["batches"] else None))
                if not request["batch"]:
                    raise ApiError("invalid_request", "No saved batch to resume.")
            if action == "publish" and not request.get("token"):
                raise ApiError("confirmation_required", "Inspect prepare output and pass its token with --confirm.")
            if action in ("triage", "reply", "settings"):
                request["command"] = "owner-mutate"
                if action == "settings":
                    if not args.get("settings_file"):
                        print(json.dumps(view["settings"], indent=2))
                        return 0
                    request.update(operation="settings", settings=json.loads(Path(args["settings_file"]).read_text()))
                elif action == "reply":
                    request.update(operation="reply", item=(args.get("items") or [None])[0])
                if args.get("body_file"):
                    request["body"] = Path(args["body_file"]).read_text()
                if args.get("reply_id"):
                    request["reply"] = args["reply_id"]
            result = dispatch(request)
            print(json.dumps(result, indent=2))
            return 0
    except (ApiError, OSError, ValueError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        if isinstance(exc, ApiError) and exc.details:
            print(json.dumps(exc.details), file=sys.stderr)
        return exc.details.get("exit_code", 1) if isinstance(exc, ApiError) else 1
