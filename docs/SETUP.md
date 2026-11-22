# Setup and operation

## Choose the two roles

| | Mailbox computer (hub) | Connecting computer (peer) |
| --- | --- | --- |
| System | Linux or macOS with an existing SSH server and `python3` on its SSH command path | Windows, Linux, or macOS with an OpenSSH client |
| Codex | The local account that owns the hub chat | The local account that owns the peer chat |
| Network | Accepts the peer's existing key-based login | Initiates all connections, including the stream that receives incoming notices |

Both accounts need Python 3.9 or newer and a Codex CLI that supports `codex queue --thread … --message … --resume` and `codex mcp`. Use native Windows tools for a native Windows chat. A chat running under WSL uses its own Linux environment. Check the SSH host fingerprint through a trusted channel the first time you set up the SSH connection. An SSH agent may supply the peer's existing key. Share just the host alias with Codex.

## Verify the download

Download the versioned ZIP and `SHA256SUMS` from the same GitHub release to one folder. On Linux run `sha256sum -c SHA256SUMS`. On macOS use `shasum -a 256 -c SHA256SUMS`. In PowerShell, the following checks the archive named by the manifest. Extract the ZIP after the check passes, then open the folder containing `riftwire.py`.

```powershell
$entry = (Get-Content .\SHA256SUMS -Raw).Trim() -split '\s+', 2
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $entry[1]).Hash
if ($actual -ne $entry[0]) { throw 'Riftwire archive checksum mismatch' }
```

## Install the hub

```sh
python3 -I -B riftwire.py install-hub
python3 -I -B riftwire.py check hub
codex mcp get riftwire --json
```

The default routing names are `hub` and `peer`. To choose your own on first install, add `--name laptop --peer desktop`. An upgrade reuses the saved pair. The hub installs its code and settings under `~/.local/share/riftwire/` and creates the `~/.local/bin/riftwire` command. Use the `python3` invocation when that directory is outside your shell's PATH.

## Install the peer

After installing the hub, run one of these commands from the same release on the peer. Replace `my-hub` with the SSH configuration alias or `user@host` for the hub account.

```powershell
py -3 -I -B .\riftwire.py install-peer --ssh my-hub
codex mcp get riftwire --json
```

```sh
python3 -I -B riftwire.py install-peer --ssh my-hub
codex mcp get riftwire --json
```

Options include `--identity FILE` for an existing dedicated private key, `--known-hosts FILE` for an existing pin file, and `--codex FILE` for the native Codex executable. The hub also accepts `--codex`. The installer checks the endpoint and picks up pending notices. It stores the Windows client in `%LOCALAPPDATA%\Riftwire`. The Unix peer uses `$XDG_DATA_HOME/riftwire-peer` or `~/.local/share/riftwire-peer`.

## Prove the two chats can talk

From the hub chat run `python3 -I -B riftwire.py verify-chat hub --wait 120`. On a Windows peer run `py -3 -I -B .\riftwire.py verify-chat peer --wait 120`. A Unix peer uses `python3 -I -B riftwire.py verify-chat peer --wait 120`. Begin in either chat and let both commands run. If an interval ends, resume the same check with `--code` and the hub's printed code.

The peer sends a unique greeting. The hub reads and acknowledges that greeting, then returns a digest of what it read. The peer verifies and acknowledges the reply. `passed` requires each destination's native queue to accept a notice and each recipient to acknowledge the correct text. The receipts carry message UUIDs and Unix timestamps in seconds. The test touches only its own two message bodies. An invitation expires after 30 minutes. Its messages and their receipts stay in the mailbox.

## Local friends list

Ask Codex or Hermes to “Show my Riftwire friends list on this PC,” or call `contacts` with `{"local_only":true,"search":"Planning"}`. The `local_chats` list includes each chat’s title, folder name and stable `host/UUID` address. Search also matches folder names and IDs. For a long list, use `local_next_offset` as the next `offset` with the same search term.

Riftwire looks up the local Codex profile’s saved, unarchived interactive chats each time. It uses Codex 0.171.0 or later for discovery. Call `set_favorite` with a chat’s `thread_id` and `favorite: true` to put it at the top on this computer. Use `favorite: false` to remove it from favorites.

Opening the list keeps titles and folder names on this computer. To advertise a chat to the paired computer, enroll it under an alias with `share: true` and an optional shared title. Call `enroll` with the same alias and `share: false` to hide the contact from the remote list and title lookup. Known links and addresses can still receive messages. On the peer, local discovery works even when the hub is offline. Delivery still uses the hub connection.

