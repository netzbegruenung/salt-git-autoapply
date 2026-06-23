# salt-git-autoapply

Periodically pulls a Salt git repo (`/srv/salt`) and, based on the messages of
the newly fetched commits, runs `salt '<minion>' state.highstate` or
`salt '<minion>' state.apply '<state>'` according to configured rules.

## How it works

One invocation = one cycle:

1. `git fetch` (does not move `HEAD`).
2. Determine the new commits in `HEAD..@{u}`.
3. If `require_signed_commits` is set, verify **every** new commit has a
   trusted SSH signature (principal = committer email, key in
   `trusted_signers_dir`). If any commit fails, the cycle aborts and nothing
   is merged or applied.
4. `git merge --ff-only` to advance the working tree.
5. Match each new commit's **subject line** against the rule regexes. Named
   capture groups are substituted into the `minion`/`state` templates.
6. De-duplicate to unique `(minion, action, state)` commands and run each once.
   An individual salt failure is logged; remaining commands still run.

A non-fast-forward (diverged local repo) aborts the cycle for a human to fix.

## Requirements

- Python 3.10+ with PyYAML
- git >= 2.34 and `ssh-keygen` (openssh-client) for SSH signature verification
- Salt master CLI (`salt`)

## Install

```sh
install -m 0755 salt_git_autoapply.py /usr/local/bin/salt_git_autoapply.py
install -d /etc/salt-git-autoapply/trusted-signers
install -m 0644 config.example.yaml /etc/salt-git-autoapply/config.yaml
# edit /etc/salt-git-autoapply/config.yaml and add signer files
install -m 0644 systemd/salt-git-autoapply.service /etc/systemd/system/
install -m 0644 systemd/salt-git-autoapply.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now salt-git-autoapply.timer
```

## Configuration

See `config.example.yaml`. Rules:

```yaml
rules:
  - name: lunes-cms
    match: '^Renovate: Update dependency digitalfabrik/lunes-cms'
    minion: 'lunes-prod.example.com'
    action: apply
    state: lunes-cms

  - name: wordpress
    match: '^Wordpress: .*\b(?P<host>wordpress\d+)\b'
    minion: '{host}.example.com'
    action: highstate
```

## Testing

```sh
# Verify + match, but print salt commands instead of running them:
salt_git_autoapply.py --config /etc/salt-git-autoapply/config.yaml --dry-run

# Watch logs:
journalctl -u salt-git-autoapply -f
```
