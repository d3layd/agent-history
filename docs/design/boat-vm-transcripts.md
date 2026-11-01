# Design note: Boat VM transcripts before teardown (piece B)

Status: draft, for review with the PR that implements it. Stacked on piece A
(`docs/design/claude-jobs-shipper.md`); it reuses A's job shipper and its
confirmed final ship.

## Goal

A fleet job (a `claude -p` job on fleet-1, run as `claude-jobs`) drives its
own Boat VMs, each running Claude Code. The runbook's rule: VMs are stopped
(paused), never deleted, until the work is verified, and their transcripts
go into agent history before teardown. This piece makes that one command
with a hard exit code:

```
agent-history boat-export --job <job_id> <vm_id> [<vm_id> ...]
```

Exit 0 means: every listed VM's full `~/.claude/projects` was pulled,
matched the VM byte for byte, arrived on the hub (rsync confirmed), **and the
hub has indexed it** (the runner's `confirm-indexed` says so). Only then may
the fleet job run `boat delete`. Exit 4 means "arrived, awaiting index
confirmation": the VM is stopped and safe to keep, not yet safe to delete.
Any other exit also leaves every VM stopped (paused) and deletes nothing.
`boat-export` never deletes a VM itself.

| exit | meaning |
|---|---|
| 0 | arrived and indexed: safe to delete |
| 4 | arrived, index not confirmed yet: keep it stopped, poll `--check` |
| 1 | a VM failed (pull, verify, stop) or the hub didn't confirm arrival |
| 2 | bad job id, VM id, job dir, or no hub configured |
| 3 | another ship held the lock |

The VMs need nothing installed: no agent-history, no connector, no hub key.
Everything is pulled from the outside with the Boat CLI.

## Data flow

```
fleet job (claude-jobs on fleet-1)
  └─ agent-history boat-export --job J bx_1 bx_2
       for each VM:
         boat info  → stopped? → boat resume (the disk survives a stop)
         boat exec  → sha256 of every ~/.claude/projects/**.jsonl on the VM
         boat scp -r bx_N:.claude/projects → <jobs>/J/vms/.bx_N.part/
         verify     → same files, same sha256 as the VM reported
         swap in    → <jobs>/J/vms/bx_N/projects/   (atomic rename)
         boat stop  → wait for state "stopped"      (also on any failure)
       ship-jobs --final J   (piece A: redact, upload-only rsync, confirm)
       write <jobs>/J/vms/bx_N/EXPORTED.json per confirmed VM
       print "safe to delete: bx_1 bx_2"
```

This follows the runbook's layout: "before deletion the job copies each VM's
~/.claude/projects folder into the job folder (one sub-folder per VM id) and
runs the shipper once."

## The index gate: how claude-jobs learns indexing is confirmed

`boat-export` runs as claude-jobs, which stays upload-only: it cannot read
the hub, so it cannot check indexing itself. The check runs on the runner's
side (`agent-history confirm-indexed --job <job_id>`, as Clamps, piece A),
and its result comes back as a file in the job directory:

```
<jobs>/<job_id>/index-confirmed.json    the JSON of `confirm-indexed --json` (or --receipt)
```

`boat-export` and `boat-export --check` treat a VM as indexed only when that
file names this job, says `"state": "indexed"`, lists the VM's session
`<job_id>/vm/<vm_id>` in `sessions`, and lists the digest of **this exact
copy** in `vm_copies`. Each VM header record carries `Copy: sha256 <digest of
the pulled files>`; `confirm-indexed` reports the digests of the VM headers
the hub has indexed, and the export receipt records its own. A confirmation
from before a re-export names the old copy, so it does not count. Content,
not clocks: the first live run showed WSL's clock stepping back two seconds
between the job's receipt and the runner's check, and on the fleet the two
files are written by different users at different times.
`--index-receipt FILE` points elsewhere.

Flow on the fleet:

```
fleet job (claude-jobs)      boat-export --job J bx_1      → 4: arrived, stopped
runner (Clamps, 1/min)       confirm-indexed --job J        → 0 once indexed
runner → job dir             write the JSON to <jobs>/J/index-confirmed.json
                             (the runner's `claude-job set-indexed`, which runs as claude-jobs)
fleet job or cleanup         boat-export --job J --check bx_1 && boat delete bx_1
```

The confirmation sits in a directory claude-jobs owns, so a job could forge
it. That would only let a job delete its own VMs early and lose its own
transcripts; it gives no read access to anything.

## File, path and header layout

On fleet-1, inside the job directory:

```
<jobs>/<job_id>/vms/<vm_id>/projects/<slug>/<session>.jsonl   the pulled copy
<jobs>/<job_id>/vms/<vm_id>/EXPORTED.json                     the receipt (below)
<jobs>/<job_id>/vms/.<vm_id>.part/                            in-flight pull, never shipped
```

On the hub, VM records are job records (piece A's format) with two
differences:

- `session_id` is `<job_id>/vm/<vm_id>`, so each VM is its own
  conversation: `history_show(session="<job_id>/vm/<vm_id>")`. The parent
  job's own conversation stays `history_show(session="<job_id>")`.
- the `job` object says `"kind": "vm", "vm_id": "<vm_id>"`.
- each record carries the VM's own Claude Code session uuid
  (`claude_session_id` and a `claude` alias, piece A), so
  `history_show(session="<uuid>")` opens that VM's conversation on an
  upgraded hub.

Each VM also gets a header record, `role: "job"`, whose text starts
`[claude-jobs vm] <job_id> vm <vm_id>` and names the Duet channel, Studio
task and the VM's Claude session ids. So `history_recall("<job_id>",
mode="text", cwd="<job_id>")` returns the parent job and its VMs together,
and `history_recall("<vm_id>", mode="text")` finds one VM. `cwd` follows
piece A's rule (the VM's own cwd never contains the job id, so it is the job
directory, with the real cwd in the header text). `origin` is the real path
of the pulled copy, which contains the job id and the VM id.

Receipt (`EXPORTED.json`, written only after the hub confirmed):

```json
{"job_id": "cj_…", "vm_id": "bx_…", "exported_at": "2026-11-01T23:10:00Z",
 "files": {"-home-user-repo/<session>.jsonl": "<sha256>"},
 "records": 42, "confirmed": "ship-jobs --final exit 0"}
```

`agent-history boat-export --job J --check bx_N` re-verifies a receipt
without touching Boat: the files on disk still match it and every record of
that VM is in the shipped set. A teardown step that runs later than the
export can gate `boat delete` on it.

## Redaction

Unchanged from piece A: the job shipper redacts every record's text, and the
values of the `job` object, before a batch is written. The pulled copy in the
job directory is raw (it sits next to the job's own raw `transcript.jsonl`,
in the claude-jobs user's 0700 tree), and nothing raw leaves fleet-1. VMs
are started `--no-env` (runbook), so their transcripts should not hold
account secrets in the first place.

## Failure modes

Every failure leaves the VM stopped (paused) and deletes nothing. A paused
VM keeps its disk for 14 days, so a failed export can be retried.

| what fails | effect | exit |
|---|---|---|
| Boat API down or slow (it was down 2026-10-30, 05:02–05:28 CDT) | each Boat call has a timeout; that VM is not exported | 1 |
| `boat resume` fails | VM stays stopped | 1 |
| VM has no `~/.claude/projects` transcripts | not exported (nothing to keep is suspicious); `--allow-empty` overrides | 1 |
| pulled copy differs from the VM's checksums | the `.part` copy is discarded | 1 |
| `boat stop` does not reach `stopped` | reported; the VM is not counted as exported | 1 |
| hub upload fails or is unconfirmed | copies stay in the job dir, batches stay queued, no receipt | 1 |
| another ship holds the lock past `--wait` | as above | 3 |
| arrived but the index confirmation is missing, partial, stale, or doesn't list the VM | VM stays stopped, receipt kept | 4 |
| bad job id, missing job, bad VM id, no hub configured | nothing is touched | 2 |

With several VMs, the ones that succeeded are listed as safe and the rest
are not; the exit is 0 only if all succeeded.

## How a parent agent reads it back

```
history_recall(query="cj_20261101T120000Z_a1b2c3", mode="text", cwd="cj_20261101T120000Z_a1b2c3")
  → hits for the job and for each VM (session_id "<job>/vm/<vm>")
history_show(session="cj_20261101T120000Z_a1b2c3/vm/bx_u6qgdzjq", limit=50)
```

## What the fleet job's teardown does (contract for the job runner)

```
agent-history boat-export --job "$JOB" "$VM"     # 4 until indexing is confirmed
# ... the runner writes <jobs>/$JOB/index-confirmed.json after confirm-indexed exits 0
agent-history boat-export --job "$JOB" --check "$VM" && boat delete "$VM"
```

`&&` stays safe: only exit 0 deletes. The runner owns `boat delete`; this
tool never calls it.
