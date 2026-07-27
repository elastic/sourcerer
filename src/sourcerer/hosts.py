# sourcerer/hosts.py
# The git-host registry: built-in defaults for known hosting providers plus the merge logic that
# folds a user's `hosts:` overrides from sourcerer.yml over them. A host's `id` becomes the value
# of git.host in documents, index names, and _id generation, so it is validated to be safe as an
# Elasticsearch index-name segment. Kept dependency-free (no ES, no click) so config parsing,
# the git layer, and setup can all import it.

from __future__ import annotations

# Standard packages
from dataclasses import dataclass

# Characters disallowed in a git.host id. The tilde is our own index-name segment delimiter
# (see sourcerer/indices.py); the rest are the characters Elasticsearch forbids in index names,
# so a host id is always safe to drop into an index name unescaped.
_FORBIDDEN_HOST_CHARS = set('~\\/*?"<>|:')

LINK_KINDS = ("directory", "file", "line", "line_range")


def validate_host_id(host_id: str) -> str:
    """Return host_id if it is a legal git.host value, else raise ValueError. A host id goes into
    _ids, document fields, and index names, so it must not contain uppercase letters, whitespace,
    or any character Elasticsearch (or our `~` delimiter) forbids in an index name."""
    if not isinstance(host_id, str) or not host_id:
        raise ValueError("host id must be a non-empty string")
    bad = sorted({c for c in host_id if c in _FORBIDDEN_HOST_CHARS})
    if bad:
        raise ValueError(f"host id {host_id!r} contains forbidden character(s) {bad}")
    if any(c.isupper() for c in host_id):
        raise ValueError(f"host id {host_id!r} must not contain uppercase characters")
    if any(c.isspace() for c in host_id):
        raise ValueError(f"host id {host_id!r} must not contain whitespace")
    return host_id


@dataclass(frozen=True)
class Host:
    """A resolved git host: its id (git.host value), display name, clone protocol + URL template,
    the four citation link templates, and an auto_skill flag. URL templates use dotted tokens
    naming the ES field a value comes from ({git.org}, {git.repo}, {git.commit}, {file.path},
    {line.number}, ...). The clone URL is substituted app-side by clone_url(); the link templates
    are handed to the agent, which fills their tokens from tool output when it formats a citation.

    auto_skill controls whether `sourcerer setup` automatically generates and pushes a citation
    skill for this host. It is False for built-in placeholder entries whose URLs require a
    user-supplied hosts: override before they are usable (aws-codecommit, azure-devops, gcp-ssm).
    Every custom host defined under hosts: in sourcerer.yml always gets auto_skill=True."""

    id: str
    name: str
    clone_protocol: str
    clone_url_template: str
    links: dict  # kind -> template string, keys are LINK_KINDS
    auto_skill: bool = True

    def clone_url(self, org: str, repo: str) -> str:
        """The concrete clone URL for one org/repo. org is substituted verbatim. Uses plain string
        replacement (not str.format) because the tokens contain a dot (`{git.org}`), which
        str.format would misparse as attribute access."""
        return (
            self.clone_url_template
            .replace("{git.org}", org)
            .replace("{git.repo}", repo)
        )

    def link_template(self, kind: str) -> str:
        """The citation URL template for one of LINK_KINDS."""
        return self.links[kind]


