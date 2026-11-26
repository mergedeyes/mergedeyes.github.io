#!/usr/bin/env bash
# Commits the given paths (if anything changed) and pushes the commit to
# main - used by both bot workflows (update-visitor-count.yml,
# generate-project-pages.yml) instead of stefanzweifel/git-auto-commit-action.
#
# Why not that action: it pushes the commit as-is, so if anything else
# landed on main while the workflow was running, the push is rejected as
# non-fast-forward and the run fails. The shared concurrency group
# (main-branch-writes) keeps the two bot workflows from overlapping, but
# it can't stop a human push - merging two PRs back to back did exactly
# that: the second merge moved main while the first merge's generator run
# was still working, and its push bounced.
#
# So: commit, then rebase onto the current main before pushing, retrying a
# few times in case main moves again in between. -X theirs resolves any
# conflict in favor of the commit being replayed - i.e. this run's freshly
# generated files, which is what should win for these bot-owned paths.
# A human change to the same file only conflicts on the exact lines the
# bot rewrote; everything else in it is kept by the rebase. Anything that
# changed upstream (say index.html) also triggers its own regeneration run,
# queued behind this one by the concurrency group, so nothing stays stale.
#
# Usage: commit_and_push.sh "<commit message>" <path or pathspec>...
# Pathspecs are git's, not the shell's: quote them ('projects/*.html') so
# git expands them - its "*" also matches across "/", so that one covers
# the nested <slug>-source/ pages too, deletions included (git add -A).
set -euo pipefail

message="$1"
shift

# Same identities the action used, so the history keeps looking the same:
# the actor (who triggered the run) as author, the Actions bot as committer.
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
author="${GITHUB_ACTOR} <${GITHUB_ACTOR_ID}+${GITHUB_ACTOR}@users.noreply.github.com>"

git add -A -- "$@"
if git diff --cached --quiet; then
  echo "Nothing changed, nothing to commit."
  exit 0
fi
git commit --quiet --author="$author" -m "$message"

for attempt in 1 2 3 4 5; do
  if git pull --quiet --rebase -X theirs origin main && git push --quiet origin HEAD:main; then
    echo "Pushed: $message (attempt $attempt)"
    exit 0
  fi
  # A failed rebase leaves the repo mid-rebase - abort so the next attempt
  # starts from the plain commit again.
  git rebase --abort 2>/dev/null || true
  echo "Push attempt $attempt failed, retrying..."
  sleep $((attempt * 5))
done

echo "Could not push after 5 attempts." >&2
exit 1
