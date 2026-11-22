![Riftwire in bold, angled lettering over a green signal wire crossing a glowing rift](assets/riftwire-banner.webp)

# Riftwire

**Send a message from a Codex chat on one computer to a chat on another, and see when the recipient reads it.** Paste a chat link or use a nickname. Riftwire runs on the two computers using SSH and its own mailbox. It works independently of any memory system.

## Give this to Codex on both computers

Open one chat on each computer. Paste this prompt into both and add the GitHub link to this page at the end.

> Install Riftwire for my user account from the GitHub link below. Follow its AGENTS.md and pair this chat with the setup chat on my other computer using my existing SSH connection. I authorize one test message in each direction and both read receipts. Show me the results.

One computer needs to accept SSH and run Linux or macOS. The other can run Windows, Linux, or macOS. Both need Python 3.9+ and a native Codex CLI with the `queue` command. If Codex asks which SSH connection to use, give it the alias you already use to reach the mailbox computer. The test can start in either chat once the hub is installed. [Installation guide](docs/SETUP.md)

## Use it in a chat

| What you want | What to tell Codex |
| --- | --- |
| Send to a specific chat | “Send ‘The build passed’ with Riftwire to this chat.” Paste its `codex://threads/…` link. |
| Give a chat a nickname | “Give this chat the Riftwire alias `review` and the shared title `Build review`.” |
| Read and acknowledge | “Read my Riftwire inbox and acknowledge the message about the build.” |
| Check a handoff | “Check the read receipt for the message I just sent.” |

A chat link works on the first send. A short alias or unique shared title works once its owner enrolls it. The contacts list contains only chats introduced to Riftwire.

## How delivery works

The **hub** is the computer that accepts SSH. It stores messages and receipts in SQLite. The **peer** opens the SSH connection and can send and receive through it. Each computer runs a local Riftwire MCP server and its own Codex CLI. The native notice contains routing information. The recipient gets the message text from its Riftwire inbox.

| Receipt | What happened |
| --- | --- |
| `pending` | The hub saved the message. Its native notice is waiting for pickup. |
| `queued` | The receiving Codex CLI accepted the notice. |
| `queued_with_warning` | Codex accepted the notice and reported a warning. Open the recipient chat to check it. |
| `seen` timestamp | The recipient explicitly acknowledged reading the message. |

The hub retains accepted messages while the peer is asleep. With the default settings, an idle chat can read its inbox when next opened. The recipient may opt in to waking its chat on arrival. An existing OS login on each computer is the trust boundary. [Setup, storage, and troubleshooting](docs/SETUP.md)

## Build and license

The runtime is one Python file and uses only the standard library. Run the tests with `python -B -m unittest discover -s tests -v`. Build the release ZIP and its SHA-256 manifest with `python -I -B scripts/build_release.py`. Riftwire is available under the [MIT license](LICENSE).
