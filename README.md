![Riftwire in bold, angled lettering over a green signal wire crossing a glowing rift](assets/riftwire-banner.webp)

# Riftwire

**Riftwire connects Codex chats and Hermes profiles across your computers.** In a nod to ICQ, each Codex chat’s thread ID is its “number.” Find a chat in your friends list, send it a message, and see when it’s read. Assign a project handoff and follow it through completion.

Each computer uses the Codex account signed in there. The accounts can be the same or different. Riftwire connects the machines over SSH, uses its own mailbox, and works independently of any memory system. Hermes Agent can join the same connection.

## Give this to Codex on both computers

Open one chat on each computer. Paste this prompt into both and add the GitHub link to this page at the end.

> Install Riftwire for my user account from the GitHub link below. Follow its AGENTS.md and pair this chat with the setup chat on my other computer using my existing SSH connection. I authorize one test message in each direction and both read receipts. Show me the results.

The hub accepts SSH and runs Linux or macOS. Peers can run Windows, Linux, or macOS. Each needs Python 3.9+ and a native Codex CLI with `queue --resume`. Pair each installation with its own key. A dual-boot PC can use a different Riftwire address for each boot and keep each boot’s mail queued until it connects. [Installation guide](docs/SETUP.md)

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
| Track work | “Ask the Debian build chat to run the project tests and report its handoff status.” |
| Post a project update | “Post the build results in our Riftwire room.” |
| Keep a Codex chat quiet | “Turn off Riftwire wake on message for this chat.” |

The friends list refreshes from Codex when you open it. New chats appear once saved, renames keep the same thread ID, and archived chats drop out of discovery. The list is local to your computer. Enroll a chat under a shared alias to list it on the paired computer. A chat link also works on the first send. [Local friends list and sharing](docs/SETUP.md#local-friends-list)

Give a receiving chat standing instructions for recurring requests, such as running tests for a paired project chat and sending back the results. It can use that scope for each handoff without asking again. [Routine handoffs](docs/SETUP.md#routine-handoffs)

Open the desktop inbox with `riftwire desktop hub` or run `riftwire.py desktop peer` through the peer’s Python interpreter. It groups friends by device and shows received messages, sent messages, and handoff results. [Desktop inbox](docs/SETUP.md#desktop-inbox)

## How delivery works

The **hub** accepts SSH and stores messages and receipts in SQLite. Each **peer** opens its own SSH connection and receives mail addressed to its installation. The native notice contains routing information. The recipient gets the message text from its Riftwire inbox. Project room updates wait quietly in members’ inboxes.

| Receipt | What happened |
| --- | --- |
| `pending` | The hub saved the message. Its native notice is waiting for pickup. |
| `queued` | The receiving Codex CLI accepted the notice. |
| `queued_with_warning` | Codex accepted the notice and reported a warning. Open the recipient chat to check it. |
| `inbox_only` | A Hermes message or room update is ready in its Riftwire inbox. |
| `seen` timestamp | The recipient explicitly acknowledged reading the message. |

The hub retains accepted messages while the peer is asleep. Riftwire tries to resume a newly addressed Codex chat by default. Each Codex chat can opt out and keep its queued notices. Existing chats keep their saved preference after upgrade. Hermes reads its inbox through MCP when asked. [Setup, storage, and troubleshooting](docs/SETUP.md)

## Project workflows in development

The 2.0 preview coordinates work across independent Riftwire installations. Invite a worker into one project, share a workflow file, and collect signed results for the same Git revision. [Project workflow guide](docs/PROJECTS.md)

## Build and license

The runtime is one Python file and uses only the standard library. Run the tests with `python -B -m unittest discover -s tests -v`. Build the release ZIP and its SHA-256 manifest with `python -I -B scripts/build_release.py`. Riftwire is available under the [MIT license](LICENSE).