# Built-in defaults for known hosting providers. Keys are host ids (git.host values). Each entry
# mirrors the sourcerer.yml `hosts[i]` shape: name, clone{protocol,url}, links{...}. URL schemes
# are best-effort per provider (GitHub/GitLab use /blob/; Bitbucket uses /src/; the Gitea family
# uses /src/commit/; etc.). Users override any field via sourcerer.yml `hosts:`.
KNOWN_HOSTS: dict[str, dict] = {
    "aws-codecommit": {
        "name": "AWS CodeCommit",
        # CodeCommit is region-scoped: each region is a distinct deployment. Define a
        # per-region hosts entry (e.g. id: aws-codecommit-us-east-1) with the region baked
        # into the clone URL and citation link templates, rather than using this built-in
        # directly. git.org is the bare org/account label; git.repo is the repository name.
        # This built-in entry exists as a shape/documentation placeholder only. Its clone URL
        # uses the region-agnostic endpoint which may work for some credential setups.
        # CodeCommit's browse view supports line highlighting via ?lines= (confirmed via AWS's
        # own gitlens integration config). The range syntax "{start}-{end}" for &lines= was not
        # directly confirmed, so double-check before relying on it.
        "auto_skill": False,
        "clone": {"protocol": "https", "url": "https://git-codecommit.amazonaws.com/v1/repos/{git.repo}"},
        "links": {
            "directory": "https://console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.directory}",
            "file": "https://console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.path}",
            "line": "https://console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.path}?lines={line.number}",
            "line_range": "https://console.aws.amazon.com/codesuite/codecommit/repositories/{git.repo}/browse/{git.commit}/--/{file.path}?lines={line.number_start}-{line.number_end}",
        },
    },
    "azure-devops": {
        "name": "Azure DevOps",
        # Azure DevOps URLs may include a project segment between the org and the repo
        # (dev.azure.com/{org}/{project}/_git/{repo}). If your repos live under a project,
        # define a per-project hosts entry (e.g. id: azure-devops-myproject) with the project
        # baked into the clone URL and citation link templates. For org-level repos (no project
        # segment), this built-in entry works as-is with git.org as the bare Azure org name.
        "auto_skill": False,
        "clone": {"protocol": "https", "url": "https://dev.azure.com/{git.org}/_git/{git.repo}"},
        "links": {
            "directory": "https://dev.azure.com/{git.org}/_git/{git.repo}?path=/{file.directory}&version=GC{git.commit}",
            "file": "https://dev.azure.com/{git.org}/_git/{git.repo}?path=/{file.path}&version=GC{git.commit}",
            # Azure DevOps ignores a bare &line=N and does nothing in the source viewer. The
            # actual "Copy permalink" behavior requires line/lineEnd/lineStartColumn/lineEndColumn
            # /lineStyle together, with lineEnd set to ONE LINE PAST the target (confirmed:
            # https://github.com/dotnet/docfx/issues/10447). That means the caller must supply an
            # already-incremented value. {line.number_plus_one} and {line.number_end_plus_one}
            # below are NOT standard fields elsewhere in this table; this host is the one place
            # they're needed, so the templating code must compute them specifically for this key.
            "line": "https://dev.azure.com/{git.org}/_git/{git.repo}?path=/{file.path}&version=GC{git.commit}&line={line.number}&lineEnd={line.number_plus_one}&lineStartColumn=1&lineEndColumn=1&lineStyle=plain",
            "line_range": "https://dev.azure.com/{git.org}/_git/{git.repo}?path=/{file.path}&version=GC{git.commit}&line={line.number_start}&lineEnd={line.number_end_plus_one}&lineStartColumn=1&lineEndColumn=1&lineStyle=plain",
        },
    },
    "bitbucket": {
        "name": "Bitbucket",
        "clone": {"protocol": "https", "url": "https://bitbucket.org/{git.org}/{git.repo}.git"},
        "links": {
            "directory": "https://bitbucket.org/{git.org}/{git.repo}/src/{git.commit}/{file.directory}",
            "file": "https://bitbucket.org/{git.org}/{git.repo}/src/{git.commit}/{file.path}",
            "line": "https://bitbucket.org/{git.org}/{git.repo}/src/{git.commit}/{file.path}#lines-{line.number}",
            "line_range": "https://bitbucket.org/{git.org}/{git.repo}/src/{git.commit}/{file.path}#lines-{line.number_start}:{line.number_end}",
        },
    },
    "codeberg": {
        "name": "Codeberg",
        "clone": {"protocol": "https", "url": "https://codeberg.org/{git.org}/{git.repo}.git"},
        "links": {
            "directory": "https://codeberg.org/{git.org}/{git.repo}/src/commit/{git.commit}/{file.directory}",
            "file": "https://codeberg.org/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}",
            "line": "https://codeberg.org/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number}",
            "line_range": "https://codeberg.org/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number_start}-L{line.number_end}",
        },
    },
    "forgejo": {
        "name": "Forgejo",
        "auto_skill": False,
        "clone": {"protocol": "https", "url": "https://code.forgejo.org/{git.org}/{git.repo}.git"},
        "links": {
            "directory": "https://code.forgejo.org/{git.org}/{git.repo}/src/commit/{git.commit}/{file.directory}",
            "file": "https://code.forgejo.org/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}",
            "line": "https://code.forgejo.org/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number}",
            "line_range": "https://code.forgejo.org/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number_start}-L{line.number_end}",
        },
    },
    "gcp-cloud-source": {
        "name": "Google Cloud Source",
        # Cloud Source Repositories closed to new customers on June 17, 2024.
        # Google is steering everyone to Secure Source Manager (see "gcp-ssm").
        "clone": {"protocol": "https", "url": "https://source.developers.google.com/p/{git.org}/r/{git.repo}"},
        "links": {
            "directory": "https://source.cloud.google.com/{git.org}/{git.repo}/+/{git.commit}:{file.directory}",
            "file": "https://source.cloud.google.com/{git.org}/{git.repo}/+/{git.commit}:{file.path}",
            "line": "https://source.cloud.google.com/{git.org}/{git.repo}/+/{git.commit}:{file.path};l={line.number}",
            "line_range": "https://source.cloud.google.com/{git.org}/{git.repo}/+/{git.commit}:{file.path};l={line.number_start}-{line.number_end}",
        },
    },
    "gcp-ssm": {
        "name": "GCP Secure Source Manager",
        # Secure Source Manager has no fixed multi-tenant domain. Each deployment is a
        # regionally-deployed, single-tenant instance with its own generated hostname:
        #   git:  https://{instance}-{project_number}-git.{location}.sourcemanager.dev/{project}/{repo}.git
        #   html: https://{instance}-{project_number}.{location}.sourcemanager.dev/{project}/{repo}
        # (confirmed: https://docs.cloud.google.com/secure-source-manager/docs/list-view-repositories)
        #
        # Define a per-instance hosts entry in sourcerer.yml with the full instance hostname
        # baked into the clone URL and citation link templates. git.org is the GCP project name;
        # git.repo is the repository name. Example id: gcp-ssm-us-central1.
        #
        # This built-in entry is a shape/documentation placeholder only. The CONFIGURE-ME
        # placeholder in the URLs below will produce obviously broken links if used without
        # a hosts: override (that is intentional).
        #
        # The /src/commit/ path convention below follows Gitea (SSM appears Gitea-based from
        # its webhook payload docs) but was not directly confirmed against a live instance.
        # Spot-check the file-browsing path before relying on it.
        "auto_skill": False,
        "clone": {"protocol": "https", "url": "https://CONFIGURE-YOUR-SSM-INSTANCE-git.sourcemanager.dev/{git.org}/{git.repo}.git"},
        "links": {
            "directory": "https://CONFIGURE-YOUR-SSM-INSTANCE.sourcemanager.dev/{git.org}/{git.repo}/src/commit/{git.commit}/{file.directory}",
            "file": "https://CONFIGURE-YOUR-SSM-INSTANCE.sourcemanager.dev/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}",
            "line": "https://CONFIGURE-YOUR-SSM-INSTANCE.sourcemanager.dev/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number}",
            "line_range": "https://CONFIGURE-YOUR-SSM-INSTANCE.sourcemanager.dev/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number_start}-L{line.number_end}",
        },
    },
    "gitea": {
        "name": "Gitea",
        "clone": {"protocol": "https", "url": "https://gitea.com/{git.org}/{git.repo}.git"},
        "links": {
            "directory": "https://gitea.com/{git.org}/{git.repo}/src/commit/{git.commit}/{file.directory}",
            "file": "https://gitea.com/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}",
            "line": "https://gitea.com/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number}",
            "line_range": "https://gitea.com/{git.org}/{git.repo}/src/commit/{git.commit}/{file.path}#L{line.number_start}-L{line.number_end}",
        },
    },
    "github": {
        "name": "GitHub",
        "clone": {"protocol": "https", "url": "https://github.com/{git.org}/{git.repo}.git"},
        "links": {
            "directory": "https://github.com/{git.org}/{git.repo}/tree/{git.commit}/{file.directory}",
            "file": "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}",
            "line": "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}#L{line.number}",
            "line_range": "https://github.com/{git.org}/{git.repo}/blob/{git.commit}/{file.path}#L{line.number_start}-L{line.number_end}",
        },
    },
    "gitlab": {
        "name": "GitLab",
        "clone": {"protocol": "https", "url": "https://gitlab.com/{git.org}/{git.repo}.git"},
        "links": {
            "directory": "https://gitlab.com/{git.org}/{git.repo}/-/tree/{git.commit}/{file.directory}",
            "file": "https://gitlab.com/{git.org}/{git.repo}/-/blob/{git.commit}/{file.path}",
            "line": "https://gitlab.com/{git.org}/{git.repo}/-/blob/{git.commit}/{file.path}#L{line.number}",
            "line_range": "https://gitlab.com/{git.org}/{git.repo}/-/blob/{git.commit}/{file.path}#L{line.number_start}-{line.number_end}",
        },
    },
    "launchpad": {
        "name": "Launchpad",
        # Launchpad uses cgit to serve the web view at git.launchpad.net itself (confirmed:
        # https://documentation.ubuntu.com/launchpad/developer/explanation/code/), so this is
        # standard cgit URL/anchor convention, not a guess. cgit has no native range-highlight
        # fragment, hence line_range only uses number_start (same limitation as AWS below).
        "clone": {"protocol": "https", "url": "https://git.launchpad.net/{git.repo}"},
        "links": {
            "directory": "https://git.launchpad.net/{git.repo}/tree/{file.directory}?id={git.commit}",
            "file": "https://git.launchpad.net/{git.repo}/tree/{file.path}?id={git.commit}",
            "line": "https://git.launchpad.net/{git.repo}/tree/{file.path}?id={git.commit}#n{line.number}",
            "line_range": "https://git.launchpad.net/{git.repo}/tree/{file.path}?id={git.commit}#n{line.number_start}",
        },
    },
    "sourcehut": {
        "name": "SourceHut",
        "clone": {"protocol": "https", "url": "https://git.sr.ht/~{git.org}/{git.repo}"},
        "links": {
            "directory": "https://git.sr.ht/~{git.org}/{git.repo}/tree/{git.commit}/item/{file.directory}",
            "file": "https://git.sr.ht/~{git.org}/{git.repo}/tree/{git.commit}/item/{file.path}",
            "line": "https://git.sr.ht/~{git.org}/{git.repo}/tree/{git.commit}/item/{file.path}#L{line.number}",
            # NOTE: single-line anchors are confirmed live (e.g. .../item/lib.rs#L109). The range
            # form below follows GitLab's convention and is believed correct but was not directly
            # confirmed against a live multi-line SourceHut link.
            "line_range": "https://git.sr.ht/~{git.org}/{git.repo}/tree/{git.commit}/item/{file.path}#L{line.number_start}-{line.number_end}",
        },
    }
}

