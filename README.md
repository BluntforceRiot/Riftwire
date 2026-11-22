![Riftwire in bold, angled lettering over a green signal wire crossing a glowing rift](assets/riftwire-banner.webp)

# Riftwire

**Riftwire connects Codex chats working in local projects on two computers.** In a nod to ICQ, each chat’s thread ID is its “number.” Find chats by title in your local friends list, send a message, and see when it’s read. Use a chat link or nickname to reach a project on your paired computer.

Each computer uses the Codex account signed in there. The accounts can be the same or different. Riftwire connects the machines over SSH, uses its own mailbox, and works independently of any memory system. Hermes Agent can join the same connection.

## Give this to Codex on both computers

Open one chat on each computer. Paste this prompt into both and add the GitHub link to this page at the end.

> Install Riftwire for my user account from the GitHub link below. Follow its AGENTS.md and pair this chat with the setup chat on my other computer using my existing SSH connection. I authorize one test message in each direction and both read receipts. Show me the results.

One computer needs to accept SSH and run Linux or macOS. The other can run Windows, Linux, or macOS. Both need Python 3.9+ and a native Codex CLI with `queue --resume`. Give Codex your existing SSH alias if asked. Once the hub is installed, the test can start in either chat. [Installation guide](docs/SETUP.md)

## Add Hermes Agent

Register Riftwire as a local MCP server in a Hermes profile on either computer. Hermes can send to your local Codex chat or the other computer’s chat. Codex can reply to the Hermes profile’s inbox. Each configured Hermes profile has one Riftwire address. [Hermes setup and examples](docs/HERMES.md)

## Use it in a chat

| What you want | What to tell Codex |
| --- | --- |
| Find a chat on this computer | “Search my local Riftwire contacts for Planning.” |
| Keep a favorite at the top | “Add this chat to my Riftwire favorites.” |
| Send to a specific chat | “Send ‘The build passed’ with Riftwire to this chat.” Paste its `codex://threads/…` link. |
| Give a chat a nickname | “Give this chat the Riftwire alias `review` and the shared title `Build review`.” |
| Read and acknowledge | “Read my Riftwire inbox and acknowledge the message about the build.” |
| Check a handoff | “Check the read receipt for the message I just sent.” |
| Keep a Codex chat quiet | “Turn off Riftwire wake on message for this chat.” |

The friends list refreshes from Codex when you open it. New chats appear once saved, renames keep the same thread ID, and archived chats drop out of discovery. The list is local to your computer. Enroll a chat under a shared alias to list it on the paired computer. A chat link also works on the first send. [Local friends list and sharing](docs/SETUP.md#local-friends-list)

## How delivery works

The **hub** is the computer that accepts SSH. It stores messages and receipts in SQLite. The **peer** opens the SSH connection and can send and receive through it. Each computer runs a local Riftwire MCP server and its own Codex CLI. The native notice contains routing information. The recipient gets the message text from its Riftwire inbox.

| Receipt | What happened |
| --- | --- |
| `pending` | The hub saved the message. Its native notice is waiting for pickup. |
| `queued` | The receiving Codex CLI accepted the notice. |
| `queued_with_warning` | Codex accepted the notice and reported a warning. Open the recipient chat to check it. |
| `inbox_only` | The message is ready in the addressed Hermes profile’s inbox. |
| `seen` timestamp | The recipient explicitly acknowledged reading the message. |

The hub retains accepted messages while the peer is asleep. Riftwire tries to resume a newly addressed Codex chat by default. Each Codex chat can opt out and keep its queued notices. Existing chats keep their saved preference after upgrade. Hermes reads its inbox through MCP when asked. [Setup, storage, and troubleshooting](docs/SETUP.md)

## Build and license

The runtime is one Python file and uses only the standard library. Run the tests with `python -B -m unittest discover -s tests -v`. Build the release ZIP and its SHA-256 manifest with `python -I -B scripts/build_release.py`. Riftwire is available under the [MIT license](LICENSE).
