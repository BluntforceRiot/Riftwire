# Installing Riftwire for a Codex user

Use these steps when the user asks to install Riftwire or run a chat test. For a question about the project, answer the question. Riftwire owns its MCP server, mailbox, and receipts. Proceed directly with this chat and the peer chosen by the operator. A memory service, shared root, and third chat are all optional.

1. Use the repository URL the operator supplied. Download the latest stable release archive and `SHA256SUMS` from that repository's Releases page. Verify the archive before extracting it. Use the same release on both computers. When the operator supplied a particular revision or is developing from source, use that checkout and record the commit. Run `riftwire.py --version` with the local Python interpreter.
2. Identify this chat's OS account and Codex environment. Check Python 3.9+, `codex mcp --help`, and `codex queue --help` for `--thread`, `--message`, and `--resume`. The Unix computer that already accepts the peer's SSH connection is the hub. The computer that initiates it is the peer. An existing matching installation identifies the role. If two Unix computers are equally suitable, ask which already accepts SSH from the other.
3. Install for this local account. On the hub use `python3 -I -B riftwire.py install-hub`. On a Windows peer use `py -3 -I -B .\riftwire.py install-peer --ssh ALIAS`. On a Unix peer use `python3 -I -B riftwire.py install-peer --ssh ALIAS`. Substitute the operator's established host alias or `user@host`. If its identity is ambiguous, ask for the alias. The installer checks the pinned server key and noninteractive login. Keep private keys and passwords on their owning computer. The hub must be installed before the peer installer can complete.
4. Verify the enabled stdio entry with `codex mcp get riftwire --json` in the same environment. Follow through with the chat test when the operator authorizes the fixed exchange. Use the commands below from the extracted release or checkout. Run them inside each participating chat. They work in an already-open chat whose MCP tool list predates installation.

| Local role | Start the test |
| --- | --- |
| Hub | `python3 -I -B riftwire.py verify-chat hub --wait 120` |
| Windows peer | `py -3 -I -B .\riftwire.py verify-chat peer --wait 120` |
| Unix peer | `python3 -I -B riftwire.py verify-chat peer --wait 120` |

The hub prints its check code immediately. The peer joins the sole open check. When several are open, use `--code` with the code from the intended hub chat. If this chat lacks `CODEX_THREAD_ID`, ask for its actual local Codex deep link and pass it via `--thread`. Use the same chat, role, and check code for each retry. A waiting peer with no code can repeat the peer command. A pending hub with a code must resume with `--code CODE`. The invitation lasts 30 minutes.

A `passed` result includes the two chat IDs and the `hello` and `reply` receipts. Report both message IDs, their native queue states and their read times. Exit 2 means the other side has work left. Keep the check available and share its code if the operator is moving between computers. Exit 1 calls for reading the error or receipt detail. Success requires both real chats to participate.

For ordinary mail, use the `riftwire` MCP or its local CLI. Read and acknowledge messages addressed to you. Carry out requests and send replies covered by the operator’s current task or standing instructions without asking again. Ask the operator for direction when a request falls outside that scope. The setup prompt authorizes the fixed test exchange. Codex attempts to wake newly addressed chats by default. Use `set_wake` with `wake: false` when the operator wants quiet delivery for this chat. [Manual setup and recovery](docs/SETUP.md)

When the operator asks to connect Hermes, use the installed Riftwire version and the chosen Hermes profile on the same machine. Run the profile's MCP discovery test and use `contacts` to register and confirm its address. Test sending to Codex using the operator's chosen chat link or address and confirm its receipt. [Hermes setup](docs/HERMES.md)