_VALID_PROTOCOLS = ("https", "ssh")


def _merge_host(base: dict | None, override: dict, host_id: str) -> Host:
    """Fold `override` (a user sourcerer.yml hosts[i] entry, already without its `id`) over a
    built-in default `base` (may be None for a wholly custom host). Only the leaf fields the user
    provides win; every omitted field keeps its default. Returns a validated Host.

    auto_skill is not a user-facing config field and is intentionally not settable via hosts:.
    It is taken from the built-in base only (default True for custom hosts with no base)."""
    base = base or {}
    name = override.get("name", base.get("name", host_id))

    base_clone = base.get("clone", {})
    over_clone = override.get("clone", {})
    protocol = over_clone.get("protocol", base_clone.get("protocol", "https"))
    if protocol not in _VALID_PROTOCOLS:
        raise ValueError(f"host {host_id!r}: clone.protocol must be one of {_VALID_PROTOCOLS}")
    if protocol == "ssh":
        raise ValueError(f"host {host_id!r}: clone.protocol 'ssh' is not implemented yet (use 'https')")
    url = over_clone.get("url", base_clone.get("url"))
    if not url:
        raise ValueError(f"host {host_id!r}: clone.url is required (no built-in default to inherit)")

    base_links = base.get("links", {})
    over_links = override.get("links", {})
    links = {}
    for kind in LINK_KINDS:
        template = over_links.get(kind, base_links.get(kind))
        if not template:
            raise ValueError(f"host {host_id!r}: links.{kind} is required (no built-in default to inherit)")
        links[kind] = template

    # auto_skill comes from the built-in base only; True for custom hosts that have no base.
    auto_skill: bool = base.get("auto_skill", True)

    return Host(
        id=host_id,
        name=name,
        clone_protocol=protocol,
        clone_url_template=url,
        links=links,
        auto_skill=auto_skill,
    )


