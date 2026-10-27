# Running prbot as a GitHub App

A GitHub App is the recommended way to run prbot against real repositories. Instead of a personal
access token that can reach everything you can, the App is installed on specific repositories and
each run gets a **one-hour installation token scoped to one repo, with only the permissions that run needs**.

## How the auth works

1. prbot signs a JWT with the App's private key (`Auth.AppAuth` in PyGithub, valid ~10 minutes).
2. It looks up the App's installation on `--repo` (or uses `GITHUB_APP_INSTALLATION_ID`).
3. It exchanges the JWT for an installation token, **down-scoping** permissions per run:

| Run | Token permissions |
|---|---|
| review (default, `--post-review`) | `contents: read`, `pull_requests: write`, `metadata: read` |
| `--open-fix-pr` | the above, but `contents: write` (pushing the `prbot/fix-pr-N` branch needs it) |

The token refreshes automatically 5 minutes before expiry. If the installation hasn't been granted a
permission the run asks for, prbot stops with a clear error instead of half-running.

## Create the App (about 3 minutes)

1. GitHub → Settings → Developer settings → GitHub Apps → **New GitHub App**.
2. Name: e.g. `prbot-<you>`. Homepage: your repo URL. **Uncheck Webhook → Active** (not needed for the CLI).
3. Repository permissions:
   - **Pull requests: Read and write**
   - **Contents: Read-only** (choose **Read and write** only if you want `--open-fix-pr`)
   - Metadata: Read-only (automatic)
4. "Where can this GitHub App be installed?" → **Only on this account**. Create.
5. Note the **App ID**. Under *Private keys*, **Generate a private key** and save the `.pem` outside the repo.
6. **Install App** → choose *Only select repositories* → pick the repo(s) prbot may touch.

## Configure

```bash
PRBOT_GITHUB_AUTH=app
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY_PATH=~/.secrets/prbot.private-key.pem
# optional, otherwise discovered from --repo:
# GITHUB_APP_INSTALLATION_ID=7890123
```

```bash
python -m prbot review --repo owner/name --pr 1 --post-review
```

Reviews and comments then appear as `prbot-<you>[bot]`, not as your user.
