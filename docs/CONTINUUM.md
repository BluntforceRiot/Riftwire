# Optional Continuum handoff

Riftwire can prepare one signed project result for a Continuum adapter. Choose the job and step on the coordinating installation. Keep the original receipt with the independently trusted worker identity and workflow so you can verify the evidence later. [Project workflows](PROJECTS.md)

## Export one result

Run the commands with the expected values for the job, worker, workflow, and commit. The archive command verifies the receipt against those inputs and prints JSON for explicit import.

```sh
umask 077
riftwire project result "$job" "$step" > receipt.json
riftwire project archive receipt.json --worker trusted-worker.json \
  --workflow workflow.json --commit "$commit" --job "$job" > continuum-result.json
```

The `trusted-worker.json` file comes from the assignment's trusted worker enrollment. The workflow and commit come from the operator's expected inputs. Select text with `--summary` followed by an exact excerpt of the signed worker summary. Add `--include-chat` to include its Codex link when present. The default export includes verified metadata and output digests. Treat the selected text as project correspondence.

## Import the archive

The archive is a single JSON object. A Continuum adapter can store it under the intended project when the operator chooses to import it.

| Field | Meaning |
| --- | --- |
| `schema`, `version` | `riftwire.continuum.result` and integer `1` |
| `id` | Stable SHA-256 ID for the coordinator, job, step, and attempt |
| `project`, `outcome` | Project name and the signed `passed`, `failed`, or `interrupted` outcome |
| `provenance` | Coordinator and worker fingerprints, job and attempt UUIDs, step, commit, `workflow_sha256`, and `receipt_sha256` |
| `artifacts` | Output paths and their SHA-256 digests as a JSON object |
| `summary`, `links.chat` | Optional selected text and native `codex://threads/UUID` link |

The `id` hashes an object with `schema`, `version`, `coordinator`, `job`, `step`, and `attempt`. The `receipt_sha256` hashes the full receipt object retained on disk. Both use Riftwire's canonical encoding with sorted JSON keys, ASCII escapes, compact separators, and no trailing newline. An adapter can match on `id` to avoid duplicate records and compare `receipt_sha256` to detect conflicting evidence for that attempt. A re-export may select a different excerpt or link while preserving both digests. Each retry with a new attempt has its own ID.

## Use chosen Continuum context in a brief

Select the notes for the new job and prepare `brief.json`. The same format is produced by `riftwire project brief JOB_UUID --step STEP_ID`.

```json
{
  "summary": "Prepare a release from the selected commit.",
  "decisions": ["Run the existing verification suite."],
  "open_questions": ["Confirm the artifact filename."],
  "references": ["The selected project note or evidence reference"]
}
```

All four fields are optional. `summary` accepts up to 8,192 UTF-8 bytes. Each list accepts up to 24 nonblank strings of up to 1,024 UTF-8 bytes each. Pass the file explicitly with `riftwire project start WORKFLOW --commit REVISION --brief brief.json` to make the chosen context part of the assignment.
