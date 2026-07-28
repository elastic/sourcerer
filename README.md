# Sourcerer

**Ask the source.** Sourcerer answers questions about your code so you can
understand it from first principles.

---

1. [Quickstart](#quickstart)
3. [Configuration](#configuration)
3. [Upgrades](#upgrades)
4. [Benchmarks](#benchmarks)
5. [Development](#development)

---

### Why Sourcerer

Sourcerer indexes snapshots of git repos so that AI agents can search the code,
generate authoritative answers, and provide inline citations for verification.
It ships with configurations for [Elastic Agent Builder](https://www.elastic.co/docs/explore-analyze/ai-features/elastic-agent-builder), which searches your code
in Elasticsearch the way a coding agent would search it on a filesystem.
Its value shines for questions that span multiple repos or versions: Sourcerer
will find the right snapshot and **grep a billion lines of code in <3 seconds.**

**Code is the primary source of truth for its own behavior.** Always authoriative,
never outdated. While documentation and tribal knowledge offers context, they
can never be the primary source of truth for its implementation.

**Go with the grain on how model are trained.** LLMs used by coding agents are
trained to use terminal commands, and [grep has worked exceptionally well](https://arxiv.org/abs/2605.15184).
Sourcerer searches code with the same semantics (e.g. grep, ls, cat, head, tail)
over multiple code repositories indexed in Elasticsearch for greater speed,
scale, relevance, security, collaboration, and customization.

**Search like a coding agent at scale.** Sourcerer is statistically tied with frontier coding agents for code retrieval based on its results from [SWE-Explore-Bench](https://github.com/Qiushao-E/SWE-Explore-Bench) (see chart below). You can expect Sourcerer to search your code as well as any coding agent, while scaling across many historical repository snapshots as if it was a single repo.

![Sourcerer: SWE-Explore-Bench Results](https://storage.googleapis.com/sourcerer-public/sourcerer-swe-explore-bench-results.png)

## Quickstart

Make sure you have [uv](https://docs.astral.sh/uv/) and [git](https://git-scm.com/downloads/) on your machine, and [Elasticsearch and Kibana](https://www.elastic.co/cloud/serverless) running somewhere.

1. Install the `sourcerer` CLI:
   ```sh
   uv tool install "git+https://github.com/elastic/sourcerer.git@v2.0.0"
   ```
2. Add connection details. Create a `.env` in your working directory, then fill it in:
   ```sh
   cat > .env <<'EOF'
   ELASTICSEARCH_URL=
   KIBANA_URL=

   # Authentication (use either API Key or both of Username and Password)
   ELASTICSEARCH_API_KEY=
   #ELASTICSEARCH_USERNAME=
   #ELASTICSEARCH_PASSWORD=
   EOF
   ```
3. Define the sources to index. Create `sourcerer.yml`, then edit it according to the [`sourcerer.yml` specification](specs/sourcerer-yml.md):
   ```sh
   cat > sourcerer.yml <<'EOF'
   sources:
   # Example: Index and retain only the latest snapshot of the "main" branch
   - git: { host: "github", org: "elastic", repo: "docs-content", ref_type: "branch" }
     match: "main"
     retain.count: 1

   # Example: Index and retain only the latest patch release snapshot of every minor release for the last two major releases
   - git: { host: "github", org: "elastic", repo: "elasticsearch", ref_type: "tag" }
     match: "v{major}.{minor}.{patch}"
     since.ref: "v8.17.0"
     retain.version: { majors: 2, patches: 1 }
   EOF
   ```
   Each source is scoped to a `git.host`, `git.org`, `git.repo`, and `git.ref_type` (can be `tag`, `branch`, or `commit`). `match` defines the names of tags, branches, or commit hashes to index based on a pattern like `v{major}.{minor}.{patch}` or a fixed string like `main`. `since` defines where to begin indexing in the commit history. `retain` defines how long to keep indexed refs before they qualify for pruning.
4. Set up the indices and agent: `sourcerer setup --config sourcerer.yml`
5. Index the repos: `sourcerer index --config sourcerer.yml`
6. Chat about your software with the Sourcerer agent in Kibana under "Agents".

## Configuration

The [`sourcerer.yml` specification](specs/sourcerer-yml.md) has the full reference of fields supported by the configuration file.

### Cloning with SSH

By default, `sourcerer` clones repos using HTTPS. You can override this in `sourcerer.yml` by setting the `urls.clone` of a Git host to an SSH-compatible URL template.

Example `sourcerer.yml` override for GitHub:

```yaml
hosts:
- id: github
  urls.clone: "git@github.com:{git.org}/{git.repo}.git"
```

Example `sourcerer.yml` overrides for built-in Git hosts:

```yaml
hosts:

# Bitbucket
- id: bitbucket
  urls.clone: "git@bitbucket.org:{git.org}/{git.repo}.git"

# Codeberg
- id: codeberg
  urls.clone: "git@codeberg.org:{git.org}/{git.repo}.git"

# Forgejo
- id: forgejo
  urls.clone: "git@code.forgejo.org:{git.org}/{git.repo}.git"

# GCP Cloud Source
# Note: SSH identity is your GCP account name. Add it to ~/.ssh/config:
#     Host source.developers.google.com
#     User you@example.com
- id: gcp-cloud-source
  urls.clone: "ssh://source.developers.google.com:2022/p/{git.org}/r/{git.repo}"

# Gitea
- id: gitea
  urls.clone: "git@gitea.com:{git.org}/{git.repo}.git"

# GitHub
- id: github
  urls.clone: "git@github.com:{git.org}/{git.repo}.git"

# GitLab
- id: gitlab
  urls.clone: "git@gitlab.com:{git.org}/{git.repo}.git"

# Launchpad
# Note: SSH identity is your Launchpad account name. Add it to ~/.ssh/config:
#     Host git.launchpad.net
#     User <your-launchpad-username>
- id: launchpad
  urls.clone: "git+ssh://git.launchpad.net/{git.repo}"

# SourceHut
- id: sourcehut
  urls.clone: "git@git.sr.ht:~{git.org}/{git.repo}"
```

### AWS CodeCommit, Azure DevOps, and Google SSM

AWS CodeCommit, Azure DevOps, and Google Secure Source Manager (SSM) require
non-standard fields beyond `git.org` and `git.repo` to properly namespace code.
Therefore you must define them by hand in `sourcerer.yml` under `hosts`.

Example `sourcerer.yml` for AWS CodeCommit, Azure DevOps, and GCP Secure Source Manager using HTTPS URLs for clones:

```yaml
hosts:

# AWS CodeCommit
# Note: Replace REGION by hand.
- id: aws-codecommit-REGION
  urls:
   clone: "https://git-codecommit.REGION.amazonaws.com/v1/repos/{git.repo}"
   directory: "https://console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.directory}"
   file: "https://console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.path}"
   line: "https://console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.path}?lines={line.number}"
   line_range: "https://console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.path}?lines={line.number_start}-{line.number_end}"

# Azure DevOps
# Note: Replace PROJECT by hand.
# Note: Untested
- id: azure-devops-PROJECT
  urls:
   clone: "https://dev.azure.com/{git.org}/PROJECT/_git/{git.repo}"
   directory: "https://dev.azure.com/{git.org}/PROJECT/_git/{git.repo}?path=/{file.directory}&version=GC{git.commit}"
   file: "https://dev.azure.com/{git.org}/PROJECT/_git/{git.repo}?path=/{file.path}&version=GC{git.commit}"
   line: "https://dev.azure.com/{git.org}/PROJECT/_git/{git.repo}?path=/{file.path}&version=GC{git.commit}&line={line.number}&lineEnd={line.number_plus_one}&lineStartColumn=1&lineEndColumn=1&lineStyle=plain"
   line_range: "https://dev.azure.com/{git.org}/PROJECT/_git/{git.repo}?path=/{file.path}&version=GC{git.commit}&line={line.number_start}&lineEnd={line.number_end_plus_one}&lineStartColumn=1&lineEndColumn=1&lineStyle=plain"
   
# GCP Secure Source Manager
# Note: Replace INSTANCE_ID, PROJECT_NUMBER, and LOCATION by hand in all places.
# Note: Untested
- id: gcp-ssm-INSTANCE_ID-PROJECT_NUMBER
  urls:
   clone: "https://INSTANCE_ID-PROJECT_NUMBER-git.sourcemanager.dev/{git.org}/{git.repo}.git"
   directory: "https://INSTANCE_ID-PROJECT_NUMBER.LOCATION.sourcemanager.dev/{git.org}/{git.repo}/src/commit/{git.commit}/{file.directory}"
   file: "https://INSTANCE_ID-PROJECT_NUMBER.LOCATION.sourcemanager.dev/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}"
   line: "https://INSTANCE_ID-PROJECT_NUMBER.LOCATION.sourcemanager.dev/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number}"
   line_range: "https://INSTANCE_ID-PROJECT_NUMBER.LOCATION.sourcemanager.dev/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number_start}-L{line.number_end}"
```

Use these values for `urls.clone` if you prefer to use SSH for clones:

* AWS CodeCommit: `"ssh://git-codecommit.REGION.amazonaws.com/v1/repos/{git.repo}"`
* Azure DevOps: `"git@ssh.dev.azure.com:v3/{git.org}/PROJECT/{git.repo}"`
* Google SSM: `"ssh://INSTANCE_ID-PROJECT_NUMBER-git.sourcemanager.dev/{git.org}/{git.repo}.git"`


### Git authentication

`sourcerer` must be able to run these `git` commands, which requires its shell environment to have persistent, non-interactive authentication configured for your remote Git hosts:

* [`git clone`](https://git-scm.com/docs/git-clone)
* [`git fetch`](https://git-scm.com/docs/git-fetch)
* [`git ls-remote`](https://git-scm.com/docs/git-ls-remote)

#### Authentication guides by Git host

|Git host|Authentication guide|
|--------|--------------------|
|AWS CodeCommit|[ssh](https://docs.aws.amazon.com/codecommit/latest/userguide/setting-up-ssh-unixes.html), [https](https://docs.aws.amazon.com/console/codecommit/connect-gc-np), [git-remote-codecommit](https://docs.aws.amazon.com/codecommit/latest/userguide/setting-up-git-remote-codecommit.html)|
|Azure DevOps|[ssh](https://learn.microsoft.com/en-us/azure/devops/repos/git/use-ssh-keys-to-authenticate?view=azure-devops), [https](https://learn.microsoft.com/en-us/azure/devops/repos/git/set-up-credential-managers?view=azure-devops)|
|Bitbucket|[ssh](https://support.atlassian.com/bitbucket-cloud/docs/set-up-an-ssh-key/), [https](https://support.atlassian.com/bitbucket-cloud/docs/create-an-api-token/)|
|Codeberg|[ssh](https://docs.codeberg.org/security/ssh-key/), [https](https://docs.codeberg.org/advanced/access-token/)|
|GCP Cloud Source|[ssh](https://docs.cloud.google.com/source-repositories/docs/authentication), [https](https://docs.cloud.google.com/source-repositories/docs/authentication)|
|GCP Secure Source Manager|[ssh](https://cloud.google.com/secure-source-manager/docs/ssh-keys), [https](https://cloud.google.com/secure-source-manager/docs/use-git)|
|GitHub|[ssh](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent), [https](https://docs.github.com/en/get-started/git-basics/caching-your-github-credentials-in-git)|
|GitLab|[ssh](https://docs.gitlab.com/ee/user/ssh.html), [https](https://docs.gitlab.com/user/profile/personal_access_tokens/)|
|Gitea|[ssh](https://docs.gitea.com/help/faq), [https](https://docs.gitea.com/development/api-usage)|
|Forgejo|[ssh](https://docs.codeberg.org/security/ssh-key/), [https](https://docs.codeberg.org/advanced/access-token/)|
|Launchpad|[ssh](https://documentation.ubuntu.com/launchpad/user/how-to/import-ssh-keys/), [https](https://documentation.ubuntu.com/launchpad/user/explanation/working-with-code/git-hosting/)|
|SourceHut|[ssh](https://man.sr.ht/tutorials/set-up-account-and-git.md)|

## Upgrades

To upgrade, reinstall from the desired release tag, replacing `v2.0.0` with the release you want:

```sh
uv tool install --reinstall "git+https://github.com/elastic/sourcerer.git@v2.0.0"
```

Git tag installations remain pinned to that release. `uv tool upgrade sourcerer` does not automatically discover a newer GitHub tag.

## Benchmarks

Sourcerer includes a benchmark harness that measure how well the agent locates the right
source code for a task. The current benchmark, `swe_explore_bench`
([SWE-Explore-Bench](https://github.com/Qiushao-E/SWE-Explore-Bench)), scores
whether, given an issue, the agent cites the correct file regions.

List what's available:

```sh
sourcerer benchmark list
```

Run a benchmark in three steps (from a directory with a `.env`, as in the
Quickstart):

1. Download and build the dataset into `./benchmarks/<name>/` (needs `git`, `uv`,
   and network access to GitHub + HuggingFace):
   ```sh
   sourcerer benchmark get swe_explore_bench
   ```
2. Index the benchmark's base commits into Elasticsearch (uses the benchmark's
   packaged `repos.yml`):
   ```sh
   sourcerer benchmark index swe_explore_bench
   ```
3. Run the eval against the Sourcerer agent (needs `KIBANA_URL` and
   `ELASTICSEARCH_API_KEY`):
   ```sh
   sourcerer benchmark run swe_explore_bench
   ```

Useful `run` options: `-k/--top-k` (comma-separated cutoffs, e.g. `-k 5,10,20`;
default `5`), `-j/--concurrency` (instances explored in parallel; default `1`),
`--connector-id` (pick the Agent Builder LLM connector), and `--resume` (skip
instances already completed). `run` will download the dataset automatically if
step 1 was skipped.

### Results and traces

Each run writes to a timestamped directory:

```
./benchmarks/<name>/results/sourcerer-<YYYYMMDDHHMMSS>/
├── top5.jsonl      # one file per --top-k value: per-instance regions + metrics
└── traces.jsonl    # full request/response trace for every instance
```

Aggregate metric averages for each `top_k` also print to the console when the run
finishes.


## Development

To run the CLI from a local checkout without installing it globally, use `uv run` from
the repo root. uv reads `pyproject.toml`, provisions a matching Python, and syncs the
dependencies into an isolated `./.venv` (gitignored) on first run:

```sh
uv run sourcerer help
uv run sourcerer setup
uv run sourcerer index elastic/elasticsearch -b main
uv run sourcerer index --config sourcerer.yml
```

The project is installed in editable mode, so edits under `src/` take effect immediately -
no reinstall needed. Because the environment is isolated in `./.venv`, this never conflicts
with a globally installed `sourcerer` (e.g. from `uv tool install`).

Equivalently, you can invoke the module directly: `uv run python -m sourcerer.cli <command>`.

### Tests

```sh
uv run pytest tests/
```

### Releases

The version in `pyproject.toml` is the source of truth. Prepare a release through
a normal pull request:

1. Run `uv version <major>.<minor>.<patch>` (without the `v` prefix).
2. Review and commit the resulting `pyproject.toml` and `uv.lock` changes.
3. Merge the pull request to `main` after its tests pass.

From an up-to-date `main` with no tracked changes, publish the corresponding tag:

```sh
./scripts/release.sh v2.0.0
```

The script requires a strict `vMAJOR.MINOR.PATCH` tag, verifies that it matches
`pyproject.toml`, confirms that `main` matches `origin/main` and that the tag is
new, checks the lockfile, runs the tests, and builds the package. After
confirmation, it creates and pushes an annotated tag. The tag-triggered GitHub
Actions workflow repeats the checks and creates the GitHub release only if they
pass.
