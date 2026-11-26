# Project workflows

The 2.0 development build runs projects across independent Riftwire installations. A coordinator stores a job at one Git revision. Workers claim the steps their operators have approved and return signed receipts. Use the same development build on each participating machine.

## Preview a workflow

Save the workflow JSON in the repository. Give a Codex or Hermes chat the repository link and ask it to inspect the workflow and map its roles to the intended workers. A preview validates the steps and shows existing invitations. It can also validate one or more local worker profiles.

```sh
riftwire project plan examples/workflows/release.json --profile builder.json
```

On Windows, substitute `py -3 -I -B .\riftwire.py` for `riftwire`. Project workflows use Git, Python 3.9 or newer, and OpenSSH with `ssh-keygen -Y` signing support. The coordinator accepts SSH through a Unix login. A Windows installation can join as an outbound worker.

A workflow names the repository, roles, step actions, dependencies, and expected artifacts. Put dependencies before their consumers. Several ready steps can run at the same time. `different_from` on a dependency requires a different enrolled worker key. Assign that role to a participant with the intended hardware. An `approve` step records the coordinator operator's decision when all prerequisites have passed.

## Invite a worker

Run `riftwire project init NAME` under each participant's local login. The worker shares the public document from `riftwire project identity`. On the coordinator, enroll that identity in one project with the intended roles.

```sh
riftwire project invite worker.json riftwire windows-verifier --write invitation.json --add-key
```

`--add-key` adds the restricted entry to the coordinator login's `authorized_keys`. Omit it when you manage the key file separately and use the returned entry. Share `invitation.json`, the reachable SSH alias or endpoint, and the coordinator's verified host pin. On the worker, save the connection using that pin.

```sh
riftwire project join release-team invitation.json --ssh coordinator --known-hosts coordinator.known_hosts
```

Each participant keeps their own key and checkout. To remove a project grant, run `riftwire project revoke WORKER_FINGERPRINT PROJECT` on its coordinator. A worker receives only project status and assignments within its grants. Use a different connection name when joining another project on the same hub.

## Approve a local worker profile

Prepare a JSON file locally. The owner chooses the repo already fetched on that computer, the reachable Git refs, the role, and the actions it can run. For the release example, a Linux builder profile can use the version-independent ZIP filename declared in the workflow.

```json
{
  "version": 1,
  "project": "riftwire",
  "role": "linux-builder",
  "repository": "https://github.com/BluntforceRiot/Riftwire",
  "checkout": "/absolute/path/to/Riftwire",
  "refs": ["refs/heads/main"],
  "actions": {
    "build": {
      "command": ["python3", "-I", "-B", "scripts/build_release.py", "--archive-name", "Riftwire-release.zip"],
      "timeout": 180
    }
  }
}
```

Specify `{"codex":"/absolute/path/to/codex","timeout":300}` for an action that opens a fresh Codex chat. Riftwire sets workspace-write for builds, tests and patches, and read-only for review and verification. It preserves the native login and uses a fixed sandbox with elevation disabled. The worker returns a verdict and a summary with its native chat link. A command profile runs the owner's configured argument list as that worker's OS user. Step text is available to the Codex worker as project context. Only execute project revisions you trust under that login. [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode)

The worker checks the exact commit against the locally permitted refs and opens a fresh detached Git worktree. Commands receive `RIFTWIRE_PROJECT_COMMIT` and `RIFTWIRE_PROJECT_INPUTS`. The input directory contains any dependency artifacts under their originating step ID. For the release example, the verifier can check `build/dist/Riftwire-release.zip` against `build/dist/SHA256SUMS` and run its approved native checks against the candidate. The receipt ties that step to the dependency's artifact digests.

For the release workflow's `windows-verifier` profile, set the `verify` action's command to `["py", "-3", "-I", "-B", "examples/verify_release.py"]`. The script checks the input manifest, compares the offered ZIP with an independent rebuild from the exact approved checkout, and runs the native Python suite from that checkout. A Unix verifier can use `python3` in place of `py -3`.

## Run, review, and verify a job

On the coordinator, start the workflow with the full Git commit ID. `--brief BRIEF.json` adds selected context. Each worker runs its own profile.

```sh
riftwire project start examples/workflows/release.json --commit FULL_GIT_COMMIT
riftwire project work builder.json
riftwire project work windows.json --via release-team
riftwire project list --project riftwire
riftwire project status JOB_UUID
riftwire project history JOB_UUID --step windows
```

