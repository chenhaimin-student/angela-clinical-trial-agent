# GitHub Publishing Checklist

This checklist is intentionally not automated by the repository.

1. Confirm the worktree is clean and the branch is `main`.
2. Confirm the repository has exactly the intended public commit history.
3. Confirm `git remote -v` is empty before choosing a destination.
4. Run all test groups and record platform limitations honestly.
5. Run a full secret scan, file-type scan, large-file scan, and Git-history scan.
6. Verify that no database, raw page, log, browser profile, runtime state, credential, internal address, or private report is tracked.
7. Review `LICENSE` and `THIRD_PARTY_NOTICES.md`.
8. Create the GitHub repository only after the owner explicitly approves publication.
9. Add the selected personal GitHub remote and push `main` only after that approval.

Do not publish `00-private-source` or any directory containing old production history.
