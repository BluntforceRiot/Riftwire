# Riftwire 2.0 roadmap

## Required for release · guided setup

Provisioning a new boot must take one guided setup. Once the hub and peer have the release, their installers exchange pairing data, start the receiver, and run an end-to-end check. The operator can see the result in either setup chat.

1. **Check the existing installation first.** Verify the signed package, installed hub version, supported Codex CLI, and saved outbound SSH route. Back up the mailbox before an upgrade. Report the exact prerequisite when setup is blocked and resume from that stage when it is met.
2. **Establish trust once.** Use an existing authenticated hub-owner SSH connection and verified host pin when available. For a new route, pass the peer one short-lived invitation with the hub address, verified host key, destination chat, and authorized device name. Use the existing SSH service for authentication and enrollment. The user enters any required credentials in the native SSH prompt. The installers exchange the public key and approval over that connection.
3. **Install and start the receiver.** Generate a dedicated key on each boot and register it with the hub's restricted Riftwire command. Keep the saved Windows address and mailbox history when adding a Debian boot on the same PC. Start per-user pickup where supported and keep the receiver ready across SSH reconnects. Preserve the operator's wake preference.
4. **Prove delivery.** Exchange one test message and receipt in each direction and report the exact destination and test message IDs. Distinguish delivery to Codex's queue from a read acknowledgment. Resume the same setup after an interruption so the installers keep their identities, approvals, and test results. Let the operator retry a selected queued message to a verified new address with a link to the original receipt.

### Release checks

- Give Codex the release link on two real machines and complete first pairing with at most one invitation transfer. The setup transfers the pairing JSON, host pin, and test data between the peers.
- Upgrade an existing 0.4.x or 1.x hub, pair the second boot of a dual-boot PC, and verify both boots with their own keys at the same hostname and IP. Switch boots with mail pending and verify the correct chat receives it after reconnecting.
- Interrupt setup after enrollment and resume the same pairing. Verify normal delivery after restarting the receiver and confirm both read receipts from the native Codex chats.
- Run the setup from ordinary user accounts using the existing SSH listener on the hub. Keep the window for any required initial authentication on the machine where the operator enters it.

## Project workflows

The 2.0 development build includes federated workflow invitations, on-demand Codex workers, parallel patches, attempt recovery, and independent release verification. Test the native Windows worker and the live optional Continuum import as part of release qualification. [Project workflows](PROJECTS.md) · [Continuum handoff](CONTINUUM.md)
