# Publish a worker to the running workspace

From a worker worktree, run `"$AGENT_LOOP_DEPLOY_COMMAND"` or the primary checkout's
`scripts/merge-deploy`. The same action is available under **Agent settings → Merge &
redeploy** in the browser.

The command returns a deployment ID and a JSON status-file path. Its sibling `.log`
contains validation output. `scripts/merge-deploy --status` shows the latest job.

The coordinator snapshots tracked and nonignored new files without changing the
worker's branch, HEAD or staged content. It validates the combined release in an
isolated worktree, pushes main without force, advances the serving checkout and
relaunches the same workspace. Active chat IDs and the supervisor's running state
are restored. Existing worktrees retain their edits; newly added workers start from
the deployed release.

Tests or merge conflicts stop deployment before publication. An independently
advanced main is never overwritten. Runtime deployment configuration, queued jobs
and logs are retained under `.agent-loop/deploy/` in the primary checkout.
