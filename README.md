# django-deploy

Reusable GitHub Actions deployment for independently versioned applications on
AWS Lightsail: **Semantic Release → Docker → Amazon ECR → SSH → Docker Compose**.

This repository contains deployment automation only. No application has been
onboarded. Applications keep their own Dockerfiles, Compose files, database
volumes, release configuration, and GitHub secrets. There are no production
credentials or server addresses in this repository.

## Workflow

1. A calling application receives a push to `main` or a manual run on `main`.
2. Semantic Release reads that application's history and `.releaserc.json`, then
   creates its version tag and GitHub release.
3. When a release is published, CI builds the exact released commit using the
   application's root `Dockerfile`. ECR receives tags `1.2.3` and `sha-<full-sha>`.
4. The deploy job enters the application's `production` GitHub environment,
   transfers selected files over verified SSH, installs `.env` with mode `600`,
   pulls images, and runs `docker compose up --detach --no-build --wait`.
5. Services with health checks must become healthy; services without them must
   be running. Add an application HTTP health check in Compose to verify HTTP
   readiness. This does not independently test the public domain, proxy or TLS.

The entire pipeline is serialized per application repository and environment.
Running deployments are not cancelled. GitHub concurrency may replace an older
pending run with a newer pending run; not every pushed commit necessarily gets
its own deployment. Do not reuse the internal `django-deploy-*` concurrency group
in the caller, because doing so can block the called workflow.

Semantic Release publishes **before** the build and deployment. A GitHub release
is not evidence of successful production deployment. If build/deploy fails, use
GitHub Actions **Re-run failed jobs**, retaining the successful release outputs.
Starting a new manual run or re-running all jobs may find no new release and skip
deployment. Manual dispatch does not force a release or provide rollback.

## Onboard an application later

1. Copy [templates/.releaserc.json](templates/.releaserc.json) to the application
   root, or keep the application's existing compatible configuration. The
   template follows Flight-AI: breaking changes bump major, `feat` bumps minor,
   and other included commits bump patch. Versions belong to each application;
   `django-deploy` versions are independent. Existing tags must match the normal
   `v1.2.3` format; review an existing project's tag history before switching
   versioning systems. The workflow currently supports `main` only.
2. Copy [templates/caller.yml](templates/caller.yml) to the application's
   `.github/workflows/deploy.yml`. Set a dedicated absolute `deploy-directory`
   and list the files to transfer. Do not run an old production workflow alongside
   the new caller. A caller can reference an immutable release tag (as shown) or
   a full commit SHA; new shared versions are adopted explicitly.
3. Configure the secrets below **in the application repository**, not here.
4. Prepare the server and application Compose contract below, then push a release
   commit. Test with a nonproduction app/server before the first production use.

The caller must grant `contents: write`, `issues: write`, and
`pull-requests: write` for Semantic Release. Only its release job receives these
permissions; the build/deploy jobs have read access to repository contents.
The workflow rejects PR and non-`main` release/deployment invocations.

### Secrets

| Secret | Where | Value |
| --- | --- | --- |
| `AWS_ACCESS_KEY_ID` | Repository | AWS key permitted to publish to this app's ECR repository |
| `AWS_SECRET_ACCESS_KEY` | Repository | Matching AWS secret |
| `AWS_REGION` | Repository | ECR region, such as `us-west-1` |
| `ECR_REGISTRY` | Repository | Registry hostname, without a protocol or trailing slash |
| `ECR_REPOSITORY` | Repository | Existing ECR repository name |
| `SERVER_HOST` | `production` environment | SSH hostname or IPv4 address |
| `SERVER_USER` | `production` environment | SSH user, such as `ubuntu` |
| `SERVER_SSH_KEY` | `production` environment | Unencrypted private key for this deployment account |
| `SERVER_KNOWN_HOSTS` | `production` environment | Verified OpenSSH known_hosts entry for the server |
| `APP_ENV` | `production` environment | Full multiline production dotenv contents |

Build credentials and ECR location must be repository secrets because the build
job does not enter `production`. Avoid conflicting overrides of these values in
the environment. GitHub provides `GITHUB_TOKEN` automatically; do not create a
personal token for releases. Temporary AWS session credentials and OIDC are not
part of this initial interface.

The caller explicitly forwards each secret, including environment secrets; the
reusable deployment job selects the caller's GitHub environment. Existing names
such as `LIGHTSAIL_HOST` can be mapped to `SERVER_HOST` in the caller without
renaming the stored secret. Set up the environment and any protection rules
before first use. Protection features depend on the application's GitHub plan
and visibility.

