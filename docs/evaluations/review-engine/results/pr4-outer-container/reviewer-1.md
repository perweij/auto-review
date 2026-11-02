10 findings, most severe first:

1. `lib/pr_review/owner_git.py:152`: The agent-config trust token uses `git ls-tree <tree> -- <path>` and the patch uses `git diff -- <paths>`, both run in `session.repo.cwd`, but those paths are root-relative from `diff --name-only`. From a subdirectory checkout the token ignores file content and the patch is empty.

   Failure scenario: Run `pr-review owner 42 fix` from `repo/sub`, which `local_repository` stores as the session cwd. `ls-tree` then looks up `sub/CLAUDE.md` and returns nothing (reproduced with git), so the token is `digest([("CLAUDE.md", "")])` and the trust prompt shows an empty diff. After one approval, a later push or fix that rewrites `CLAUDE.md` or `.mcp.json` yields the same token, so `trust()` passes and providers run under unreviewed agent configuration.

2. `lib/pr_review/owner_providers.py:147`: `invoke` sets `batch["checkpoint"] = after` before checking the return code and result, so partial edits from a failed fix attempt become the accepted checkpoint without explicit adoption.

   Failure scenario: A fix provider edits files, then exits non-zero or returns an invalid result. The checkpoint now equals the partial tree, so the `snapshot != checkpoint` guard (`worktree_changed`, owner_fixes.py:201) never fires. Resuming with `--skip TASK` (or `? S`) marks the task skipped, but its partial edits stay in the worktree and in `final_tree`. The batch can reach `ready` with code from an abandoned task, which contradicts the documented 'inspect partial changes, then explicitly adopt' rule. A cancelled run does not move the checkpoint, so it behaves differently.

3. `lib/pr_review/owner_fixes.py:211`: `adopt` moves every non-skipped, non-blocked task to `reviewing`, including `queued` tasks that never ran, so their fix step is bypassed.

   Failure scenario: A plan has t1, t2 and t3, and t1 is interrupted while fixing. The user presses `A` to adopt t1's partial edits. t2 and t3 get implementation 'Manually adopted changes' and go straight to review. `run_task` skips the fix branch, and `successful()` accepts a `no_change` outcome with no blocking findings. The tasks become `complete` and the batch `ready` although no fix was attempted for that feedback; otherwise a blocking finding uses their single repair. `blocked` tasks do still get a real fix run later, so the handling is inconsistent.

4. `lib/pr_review/owner_fixes.py:263`: A paused batch whose selected comment was deleted can never resume. Every refresh marks missing items as changed, which sets `discussion_stale`, and the replan path then requires all `batch["items"]` to be non-missing.

   Failure scenario: A reviewer deletes a comment selected in a paused batch. `refresh` (owner_state.py:222-225) re-adds the key to `changed` on every call, so plain resume raises `stale_discussion`. `--replan` reaches `session.items(state, batch["items"])` (or `context()` for the plan) and raises `invalid_target`. The worktree still holds accumulated fixes, so a new batch also fails with `dirty_worktree` until the human commits or discards them by hand.

5. `lib/pr_review/owner_state.py:206`: Change detection uses the content fingerprint, which includes the viewer's own comments, so the owner's own replies count as discussion changes. They stale batches, unstage other drafts and abort multi-reply publication.

   Failure scenario: Two staged replies target the same thread, for example a manual one and a fixed-in one. `publish` posts the first, then the loop's `refresh` changes the thread fingerprint and `blockers` raises `stale_discussion`, leaving the batch half-published. A self-reply also sets `discussion_stale` on any batch containing that thread, forcing a replan that requeues completed tasks (owner_fixes.py:224). If the owner replies before pushing, `associate` skips that thread on the `verified_fingerprints` mismatch (owner_publication.py:68), so no fixed-in reply is created and no error appears. A viewer-excluding `notification` fingerprint already exists but is not used for this.

6. `lib/pr_review/owner_fixes.py:288`: The catch-all handler overwrites any batch status with `paused`, which demotes an already verified `ready` batch when a resume attempt fails for an unrelated reason.

   Failure scenario: A batch is `ready` and the human has committed nothing yet. The user presses `r` (latest batch is the default), and it hits a transient live-auth probe failure, `stale_discussion` from a new comment, `trust_required` or `settings_changed`. The handler sets status `paused`, so `associate` no longer accepts it: explicit calls return `invalid_target`, automatic ones skip it, and no fixed-in replies can be created. The only `ready` protection is the HEAD-mismatch branch at line 189, and recovery requires another paid review that must succeed. The same happens when the last `check_fresh` at line 283 fails right after `final_review` passed.

7. `lib/pr_review/owner_fixes.py:197`: A task left in `fixing` or `repairing` after an ordinary provider failure cannot simply be retried, because resume demands `adopt` or `skip` even when nothing changed.

   Failure scenario: The fix provider hits a rate limit or budget error before editing anything. `invoke` raises `model_failed` and the task stays `fixing`. Pressing `r` always raises `interrupted_task`. `A` sends the unfixed task to review only. `S` abandons it and blocks dependents. `p` also raises, because `replan` does not satisfy the guard, and Emacs has no skip-plus-replan command. A one-off provider error therefore prevents the normal re-run of the task's fix step.

8. `emacs/magit-auto-review-owner.el:92`: `magit-auto-review-owner--selected` treats the root section as a group, and a fresh buffer lands at `point-min`, so action keys there select all feedback in the buffer, including Deferred/History.

   Failure scenario: The user opens an owner buffer and presses `f` with point on the 'PR Owner' heading. All actionable items are sent to `owner-fix` with no confirmation, including resolved, handled and deferred ones, which starts paid model runs that edit source. If any item is `missing`, the request fails with `invalid_target`. `k` or `h` in the same position defers or marks all feedback handled, and `V` fails if any empty verdict is included.

9. `lib/pr_review/owner_git.py:73`: `configure_push` sets `branch.<owner-branch>.pushRemote` even though the local branch name differs from the PR head branch, so Magit's push-to-pushremote targets the wrong remote branch.

   Failure scenario: The local branch is `pr-owner/42-abc` and the PR head is `feature`. Magit `P p` pushes to `origin/pr-owner/42-abc`, creating a stray branch and leaving the PR head unchanged. `associate` then finds the commits are not on the PR head, so no fixed-in reply is created. Plain `git push` with `push.default=simple` is refused because the upstream name differs. The setup never creates the 'explicit owner-branch push mapping' that docs/pr-owner-mode.md describes, such as a push refspec; only `P u` reaches the PR.

10. `lib/pr_review/owner_state.py:101`: The remembered toolkit is keyed by `[common, identity]`, and identity becomes null when no repository is passed and the checkout has several remotes, so the PR list and direct opens use different keys.

    Failure scenario: A checkout has `origin` and `upstream`. The user chooses Owner with `M` in the PR list, which saves under `github.com/org/repo`. `magit-auto-review` from a Forge topic or prefix argument calls `--open-pr` without a repository (magit-auto-review.el:490), so `toolkit` reads the null-identity key. It returns the default `reviewer` and opens the wrong toolkit, despite the saved choice.
