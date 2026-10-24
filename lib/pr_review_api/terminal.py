"""Small terminal adapter for drafts already adopted by the public API.

This keeps one source of human edits/staging after switching between Emacs and
the terminal, instead of accidentally publishing the old aggregate Markdown.
"""
import os
import shlex
import subprocess
import tempfile

from .common import ApiError
from .main import dispatch


def terminal(path):
    selector = {"protocol": 1, "session": path}
    while True:
        review = dispatch({**selector, "command": "read"})
        request = {**selector, "generation": review["generation"], "revision": review["revision"]}
        for status in ("unstaged", "staged", "suppressed", "published"):
            print(f"\n{status.title()}")
            for c in review["comments"]:
                if c["status"] == status:
                    print(f"  {c['id']}: {c['title']}" + (" (needs attention)" if c["blockers"] else ""))
        print(f"\nVerdict: {review['verdict']}. Model suggestion: {review['recommendation']}")
        print("s ID stage · u ID unstage · k ID suppress · r ID restore · e ID edit · i ID inspect")
        print("a ID place · v VERDICT · p publish staged · q quit. Full context is available in Magit.")
        try:
            choice = input("> ").strip().split(maxsplit=1)
            if not choice or choice[0] in ("q", "n"):
                return
            key, identity = choice[0], choice[1] if len(choice) > 1 else ""
            if key == "p":
                prepared = dispatch({**request, "command": "prepare"})
                if not prepared["ready"]:
                    print(prepared["reason"])
                    continue
                for c in prepared["review"]["comments"]:
                    if c["status"] == "staged":
                        print(f"\n{c['title']} · {c['placement']}\n{c['body']}")
                for warning in prepared.get("warnings", []):
                    print(warning)
                if input(f"Publish these {prepared['counts']} with {prepared['verdict'] or 'replies only'}? Type yes: ") == "yes":
                    result = dispatch({**request, "revision": prepared["review"]["revision"], "command": "publish",
                                       "confirmation": prepared["confirmation"]})
                    print("Published." if result["complete"] else result["reason"])
                continue
            if key == "v":
                dispatch({**request, "command": "mutate", "operation": "verdict", "verdict": identity.upper()})
                continue
            c = next((c for c in review["comments"] if c["id"] == identity), None)
            if c is None:
                print("Choose a comment ID.")
                continue
            mutation = {**request, "command": "mutate", "comment": identity}
            if key == "i":
                print(c["body"])
                for row in c["context"].get("rows", []):
                    print(f"{row['old'] or '':>5} {row['new'] or '':>5} {'>' if row['highlight'] else ' '} {row['kind']}{row['text']}")
                for entry in c["context"].get("thread", {}).get("comments", []):
                    print(f"\n@{entry['author']}: {entry['body']}")
                print("\n".join(c["blockers"]))
                continue
            if key == "e":
                with tempfile.NamedTemporaryFile(mode="w+", suffix=".md") as file:
                    file.write(c["body"])
                    file.flush()
                    subprocess.run([*shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"), file.name], check=True)
                    file.seek(0)
                    mutation.update(operation="edit", body=file.read())
            elif key == "a":
                place = input("Placement: 'overall', 'auto', 'thread KEY', or 'PATH SIDE START END': ").split()
                if place == ["auto"]:
                    mutation.update(operation="automatic")
                else:
                    if place == ["overall"]:
                        target = {"kind": "review"}
                    elif len(place) == 2 and place[0] == "thread":
                        target = {"kind": "reply", "key": place[1]}
                    elif len(place) == 4:
                        target = {"kind": "inline", "path": place[0], "side": place[1].upper(), "start_line": int(place[2]), "line": int(place[3])}
                    else:
                        print("Invalid placement.")
                        continue
                    mutation.update(operation="placement", placement=target)
            elif key in ("s", "u", "k", "r"):
                mutation["operation"] = {"s": "stage", "u": "unstage", "k": "suppress", "r": "restore"}[key]
            else:
                print("Unknown command.")
                continue
            result = dispatch(mutation)
            for skipped in result.get("skipped", []):
                print("; ".join(skipped["reasons"]))
        except EOFError:
            return
        except (ApiError, ValueError, subprocess.CalledProcessError) as exc:
            print(str(exc))
