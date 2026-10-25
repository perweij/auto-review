# GitHub review process overview

Research date: 2026-10-25.

This document records GitHub's review workflow and provides a checklist for a
future coverage audit of pr-review. It distinguishes GitHub behavior from proposed
program design. It does not establish that the implementation covers these
operations or commit the project to implementing every GitHub feature.

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

## Reviewer workflow coverage checklist

GitHub provides mechanisms; the reviewer's substantive responsibility is to assess
the change, communicate actionable feedback, and revisit that assessment after
revisions. Use this inventory for the program audit:

| Area | Operations or behavior to cover |
| --- | --- |
| Understand the change | Read the PR description, related discussion, changed files, and relevant surrounding code. |
| Inspect and verify | Investigate correctness, tests, security, and dependency changes; track files viewed where useful. |
| Leave feedback | General comments; file, single-line, and multiline comments; exact code suggestions. |
| Prepare a review | Publish an individual comment or accumulate private pending comments; edit/discard drafts; submit the review. |
| Give a verdict | Comment, approve, or request changes, with a clear summary. |
| Continue discussions | Read complete threads, reply in the appropriate conversation, and correct earlier feedback. |
| Track completion | Distinguish unresolved, resolved, and outdated threads; resolve or reopen when permitted. |
| Review revisions | Inspect new changes and verify earlier concerns; submit an updated verdict when appropriate. |
| Handle review lifecycle | Understand pending, submitted, and dismissed reviews, review requests, and requests for another review. |
| Respect merge policy | Account for required approvals, code owners, conversation resolution, stale approvals, and approval of the latest push. |

Sources: [review guide](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/reviewing-proposed-changes-in-a-pull-request),
[review quickstart](https://docs.github.com/en/pull-requests/get-started/reviewing-pull-requests-quickstart),
[review API](https://docs.github.com/en/rest/pulls/reviews),
[thread reopening API](https://docs.github.com/en/graphql/reference/pulls#unresolvereviewthread),
and [branch protection settings](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).

## Proposed program model and audit approach

Keep these dimensions separate:

- **Finding assessment:** does the problem still exist?
- **Conversation state:** is the thread resolved, and is its code context outdated?
- **Review state:** what verdict has the reviewer submitted, and against which commit?
- **Merge requirements:** what does GitHub currently require?

This separation is a design recommendation derived from the documented behavior.
GitHub's resolution flag records workflow state; it does not independently prove
that a finding is fixed. Likewise, a review verdict is not a complete statement of
merge eligibility.

At the time of this research, the [project README](../README.md) explicitly says
that outdated comments are not automatically resolved and that the tool never
resolves or reopens threads. These are documented starting points, not verified
implementation coverage.

The next audit should classify each capability as **implemented**, **partial**,
**missing**, or **deliberately delegated to GitHub/Forge**, citing implementation
and relevant verification evidence. Decide which parts of the reviewer workflow
the program should own before treating every unimplemented operation as a gap.

Useful regression scenarios for that audit include:

- A thread becomes outdated while remaining unresolved.
- All threads are resolved while a Request changes verdict remains active.
- An approving review coexists with an unresolved conversation.
- A new push dismisses an approval or requires fresh approval under repository rules.
- A previously resolved concern recurs and needs follow-up or reopening.
- A reviewer lacks permission to resolve, reopen, or dismiss the relevant item.

No implementation audit or refactoring was performed as part of this research.