## Daily use and troubleshooting

- **Address a chat.** Use `host/alias` on either computer or `host/UUID` for a Codex chat without an alias. An unqualified link, UUID, alias, or exact shared title routes to the other computer. For a local deep link use `host/codex://threads/<UUID>`. Use `contacts` to see the actual host names and addresses.
- **Check a new message immediately.** Use `inbox` and then `ack` with its UUID after reading it. Require a direct operator instruction covering any reply, command, access request, or other action drawn from peer correspondence.
- **An open chat has old tools.** Inside that chat's Codex shell, use the installed CLI. On the hub, `printf '{}' | ~/.local/bin/riftwire call hub inbox` reads its inbox. On a Windows peer, `'{}' | py -3 "$env:LOCALAPPDATA\Riftwire\riftwire.py" call-peer inbox` does the same. On a Unix peer, invoke the installed peer script with `call-peer inbox`. For `ack`, supply JSON with `message_id` on standard input. Refresh the MCP connections or open a new chat to load the tools.
- **The peer was asleep.** Its MCP reconnects and retrieves notices after the hub becomes reachable. For background pickup without an active MCP client, run the installed peer script with `listen-peer` under that same login. An interrupted delivery can repeat the native notice for one message UUID. The mailbox stores the message once.
- **The native queue reported a failure.** Check that this login's Codex CLI can access the intended chat and supports `queue`. The message remains in the inbox. After correcting the problem, start a fresh two-chat check.
- **A send was interrupted.** Retry using the same `message_id`, recipient and text. The hub handles repeated submissions for the same message. A message can hold up to 8 KiB of UTF-8 text.
- **The mailbox reached a limit.** The SSH peer can submit 30 new messages per minute across its Codex and Hermes addresses. Idempotent retries keep the original message. The SQLite mailbox has a 64 MiB growth limit. An existing larger database keeps its allocated pages and history. At the storage limit, back up the mailbox before archiving read history on the hub.
- **Control wake on message.** A newly addressed or enrolled Codex chat defaults to `wake: true`. Riftwire uses `codex queue --resume` to try to wake the chat. Codex uses the chat’s existing model. Ask Codex to use `set_wake` with `wake: false` to queue notices quietly, or with `wake: true` to resume. Enrollment also accepts `wake`. Omitting it keeps the saved setting. Contacts saved by earlier releases retain their stored setting, which was usually false.
- **Read a reply in Hermes.** Use the Hermes profile’s `inbox` and `ack` tools. A receipt with `notice: inbox_only` means the reply is stored and available through MCP. [Hermes setup](HERMES.md)

## Privacy and extensions

SSH encrypts the connection and checks the hub's host key. When you supply `--known-hosts`, Riftwire uses that file as its sole host-key source and verifies each new connection. The hub saves message bodies in a plaintext SQLite file owned by the hub account. Native notices contain the message UUID and routing addresses. Anyone with the relevant operating-system account's access is inside the trust boundary. Use encryption at rest on the hub if your messages require it.

Riftwire runs independently. The optional Hermes MCP uses the same mailbox and SSH route. Memory services and shared files have their own lifecycles. The base setup requires the two local Codex installations, Python, and the SSH route.

For a dedicated messaging-only SSH key, the hub operator can place a restricted entry in that user's `authorized_keys`. Replace the interpreter and script paths with their verified absolute paths and use the peer's public key.

```text
restrict,command="/usr/bin/python3 -I -B /home/YOUR_LOGIN/.local/share/riftwire/riftwire.py ssh-gate" ssh-ed25519 PUBLIC_KEY riftwire-peer
```

Choose that key through a dedicated SSH alias or the peer installer's `--identity` option. The gate admits only Riftwire's endpoint check, notification pickup and stream, and registered mailbox calls.

## Upgrade or disconnect

Install the new release on the hub first, then on the peer, using the same role and SSH alias. Peer upgrades reuse saved `--identity` and `--known-hosts` files for the connection check and subsequent traffic. Supply the verified file again if an earlier upgrade removed its setting or the file moved. Refresh the MCP connections so existing processes load the new code. The hub retains its SQLite mailbox on upgrade. Use a matching release on both sides for the installation test.

To disconnect, run `codex mcp remove riftwire` on each participating login, close any standalone listener, and reload Codex's MCP connections. The local installation files and existing mailbox remain under their per-user paths. A dedicated SSH public key can be revoked by its owning hub administrator.