Obtain the SSH host key through a trusted channel, for example the Lightsail
browser console, and verify its fingerprint. `ssh-keyscan` alone does not verify
server identity. For a nondefault port, known_hosts must identify `[host]:port`.
IPv6 SSH destinations are not currently supported by the input validation.

`APP_ENV` uses Docker Compose dotenv syntax. Preserve quoting and escape literal
dollar signs as required by Compose. It is written as data, never sourced as a
shell script. Do not put `IMAGE` in `APP_ENV`; CI supplies the released image.
Use `.dockerignore` to exclude `.env`, databases, keys, and other local data from
the image build context. Runtime secrets do not belong in `build-args`.

### Inputs and outputs

| Input | Default | Meaning |
| --- | --- | --- |
| `deploy-directory` | Required | Dedicated absolute server path, e.g. `/home/ubuntu/flight-ai` |
| `compose-files` | `docker-compose.yml` | Newline-separated relative paths in merge order |
| `deployment-files` | `docker-compose.yml` | Newline-separated relative files/directories to copy |
| `build-args` | Empty | Newline-separated build arguments, e.g. `BUILD_ENV=production` |
| `environment` | `production` | GitHub environment in the calling application |
| `ssh-port` | `22` | Server SSH port |
| `wait-timeout` | `180` | Readiness timeout in seconds, between 1 and 600 |

Every Compose file must be included in `deployment-files`. Paths cannot contain
spaces, hidden components, symlinks, or `..`. Directory inputs recursively copy
their contents; list only public deployment configuration, never secret material.
Files no longer included are not deleted from the server. Keep deployment
configuration paths distinct from persistent application data.

Workflow outputs are `version` and `published`. They describe release creation,
not deployment success. The deployment job's result indicates readiness success.

### Application and server contract

- Linux server with Bash, GNU coreutils, tar, Docker Engine, and a current Docker
  Compose v2 plugin supporting `up --wait --wait-timeout`. The SSH login shell
  must accept POSIX shell commands. The account must run Docker without `sudo`
  and write the deployment directory. Host AWS CLI/credentials are unnecessary:
  CI transfers an expiring ECR password, uses a temporary Docker config, and
  removes those temporary credentials when the SSH command exits.
- An existing ECR repository and AWS IAM permissions for authorization, layer
  upload/download, image publishing, and pulling. Builds use the runner's native
  Linux amd64 architecture; an ARM server needs future platform support.
- An application-root Dockerfile that builds without runtime secrets. Its
  Compose application services must use `image: ${IMAGE:?IMAGE must be set}`.
  All services sharing this application's image use that same value. Other
  services can use published images, such as PostgreSQL. Every service must be
  pullable; deployment never builds on the server.
- Application-defined migrations and static collection: either entrypoint logic
  or one-shot Compose services with `service_completed_successfully`
  dependencies. The shared workflow deliberately runs no Django commands.
- Application-defined health checks, restart policies, data volumes, log handling,
  and backup/restore procedures. Bind-mounted data directories and permissions
  must already be prepared. Do not package databases in `deployment-files`.
- Existing DNS, reverse proxy, HTTPS, firewall rules, and host maintenance. This
  project does not provision Lightsail or change server networking.

Updating Compose files and environment configuration is not an atomic release
switch. A failure may leave updated files, partial service updates, or completed
database migrations. There is no automatic rollback or zero-downtime guarantee.
The workflow does not prune images or volumes; host maintenance must control
disk usage while retaining whatever image history is needed for recovery.

## Maintain this repository

The reusable workflow is self-contained so callers execute exactly their selected
workflow version. Third-party actions and Semantic Release are pinned. No
separate checkout of this repository or runtime script download is needed.

Local checks:

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
actionlint .github/workflows/*.yml
```

This repository uses uv, `pyproject.toml`, and a committed `uv.lock` for Python
dependencies, with Python selected by `.python-version`. CI installs the same
locked development dependencies. Use `uv add --dev` to add tooling and commit
both manifest and lockfile. Semantic Release itself is a Node-based GitHub
Action; application images continue to use their own Dockerfiles and dependency
managers until those applications are onboarded.

CI runs workflow linting and credential-free tests of bundle generation, secret
handling, path validation, and the remote Compose invocation. Tests use temporary
directories and fake AWS/Docker/SSH commands; they do not build an application
image, create a release in a consumer, access ECR, or deploy to a real server.

Publish shared changes with a new immutable tag and GitHub release, for example
`v1.0.1`. The caller template is pinned to `v1.0.0`; applications decide when to
upgrade. This repository's CI validates the automation but does not call its own
production deployment workflow.

References: [GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows),
[Semantic Release](https://github.com/cycjimmy/semantic-release-action),
[Docker Compose readiness](https://docs.docker.com/reference/cli/docker/compose/up/).
