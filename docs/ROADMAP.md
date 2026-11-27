# Riftwire 2.0 roadmap

## Required for release · guided setup

Provisioning a new boot must take one guided setup. Once the hub and peer have the release, their installers exchange pairing data, start the receiver, and run an end-to-end check. The operator can see the result in either setup chat.

The development build implements the invitation, enrollment, receiver setup, two-way check, and linked retry. [Guided setup](SETUP.md#guided-setup-in-20)

1. **Check the existing installation first.** Verify the signed package, installed hub version, supported Codex CLI, and saved outbound SSH route. Back up the mailbox before an upgrade. Report the exact prerequisite when setup is blocked and resume from that stage when it is met.
2. **Establish trust once.** Use an existing authenticated hub-owner SSH connection and verified host pin when available. For a new route, pass the peer one short-lived invitation with the hub address, verified host key, destination chat, and authorized device name. Use the existing SSH service for authentication and enrollment. The user enters any required credentials in the native SSH prompt. The installers exchange the public key and approval over that connection.
3. **Install and start the receiver.** Generate a dedicated key on each boot and register it with the hub's restricted Riftwire command. Keep the saved Windows address and mailbox history when adding a Debian boot on the same PC. Start per-user pickup where supported and keep the receiver ready across SSH reconnects. Preserve the operator's wake preference.
4. **Prove delivery.** Exchange one test message and receipt in each direction and report the exact destination and test message IDs. Distinguish delivery to Codex's queue from a read acknowledgment. Resume the same setup after an interruption so the installers keep their identities, approvals, and test results. Let the operator retry a selected queued message to a verified new address with a link to the original receipt.

### Release checks

- Give Codex the release link on two real machines and complete first pairing with at most one invitation transfer. The setup transfers the pairing JSON, host pin, and test data between the peers.
- Upgrade an existing 0.4.x or 1.x hub, pair the second boot of a dual-boot PC, and verify both boots with their own keys at the same hostname and IP. Switch boots with mail pending and verify the correct chat receives it after reconnecting.
- Interrupt setup after enrollment and resume the same pairing. Verify the receiver at the next OS login and confirm two-way delivery with the native Codex chats.
- Run the setup from ordinary user accounts using the existing SSH listener on the hub. Keep the window for any required initial authentication on the machine where the operator enters it.

### Native delivery verification

On Linux, the packaged verifier uses two disposable Codex profiles, a pinned OpenSSH connection, and a loopback model fixture. Use a regular login with OpenSSH, `ip`, and Python with `websockets` 15 or newer.

```sh
unshare --user --map-current-user --keep-caps --net \
  sh -c 'ip link set lo up && python3 -I -B scripts/verify_native_linux.py --codex "$1"' \
  sh "$(command -v codex)"
```

The verifier checks that both idle chats start a new turn containing the exact Riftwire notice. It also checks both read receipts and counts the temporary owner SSH logins. A native Codex turn can complete even when the queue reports a warning.

The Windows CI check exercises the native chat directory, project worker, and per-user Task Scheduler registration. Check its scheduler launch result on an interactive Windows login. During the dual-boot check, send a unique message to an idle chat on each boot and let the receiving chat read and acknowledge it. Record the message ID, the intended boot and chat, the new native turn, and the read receipt.

## Project workflows

The 2.0 development build includes federated workflow invitations, on-demand Codex workers, parallel patches, attempt recovery, and independent release verification. Verify the optional signed archive import when the Continuum adapter is available. [Project workflows](PROJECTS.md) · [Continuum handoff](CONTINUUM.md)