def resolve_hosts(config_hosts: list[dict] | None) -> dict[str, Host]:
    """Merge the user's sourcerer.yml `hosts:` entries (may be None) over the built-in defaults
    and return the full registry as {host_id: Host}. A config entry that names a known id
    overrides only the fields it provides; an entry with an unknown id defines a wholly custom
    host (which must then supply clone.url and all four links). Every built-in host is always
    present in the result, even if the config overrides none of them."""
    overrides: dict[str, dict] = {}
    for i, entry in enumerate(config_hosts or []):
        if not isinstance(entry, dict):
            raise ValueError(f"hosts[{i}] must be a mapping")
        host_id = entry.get("id")
        if not host_id:
            raise ValueError(f"hosts[{i}]: 'id' is required")
        validate_host_id(host_id)
        if host_id in overrides:
            raise ValueError(f"hosts[{i}]: duplicate host id {host_id!r}")
        overrides[host_id] = {k: v for k, v in entry.items() if k != "id"}

    resolved: dict[str, Host] = {}
    for host_id, base in KNOWN_HOSTS.items():
        resolved[host_id] = _merge_host(base, overrides.get(host_id, {}), host_id)
    for host_id, override in overrides.items():
        if host_id in resolved:
            continue
        resolved[host_id] = _merge_host(None, override, host_id)
    return resolved
