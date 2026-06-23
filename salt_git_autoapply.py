#!/usr/bin/env python3
"""Pull a Salt git repo and apply states based on new commit messages.

Run once per invocation; schedule it with the bundled systemd timer.
"""

import argparse
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass

import yaml

log = logging.getLogger("salt-git-autoapply")


class CycleError(Exception):
    """A condition that aborts the whole cycle without applying anything."""


@dataclass(frozen=True)
class SaltCommand:
    minion: str
    action: str  # "highstate" or "apply"
    state: str | None

    def argv(self, salt_binary: str) -> list[str]:
        if self.action == "highstate":
            return [salt_binary, self.minion, "state.highstate"]
        return [salt_binary, self.minion, "state.apply", self.state]

    def describe(self) -> str:
        if self.action == "highstate":
            return f"salt {self.minion} state.highstate"
        return f"salt {self.minion} state.apply {self.state}"


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    if not config.get("repo_path"):
        raise CycleError("config: 'repo_path' is required")
    config.setdefault("salt_binary", "/usr/bin/salt")
    config.setdefault("command_timeout", 1800)
    config.setdefault("require_signed_commits", False)
    config.setdefault("trusted_signers_dir", None)
    config.setdefault("rules", [])

    if config["require_signed_commits"] and not config["trusted_signers_dir"]:
        raise CycleError(
            "config: 'trusted_signers_dir' is required when "
            "'require_signed_commits' is true"
        )

    compiled = []
    for rule in config["rules"]:
        for key in ("name", "match", "minion", "action"):
            if key not in rule:
                raise CycleError(f"rule {rule!r}: missing '{key}'")
        if rule["action"] not in ("highstate", "apply"):
            raise CycleError(
                f"rule {rule['name']!r}: action must be 'highstate' or 'apply'"
            )
        if rule["action"] == "apply" and not rule.get("state"):
            raise CycleError(
                f"rule {rule['name']!r}: action 'apply' requires 'state'"
            )
        compiled.append(
            {
                "name": rule["name"],
                "regex": re.compile(rule["match"]),
                "minion": rule["minion"],
                "action": rule["action"],
                "state": rule.get("state"),
            }
        )
    config["rules"] = compiled
    return config


def git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-C", repo, "-c", f"safe.directory={repo}", *args]
    return subprocess.run(
        cmd, check=check, capture_output=True, text=True
    )


def build_allowed_signers(trusted_signers_dir: str, dest_dir: str) -> str:
    """Concatenate every file in the trusted-signers dir into one file."""
    if not os.path.isdir(trusted_signers_dir):
        raise CycleError(
            f"trusted_signers_dir does not exist: {trusted_signers_dir}"
        )
    entries = []
    for name in sorted(os.listdir(trusted_signers_dir)):
        path = os.path.join(trusted_signers_dir, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            entries.append(fh.read())
    if not all(e.strip() for e in entries) or not entries:
        raise CycleError(
            f"no signer entries found in {trusted_signers_dir}"
        )
    dest = os.path.join(dest_dir, "allowed_signers")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("\n".join(entries) + "\n")
    return dest


def verify_commit(repo: str, sha: str, allowed_signers: str) -> bool:
    result = git(
        repo,
        "-c", "gpg.format=ssh",
        "-c", f"gpg.ssh.allowedSignersFile={allowed_signers}",
        "verify-commit", sha,
        check=False,
    )
    if result.returncode == 0:
        return True
    log.error("signature verification failed for %s: %s", sha[:12],
              result.stderr.strip())
    return False


def new_commits(repo: str) -> list[str]:
    """SHAs in HEAD..@{u}, oldest first. Empty if nothing was fetched."""
    try:
        upstream = git(repo, "rev-parse", "@{u}").stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise CycleError(
            "no upstream tracking branch configured for the current branch "
            f"({exc.stderr.strip()})"
        )
    rng = f"HEAD..{upstream}"
    out = git(repo, "rev-list", "--reverse", rng).stdout.strip()
    return out.split("\n") if out else []


def commit_subject(repo: str, sha: str) -> str:
    return git(repo, "log", "-1", "--format=%s", sha).stdout.strip()


def commands_for_subject(subject: str, rules: list[dict]) -> list[SaltCommand]:
    commands = []
    for rule in rules:
        match = rule["regex"].search(subject)
        if not match:
            continue
        groups = match.groupdict()
        try:
            minion = rule["minion"].format_map(groups)
            state = rule["state"].format_map(groups) if rule["state"] else None
        except KeyError as exc:
            log.error(
                "rule %r references unknown capture group %s; skipping",
                rule["name"], exc,
            )
            continue
        commands.append(SaltCommand(minion=minion, action=rule["action"],
                                    state=state))
        log.info("commit subject matched rule %r -> %s",
                 rule["name"], commands[-1].describe())
    return commands


def execute(command: SaltCommand, salt_binary: str, timeout: int) -> bool:
    log.info("running: %s", command.describe())
    try:
        result = subprocess.run(
            command.argv(salt_binary),
            check=False, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.error("timed out after %ss: %s", timeout, command.describe())
        return False
    if result.stdout.strip():
        log.info("output of %s:\n%s", command.describe(), result.stdout.strip())
    if result.returncode != 0:
        log.error("salt exited %s for %s: %s", result.returncode,
                  command.describe(), result.stderr.strip())
        return False
    return True


def run_cycle(config: dict, dry_run: bool) -> int:
    repo = config["repo_path"]

    git(repo, "fetch")

    commits = new_commits(repo)
    if not commits:
        log.info("no new commits; nothing to do")
        return 0
    log.info("fetched %d new commit(s)", len(commits))

    if config["require_signed_commits"]:
        with tempfile.TemporaryDirectory(prefix="salt-git-autoapply-") as tmp:
            allowed = build_allowed_signers(config["trusted_signers_dir"], tmp)
            for sha in commits:
                if not verify_commit(repo, sha, allowed):
                    raise CycleError(
                        f"commit {sha[:12]} is unsigned or untrusted; "
                        "aborting without applying anything"
                    )
        log.info("all %d commit(s) have a trusted signature", len(commits))

    seen: set[SaltCommand] = set()
    ordered: list[SaltCommand] = []
    for sha in commits:
        for command in commands_for_subject(commit_subject(repo, sha),
                                             config["rules"]):
            if command not in seen:
                seen.add(command)
                ordered.append(command)

    if dry_run:
        if ordered:
            for command in ordered:
                log.info("[dry-run] would run: %s", command.describe())
        else:
            log.info("[dry-run] no commit matched any rule")
        log.info("[dry-run] leaving working tree unchanged (no merge)")
        return 0

    git(repo, "merge", "--ff-only", "@{u}")
    log.info("fast-forwarded working tree")

    if not ordered:
        log.info("no commit matched any rule")
        return 0
    log.info("%d unique salt command(s) to run", len(ordered))

    failures = 0
    for command in ordered:
        if not execute(command, config["salt_binary"],
                       config["command_timeout"]):
            failures += 1
    log.info("done: %d succeeded, %d failed",
             len(ordered) - failures, failures)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", default="/etc/salt-git-autoapply/config.yaml",
        help="path to the YAML config file",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="verify and match, but print salt commands instead of running",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = load_config(args.config)
        return run_cycle(config, args.dry_run)
    except CycleError as exc:
        log.error("%s", exc)
        return 2
    except subprocess.CalledProcessError as exc:
        log.error("git command failed: %s\n%s",
                  " ".join(exc.cmd), exc.stderr.strip())
        return 2


if __name__ == "__main__":
    sys.exit(main())
