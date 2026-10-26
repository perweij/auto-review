# GitHub review process overview

Auto-review keeps finding assessments, conversation state, review verdicts, and
merge requirements separate. This guide explains those distinctions and which
operations are handled here or left to GitHub/Forge.

## Comments, conversations, and verdicts

GitHub tracks conversation resolution separately from the reviewer's verdict.
**Request changes is a verdict on the PR, not a stronger type of individual
comment.** A review groups comments together and can carry a summary and a verdict.

| Item | What it represents | How it is dealt with |
| --- | --- | --- |
| General PR comment | Discussion on the Conversation tab | Reply or follow up; it is not a code-review thread with a resolution checkbox. |
| Code-review thread | Discussion attached to a file, line, or range | Reply, resolve, or reopen the conversation. |
| Suggested change | A review comment proposing exact replacement code | Discuss it or apply it as a commit, individually or in a batch. |
| Review verdict | Comment, Approve, or Request changes for the PR | Submit another review when the assessment changes; an authorized person can also dismiss a review. |

Sources: [review API](https://docs.github.com/en/rest/pulls/reviews),
[comments](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/commenting-on-a-pull-request),
[review decisions](https://docs.github.com/en/pull-requests/concepts/giving-reviews),
[suggestions](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/incorporating-feedback-in-your-pull-request),
and [review dismissal](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/dismissing-a-pull-request-review).

## Outdated does not mean resolved

GitHub exposes two separate thread properties:

- `isOutdated`: newer changes have outdated the thread's code context.
- `isResolved`: the conversation has been marked resolved.

A thread can be **outdated and still unresolved**. Changing the commented-on code
can make a comment outdated without fixing the underlying problem. Whether the
concern remains valid requires another assessment. GitHub's conversation navigator
exposes unresolved, resolved, and outdated conversations.

Program implication: do not automatically remove outdated threads from the
outstanding-feedback list or treat their findings as fixed.

Sources: [thread API definition](https://docs.github.com/en/graphql/reference/pulls#pullrequestreviewthread)
and [conversation navigation](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/commenting-on-a-pull-request#discovering-and-navigating-conversations).

## Resolving threads does not clear a change-request verdict

For example:

1. A reviewer submits Request changes with two code comments.
2. The author pushes fixes and resolves both conversations.
3. The reviewer's change-request verdict remains a separate matter.
4. The reviewer inspects the fixes and submits Approve, or an authorized person
   dismisses the blocking review.

Whether a verdict prevents merging depends on repository rules and reviewer
permissions. Required conversation resolution is a separate merge requirement.
An ordinary comment can therefore block merging through an unresolved
conversation, even without a change-request verdict.

GitHub documents conversation resolution for the PR author and people with
repository write access. Who *should* resolve conversations—author or reviewer—is
a team convention. Integrations should respect the API's current permission
fields, including `viewerCanResolve` and `viewerCanUnresolve`.

Sources: [review submission rules](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/reviewing-proposed-changes-in-a-pull-request),
[branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches),
[resolution permissions](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/commenting-on-a-pull-request#resolving-conversations),
and [thread API definition](https://docs.github.com/en/graphql/reference/pulls#pullrequestreviewthread).

## New commits and stale approvals

An outdated thread and a dismissed stale approval are different events.
Repository settings can dismiss previous approvals when the reviewed diff changes,
or require someone other than the latest pusher to approve the latest reviewable
push. Neither substitutes for checking whether individual concerns were addressed.

Authors can request another review after substantial changes. Reviewers should
reassess the revised code and update their verdict as appropriate.

Sources: [branch protection settings](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
and [re-requesting a review](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/incorporating-feedback-in-your-pull-request#re-requesting-a-review).

## Auto-review workflow coverage

The Emacs interface keeps draft publication and conversation follow-up
separate. `C` opens all conversations, `F` checks selected threads against the
latest source, and `x`/`X` explicitly resolve/reopen a thread. The backend exposes
the same capabilities through its versioned API. Models propose assessments;
they do not perform GitHub workflow actions.

| Capability | Coverage and evidence |
| --- | --- |
| Separate assessment, conversation, submitted decision, and next verdict | Implemented in the API state view and Emacs status summary. `tests/test_workflow.py` checks independent verdict and resolution state. |
| Preserve outdated unresolved conversations | Implemented in the Conversations view and assessment input. Backend and Emacs tests cover outdated-but-unresolved threads. |
| Reply, resolve, and reopen | Implemented with explicit actions, current permission checks, fresh-context checks, and read-back after uncertain responses. Resolution is separate from staged publication. |
| Review selected concerns after new commits | Implemented as limited follow-up generations using the saved processing provider and focused verification. `tests/followup.bats` covers latest-head analysis, reuse, failure/resume, provider selection, and returning to full review. |
| Choose the overall verdict | Implemented. New draft generations default to Comment; stronger verdicts require explicit selection. Limited follow-up publication discloses its scope. |
| Preserve staged work during refresh | Implemented through discussion dependencies. Thread replies depend on their selected/referenced conversations; new inline and overall comments conservatively depend on all discussion content. Unrelated resolution metadata does not unstage them. |
| Inspect submitted GitHub review status | Implemented as optional cached metadata, separate from publication receipts and the model recommendation. Missing status remains unavailable. Later Comment activity is shown separately from opinionated review state. |
| New inline comments, multiline ranges, local drafts, and exact publication | Supported by the shared draft/publication workflow. Published comments remain read-only; follow-ups create new drafts. |
| General timeline comments, dedicated file-level comments, suggestion application, and GitHub-native pending-review editing | Delegated to GitHub/Forge. The tool supports an overall review assessment and editable comment text, but does not claim dedicated controls for all these operations. |
| Review requests, dismissals, remote comment editing/deletion, and file-viewed synchronization | Deliberately delegated to GitHub/Forge by Auto-review. |
| Complete merge-policy interpretation and merge administration | Delegated to GitHub. The displayed aggregate review status is not a complete merge-eligibility calculation. |

The workflow tests use mocked GitHub/model responses and real local Git snapshots;
they do not validate a model's semantic accuracy or publish to a live PR. Full
validation uses the backend checks plus Emacs byte compilation and ERT. See
[the API contract](api.md) and [Emacs usage](../emacs/README.md) for operations and
compatibility behavior.
