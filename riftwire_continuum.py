"""Project a verified Riftwire result for an optional Continuum importer."""


SCHEMA = "riftwire.continuum.result"
VERSION = 1


def archive_record(projects, record, worker, definition, commit, job=None, *, summary=None, include_chat=False):
    """Return the selected result's metadata and evidence digests for local export."""
    verified = projects.verify_record(record, worker, definition, commit, job)
    assignment = record["assignment"]
    report = record["envelope"]["result"]
    attempt = {
        "coordinator": verified["coordinator"],
        "job": projects.guid(verified["job"]),
        "step": verified["step"],
        "attempt": projects.guid(assignment.get("attempt")),
    }
    schema = {"schema": SCHEMA, "version": VERSION}
    # Optional excerpts and links can change without changing the attempt's identity.
    exported = {
        **schema,
        "id": projects.object_id({**schema, **attempt}),
        "project": assignment["project"],
        "outcome": verified["outcome"],
        "provenance": {
            **attempt,
            "commit": verified["commit"],
            "worker": verified["worker"],
            "workflow_sha256": assignment["workflow_sha256"],
            "receipt_sha256": projects.object_id(record),
        },
        "artifacts": verified["artifacts"],
    }
    if summary is not None:
        excerpt = projects.string(summary, 16384)
        if excerpt not in report["summary"]:
            raise ValueError("Choose an exact excerpt from the signed worker summary.")
        exported["summary"] = excerpt
    if type(include_chat) is not bool:
        raise ValueError("Choose whether to include the worker's chat link.")
    if include_chat and report["chat"] is not None:
        exported["links"] = {"chat": f"codex://threads/{projects.guid(report['chat'])}"}
    return exported