Use `--watch` to poll for eligible work under the same profile. A local worker can finish and save its signed result during a connection loss. The next successful connection can submit the saved result under the same claim. An interrupted step keeps its worktree. After an abnormal stop, use `work PROFILE --checkpoint` with the same connection to sign and report preserved changes. The coordinator can then `requeue JOB_UUID STEP`. The next eligible worker receives the checkpoint and applies its patch to a fresh checkout. Old receipts remain recorded in the project database.

When a worker cannot return a checkpoint or receipt, the coordinator can close its exact claim with `riftwire project abandon JOB_UUID STEP ATTEMPT_UUID --reason "Worker stopped during verification."`. Get the attempt UUID from `project status`. History records the operator's decision, and the coordinator rejects later reports for that claim. Confirm the old worker has stopped before using `project requeue` to make the step eligible again.

For parallel fixes, use `examples/workflows/compare-fixes.json`. Run the two author profiles and then inspect the two signed patches with `riftwire project compare JOB_UUID first-fix second-fix`. Each worktree stays separate. Reviewers can receive both patches as dependency inputs.

When a required approval is ready, the coordinator operator may record it with `riftwire project approve JOB_UUID publish-review --note "Reviewed the signed results."`. Publication uses the repository's own release process. Codex and Hermes can discover jobs with `project_list`, read progress with `project_status`, and inspect recorded attempts with `project_history`. A worker can supply its saved `via` connection name. The coordinator checks the worker's current project grant.

Export a receipt and verify it with the worker identity you trust independently and the expected workflow and commit.

```sh
riftwire project result JOB_UUID windows > receipt.json
riftwire project verify receipt.json --worker worker.json --workflow examples/workflows/release.json --commit FULL_GIT_COMMIT --job JOB_UUID
```

Pin the identity from the invitation exchange. The worker's signature covers its verdict, output checksums, and the assignment digest. That assignment includes the input checksums. A separate native runner can independently repeat the check on its own checkout and compare the resulting evidence. Each artifact can be up to 1 MiB. A step can output up to 2 MiB, including its patch.

Use the `next_before` cursor from `project history` with `--before` to page through older attempts. On the coordinator, `project result JOB_UUID STEP --attempt ATTEMPT_UUID` exports a prior signed receipt after a requeue.

## Bring selected context to another agent

Use `riftwire project brief JOB_UUID --step STEP_ID > brief.json` to retain selected step summaries and artifact references. A brief accepts `summary`, `decisions`, `open_questions`, and `references`. Use `--brief brief.json` to send it with a new job to the eligible worker. To prepare a selected verified result and its provenance for import into project memory, use `riftwire project archive`. [Optional Continuum handoff](CONTINUUM.md)

## Protocol for another client

The project SSH key uses the forced command `riftwire-project-v1`. Send one UTF-8 JSON document on stdin, close stdin, and read one JSON response. Requests carry `version` 1, `method`, and `arguments`. Responses carry the `coordinator` fingerprint with `result` or `error`. The bound public key supplies the caller identity.

| Method | Arguments | Result |
| --- | --- | --- |
| `hello` | `project` | Protocol version and coordinator identity |
| `list` | `project`, optional `limit` | Recent jobs in the invited project and a `has_more` flag |
| `status` | `job` | Step states, approvals, and artifact digests |
| `history` | `job`, optional `step`, `limit`, `before` | Recorded attempt summaries and the next page cursor |
| `claim` | `project`, `role`, `actions`, `repository` | An existing or ready assignment for the member, or `null` |
| `report` | `job`, `step`, `attempt`, `envelope` | The recorded result digest and outcome |

An assignment carries the job, attempt, member, coordinator, repository, revision, normalized workflow digest, step, brief, input artifacts, and any checkpoint. A result contains `version`, `member`, `assignment` (the assignment SHA-256), `outcome`, `summary`, `artifacts`, and `chat` (a native chat UUID or `null`). An envelope contains `result` and its armored `signature`. To compute digests or sign a result, encode JSON with sorted object keys, ASCII escapes for non-ASCII characters, compact separators, finite numbers, and no trailing newline. Use OpenSSH Ed25519 signing with namespace `riftwire-project-v1`. Artifacts carry `sha256` and base64 `content`. Requests and replies are capped at 6 MiB.

## Verify a project connection locally

On a Unix login with Git and a working OpenSSH server executable, run `python3 -I -B scripts/verify_project_link.py`. The fixture creates two project stores and repositories. It joins them through a temporary SSH listener on loopback, sends a build artifact, and checks the independent worker's signed result. It uses disposable credentials and project data in a private directory.
