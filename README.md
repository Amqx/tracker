# Tracker

Watches a list of GitHub repositories and posts new commits to a Discord webhook.
A GitHub Action runs it on a schedule; the last seen commit per repository lives in
`state.json`, which the workflow commits back to this repo so each run only reports
what is new.

Secrets the workflow expects: `DISCORD_WEBHOOK_URL` (required) and `GH_PAT`
(optional -- needed to watch private repositories, and to push `state.json` back
with something other than `GITHUB_TOKEN`).

## Setup

Clone this repository, then create a branch called `personal`. Then modify `repos.yml`
as below and enable GitHub Actions.

## Configuring repositories

```yaml
defaults:
  webhook_env: DISCORD_WEBHOOK_URL   # env var holding the webhook URL
  username: commit tracker           # override the webhook's display name
  # avatar_url: https://...
  notify_on_first_run: false         # true = announce history on the first run

repositories:
  - astral-sh/uv                     # default branch
  - python/cpython@main              # explicit branch

  - repo: your-org/your-service
    branch: main
    label: your-service              # title shown in the embed
    webhook_env: DISCORD_WEBHOOK_DEPLOYS   # send this repo to another channel
    notify_on_first_run: true
```

A per-repo `webhook_env` also needs its secret mapped into the workflow's `env:`
block -- the script reads environment variables, and Actions only exposes the ones
you map.
