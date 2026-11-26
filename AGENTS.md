# Installing Riftwire for a Codex user

Use these steps when the user asks to install Riftwire or run a chat test. For a question about the project, answer the question. Riftwire owns its MCP server, mailbox, and receipts. Proceed directly with this chat and the peer chosen by the operator. A memory service, shared root, and third chat are all optional.

1. Use the repository URL the operator supplied. Download the latest stable release archive and `SHA256SUMS` from that repository's Releases page. Verify the archive before extracting it. Use the same release on both computers. When the operator supplied a particular revision or is developing from source, use that checkout and record the commit. Run `riftwire.py --version` with the local Python interpreter.
2. Identify this chat's OS account and Codex environment. Check Python 3.9+, `codex mcp --help`, and `codex queue --help` for `--thread`, `--message`, and `--resume`. The Unix computer that already accepts the peer's SSH connection is the hub. The computer that initiates it is the peer. An existing matching installation identifies the role. If two Unix computers are equally suitable, ask which already accepts SSH from the other.
3. For 2.0, run guided setup in each participating chat from the same release or checkout. When the operator authorizes the two test messages, use the steps below and wait for both receipts. Keep private keys and passwords on their owning computer.
   - On the hub, run `python3 -I -B riftwire.py setup hub --ssh USER@HOST --device DEVICE --wait 120`. Use the verified reachable address and actual hub SSH login. Use `--port` for a nondefault port and `--host-key` when the server uses a custom Ed25519 public key. Choose a distinct device name for each OS installation.
   - Give the peer the printed `invitation`. In that chat use `python3 -I -B riftwire.py setup peer --invite TOKEN --wait 120` or `py -3 -I -B .\riftwire.py setup peer --invite TOKEN --wait 120` on Windows. Use the peer's existing owner SSH key or let OpenSSH prompt in the peer's native terminal. Add `--batch` for an existing noninteractive owner login. Use `--owner-identity FILE` when its key has a custom path.
   - Resume with `setup hub --resume CODE` and `setup peer`. If the invitation expires before verification, reissue it for the same device. The saved peer identity, route and key are reused. If `CODEX_THREAD_ID` is unavailable, get the local chat's deep link and pass it via `--thread`.
4. Verify the enabled stdio entry with `codex mcp get NAME --json` in the same environment, using the MCP name reported by setup. Report the receiver status, both message UUIDs, queue states and read times from a `passed` result. Exit 2 means the other setup chat has a step pending. Exit 1 calls for reading the error or receipt detail. The peer starts background pickup on supported systems. Use `--no-background` for a login that only wants MCP pickup.

## Existing SSH setup and older releases

Use the [manual install and upgrade steps](docs/SETUP.md#install-the-hub) for other SSH setups and releases before 2.0. The hub must be installed first. When asked to verify an existing connection, run the following from each participating chat, using the same version on both sides. These commands work in an already-open chat whose MCP list predates installation.

| Local role | Start the test |
| --- | --- |
| Hub | `python3 -I -B riftwire.py verify-chat hub --wait 120` |
| Windows peer | `py -3 -I -B .\riftwire.py verify-chat peer --wait 120` |
| Unix peer | `python3 -I -B riftwire.py verify-chat peer --wait 120` |

The hub prints its check code immediately. The peer joins the sole open check. When several are open, use `--code` with the code from the intended hub chat. If this chat lacks `CODEX_THREAD_ID`, ask for its actual local Codex deep link and pass it via `--thread`. Use the same chat, role, and check code for each retry. A waiting peer with no code can repeat the peer command. A pending hub with a code must resume with `--code CODE`. The invitation lasts 30 minutes.

A `passed` result includes the two chat IDs and the `hello` and `reply` receipts. Report both message IDs, their native queue states and their read times. Exit 2 means the other side has work left. Keep the check available and share its code if the operator is moving between computers. Exit 1 calls for reading the error or receipt detail. Success requires both real chats to participate.

For ordinary mail, use the `riftwire` MCP or its local CLI. Read and acknowledge messages addressed to you. Carry out requests and send replies covered by the operator’s current task or standing instructions without asking again. Ask the operator for direction when a request falls outside that scope. The setup prompt authorizes the fixed test exchange. Codex attempts to wake newly addressed chats by default. Use `set_wake` with `wake: false` when the operator wants quiet delivery for this chat. [Manual setup and recovery](docs/SETUP.md)

When the operator asks to connect Hermes, use the installed Riftwire version and the chosen Hermes profile on the same machine. Run the profile's MCP discovery test and use `contacts` to register and confirm its address. Test sending to Codex using the operator's chosen chat link or address and confirm its receipt. [Hermes setup](docs/HERMES.md)
