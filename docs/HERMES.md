# Connect Hermes Agent

Add Riftwire as a local MCP server in Hermes. A Hermes profile on either Riftwire computer can send to Codex on that computer or across the existing SSH connection. Codex can reply to the profile’s Riftwire inbox.

## Register one profile

1. Upgrade both ends of the [Riftwire connection](SETUP.md) and refresh their MCP connections. On the selected computer, use the Hermes profile that runs as the same OS user and in the same native or WSL environment as Riftwire.
2. Choose an alias for the profile. The examples use `hermes`. Choose a distinct alias for each profile on the same computer. Sessions using the same profile share its address, inbox, and read receipts.
3. Run the matching command from the verified Riftwire release directory. The command prints the configuration with the correct interpreter and installed paths.

| Computer | Command |
| --- | --- |
| Unix hub | `python3 -I -B riftwire.py hermes-config hub --alias hermes` |
| Windows peer | `py -3 -I -B .\riftwire.py hermes-config peer --alias hermes` |
| Unix peer | `python3 -I -B riftwire.py hermes-config peer --alias hermes` |

Use the printed `command` and `args` with `hermes mcp add riftwire --command PATH --args ...`. To target a named profile, put `-p PROFILE` before `mcp`. You can also copy the printed `riftwire` entry into that profile’s `mcp_servers` mapping in `config.yaml`. Keep the other server entries. [Hermes MCP configuration](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp)

The following examples assume the normal installation paths. Replace the interpreter or script path with the one printed by `hermes-config` when it differs.

**Linux or macOS hub**

```sh
hermes mcp add riftwire --command "$(command -v python3)" --args \
  -I -B "$HOME/.local/share/riftwire/riftwire.py" mcp-hermes hub --alias hermes
```

**Native Windows peer**

```powershell
$cfg = (py -3 -I -B .\riftwire.py hermes-config peer --alias hermes | ConvertFrom-Json).mcp_servers.riftwire
$python = $cfg.command
$riftwireArgs = @($cfg.args)
hermes mcp add riftwire --command $python --args @riftwireArgs
```

On a Unix peer use the `hermes-config peer` output. If the existing route uses an SSH agent socket, ensure it is reachable from the Hermes MCP process. Hermes filters the environment of stdio subprocesses. An explicit `IdentityAgent` setting in the peer’s OpenSSH config can point it at the existing socket. [Hermes stdio environment](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp#stdio-env-filtering)

Run `hermes mcp test riftwire` under the same profile. Start a chat or run `/reload-mcp` and ask Hermes to use Riftwire’s `contacts` tool. Its result registers the profile and shows its address, such as `hub/hermes`. The other tools are `send`, `inbox`, `ack`, and `receipt`.

## Send and verify

Ask Hermes to “Use Riftwire to send ‘Hello from Hermes’ to `hub/build`.” The fully qualified address selects the exact host. A bare Codex chat link routes to the opposite computer. Prefix a link with the local host name to reach the local Codex chat, for example `hub/codex://threads/<UUID>`. Use the host names returned by `contacts` and the actual Codex link or address.

The recipient can read and acknowledge the message. Ask Hermes to check its receipt by message ID. Codex may try to resume automatically, depending on that chat’s wake preference. On the connecting computer, either the Codex or Hermes Riftwire MCP can pick up pending Codex notices. A standalone `listen-peer` also works.

To reply, ask Codex to send to the address shown by Hermes, such as `hub/hermes`. Then ask Hermes to read its Riftwire inbox and acknowledge the response. Hermes replies are pulled from the inbox on demand. The sender’s receipt shows `inbox_only` until the profile acknowledges it, at which point the receipt also includes `seen`.

To disconnect Hermes, remove its `riftwire` MCP entry from that profile and reload its MCP connections. Configure the same alias again to reopen that profile’s existing mailbox.
