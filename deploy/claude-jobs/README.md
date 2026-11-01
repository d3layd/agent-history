# claude-jobs shipper: install steps

These are Dennis's steps on lvtd-fleet-1. Nothing in this PR runs them. Each
one is a cross-user or root change, so run them yourself (`!` passthrough).
The hub needs no new agent-history version for this: only a directory, a key
line and a watch line.

Design: `docs/design/claude-jobs-shipper.md`.

## 0. Get the files onto the box

```bash
git clone --depth 1 -b feat/claude-jobs-shipper https://github.com/d3layd/agent-history /tmp/ah-deploy
cd /tmp/ah-deploy/deploy/claude-jobs
```

## 1. Hub (user `ahub`): inbox directory, key, watch

```bash
# a. The machine directory the key will be pinned to
sudo -u ahub install -d -m 700 /home/ahub/inbox/dennis/claude-jobs

# b. claude-jobs' upload-only key (created in step 2a). Append ONE line:
#    command="rrsync -wo /home/ahub/inbox/dennis/claude-jobs",restrict ssh-ed25519 AAAA... ingest-dennis-claude-jobs
sudo -u ahub sh -c 'cat >> /home/ahub/.ssh/authorized_keys' < /tmp/claude-jobs-ingest.line

# c. Index on arrival: add the directory to the tenant's .path drop-in
sudo systemctl edit agent-history-hub-index@dennis.path
#     [Path]
#     PathChanged=/home/ahub/inbox/dennis/claude-jobs
sudo systemctl restart agent-history-hub-index@dennis.path
```

The one-minute index timer picks batches up even without step 1c.

## 2. claude-jobs user: install, key, env, timer

```bash
sudo -iu claude-jobs
# a. Upload-only key; then build the line for step 1b
ssh-keygen -t ed25519 -N '' -C ingest-dennis-claude-jobs -f ~/.ssh/agent-history-ingest
printf 'command="rrsync -wo /home/ahub/inbox/dennis/claude-jobs",restrict %s\n' \
  "$(cat ~/.ssh/agent-history-ingest.pub)" > /tmp/claude-jobs-ingest.line
ssh-keyscan -H localhost >> ~/.ssh/known_hosts

# b. agent-history (from this branch until it is released)
uv tool install --force "agent-history @ git+https://github.com/d3layd/agent-history@feat/claude-jobs-shipper"

# c. Env file (0600), real paths only
install -d -m 700 ~/.config/agent-history
cat > ~/.config/agent-history/env <<'ENV'
AGENT_HISTORY_HUB_INGEST=ahub@localhost:
AGENT_HISTORY_HUB_KEY=/home/claude-jobs/.ssh/agent-history-ingest
AGENT_HISTORY_MACHINE=claude-jobs
AGENT_HISTORY_JOBS_DIR=/var/lib/claude-jobs/jobs
ENV
chmod 600 ~/.config/agent-history/env

# d. Dry run first: it reads jobs and prints counts, writes and sends nothing
set -a; . ~/.config/agent-history/env; set +a
agent-history ship-jobs --dry-run

# e. The 2-minute timer
install -d ~/.config/systemd/user
cp /tmp/ah-deploy/deploy/claude-jobs/agent-history-ship-jobs.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent-history-ship-jobs.timer
exit

# f. Root, once: let claude-jobs' user timers run with nobody logged in
sudo loginctl enable-linger claude-jobs
```

## 3. Root: the final-ship hook the runner calls

```bash
sudo install -d -o root -g root -m 0755 /usr/local/lib/claude-jobs
sudo install -o root -g root -m 0755 /tmp/ah-deploy/deploy/claude-jobs/ship-final /usr/local/lib/claude-jobs/ship-final
```

## Verify

```bash
sudo -iu claude-jobs systemctl --user list-timers agent-history-ship-jobs.timer
sudo -iu claude-jobs journalctl --user -u agent-history-ship-jobs -n 20
# after the first job: read it back through the Router
#   history_recall(query="<job id>", mode="text", cwd="<job id>")
#   history_show(session="<job id>")
```

Rollback: `systemctl --user disable --now agent-history-ship-jobs.timer` as
claude-jobs, delete the authorized_keys line, `rm /usr/local/lib/claude-jobs/ship-final`.
Records already on the hub stay (the hub is append-only).
