# QQ 9.9.33 VM live adaptation

Status: API and RulePack activation are verified; real QQ end-to-end automation is not complete and automatic replies remain disabled. The user-authorized product scope is all one-to-one direct chats, including temporary sessions; group chats are excluded and friend requests are never auto-accepted. Historical friend-only wording and historical all-gates-before-composer wording describe prior experiments, not current hard gates.

Local nonce write/read testing may proceed independently once the guest has one unique QQ window, a guarded in-scope input, an empty-composer precondition, and focus/scope guards. A real Send still requires peer and conversation-type verification, readback, revision revalidation, and the existing transaction checks.

## Current gap

`vm_driver/transport.py` assumes facts that fixtures can provide but a Chromium-based QQ client may not expose through UIA:

- conversation rows must have a non-empty `AutomationId`; otherwise `list_conversations` drops them;
- `sha256(AutomationId|ClassName)` is treated as participant identity, although it does not identify a person and may drift or collide;
- conversation selection requires `SelectionItemPattern`, composer access requires `ValuePattern`, and sending requires `InvokePattern`;
- message direction depends on UIA `ClassName` containing `container--self`, `container--other`, or `container--peer`;
- absent message IDs fall back to `sha256(ClassName|text)`, which collides for repeated identical messages;
- exact ancestor `AutomationId` suffixes and a whole-tree digest are sensitive to unread badges, message content, timestamps, virtualization, and client updates.

Until the live probe establishes these properties, `runtime.example.json` placeholders must remain placeholders and capability fields must remain unsupported.

## Existing parts to reuse

The existing read-only stack already supplies most of the safety and evidence contracts:

1. `qq_uia_probe_helper/Program.cs` enumerates real UIA pattern availability, redacted topology, normalized geometry, window/process facts, current-chat identity candidates, message candidates, profile evidence, and avatar discovery. It emits no raw profile identifier in the identity report and already separates read-only discovery from authorized selection.
2. `live_driver/probe_bridge.py` validates probe reports and converts redacted nodes into `UiNodeInput` records with trusted semantic anchors.
3. `live_driver/topology.py` ranks roles using control type, supported patterns, class, normalized region, and ancestor control-type structure. `Q1SelectorPack.structural_signature` excludes transient runtime IDs.
4. `live_driver/environment.py` binds evidence to the signed executable, process/window signature, exact HWND, client version, presentation, modal state, and environment fingerprint.
5. `live_driver/identity.py` provides human confirmation, versioned bindings, conflict/quarantine behavior, audit entries, and resolution that requires an evidence set rather than a display name.
6. `live_driver/profile_identity.py`, `avatar_identity.py`, and `wgc_avatar.py` provide privacy-checked second-signal evidence. They can support a binding only when the corresponding live capture is unique and stable; they must not manufacture a signal when QQ exposes none.
7. `live_driver/uia_capture.py` and `observation.py` scope captures to one exact HWND, require identity evidence from that same scope, attach confidence to direction/time, quarantine uncertainty, and deduplicate by both watermark and source-evidence hash.
8. `live_driver/front_half.py` already composes Q0 environment, Q1 topology, Q2 observation, and Q3 identity into a fail-closed read-only assessment.

The old `vm_driver` models and send transaction can remain the runtime-facing boundary, but they should consume certified artifacts from this stack instead of independently inferring identity from raw UIA IDs.

## Small implementation modules

### 1. Live probe artifact

Input: one logged-in, modal-free QQ 9.9.33 window in the guest.  
Output: the existing sanitized probe JSON plus client executable/version/signature and exact HWND scope.

Run the existing read-only probe with topology enabled. The first-session gate captures the conversation list at rest, one candidate from the already authorized visible test scope selected, and the same state after scroll away/back. Do not restart QQ merely to complete this first gate or force the user to scan again. A normal QQ restart is a later persistence gate; until it passes, any accepted selector or binding is explicitly limited to the current QQ session. No typing or send action is part of this module.

Acceptance:

- every report passes `ingest_probe_report`;
- Q0 identifies exactly one signed QQ window;
- raw message text, raw QQ number, API key, and raw profile identifier are absent from persisted topology;
- role mappings report `unique`, `ambiguous`, or `not_found` honestly.

### 2. Executable selector adapter

Input: a reviewed `Q1SelectorPack`.  
Output: a guest-only selector evaluator for the exact QQ version/environment.

Match control type, actual pattern support, class when stable, normalized region, and ancestor control-type chain. Treat runtime IDs and bounding rectangles as current-session locators only. Do not require `AutomationId`; use its digest only as optional evidence. The adapter must return absent/ambiguous instead of choosing the first match.

Acceptance fixtures should cover empty and duplicate automation IDs, virtualized rows, reorder/unread changes, one QQ restart, ambiguous controls, and client-version mismatch.

### 3. Conversation binding capture

Input: a candidate row from the user-authorized visible test scope and a read-only current-chat capture.  
Output: a pending binding application containing exact HWND/environment/selector-pack scope plus at least one stable second identity signal.

Preferred second signals are a privacy-preserving HMAC of an explicit profile identifier or a stable avatar HMAC captured through the existing profile/avatar modules. A display name, row position, `AutomationId`, or header text alone is insufficient. The planned startup batch may promote only contacts with authenticated evidence tied to the current account/session and runtime locator; contacts without sufficient evidence remain paused. This replaces the earlier per-contact manual-confirmation wording.

Binding capture must also classify the conversation as one-to-one or group before promotion. The production `QQIdentityBinding` now carries `conversation_type` and `friendship_verified`, while legacy/offline construction defaults remain permissive. A one-to-one personal RulePack cannot be attached to a group title as if it identified one person. The approved product scope is QQ friend direct messages only; group candidates remain excluded and paused. Initial runtime acceptance therefore requires a certified direct candidate with a stable second identity signal.

Historical profile-window experiments were marked NO-GO because opening QQ details could steal the host foreground and `WindowPattern.Close` did not reliably restore the prior state. The dedicated VM changes the foreground premise: taking the *guest* foreground is allowed. The existing title-invoke/profile-number path may therefore be reassessed as a second signal, but only after a live test proves that it opens the intended profile, captures the identifier privately, closes the exact transient window, and restores the original conversation and HWND scope. The former “must stay non-foreground” condition must not be copied into the VM profile as a blanket restriction; the recovery failure remains a real blocker until retested.

First-session acceptance requires the same signal after reselection, a negative test against another visible contact, an evidenced conversation type, no raw identifier in the artifact, and registry quarantine on any signal or scope drift. Restart stability is a later persistence acceptance test; until it passes, the binding expires with the QQ session and must not be described as permanent. If QQ exposes no stable second signal or cannot distinguish one-to-one from group scope, report `binding_not_certifiable`.

### 4. Read-only message normalization

Input: exact-HWND current-chat reports plus the certified binding evidence.  
Output: `VisibleConversationSnapshot` and `ObservationBatch` through `uia_capture.py` and `observation.py`.

Direction must come from live structural/geometry evidence with confidence, not CSS-like class substrings. A message watermark must distinguish repeated identical text using structural/runtime evidence local to the capture; it is not a platform message ID. Unknown direction, time, conversation, or low confidence remains quarantined.

Acceptance covers repeated equal text, inbound/outbound/system/time rows, virtualized history gaps, duplicate captures, selected-contact drift, and HWND replacement. This gate performs no composer access.

### 5. Foreground write capability gate

Only after modules 1–4 pass should a separate probe assess the real composer and send button patterns. `ValuePattern`, `TextPattern`, `InvokePattern`, or another mechanism may be accepted only from observed QQ 9.9.33 behavior and with readback. No fallback should be guessed into configuration.

If the live client has no writable `ValuePattern`, a guest-keyboard fallback can be considered only inside the certified VM, with the exact bound QQ HWND in the guest foreground, an empty composer precondition, independent readback, target and revision revalidation, and the existing prepare/commit/verify idempotency checks. It cannot use host desktop input and cannot weaken identity checks. This remains an unimplemented candidate until the topology and controlled composer probe establish the need.

Acceptance must prove unique composer resolution, empty/readback behavior, unique send control resolution, pre-send target revalidation, and post-send receipt verification within the already authorized visible test scope. No extra confirmation step is required merely to continue that controlled validation after the prerequisite gates pass. Runtime sending remains disabled while identity or capability evidence is incomplete, and broad automatic replies remain disabled until the product configuration explicitly enables them.

## Live fields required now

The onsite read-only capture should retain or safely digest:

- executable path, Tencent signature result, file version, process ID, HWND, window class and presentation;
- per-role `ControlType`, `AutomationId` presence/uniqueness, class, parent chain, normalized rectangle, enabled/offscreen state;
- supported SelectionItem, Selection, Invoke, Value, Text, LegacyIAccessible, Scroll, and ScrollItem patterns;
- candidate-row stability across select/unselect, scroll, unread changes, reorder, and restart;
- selected-header/profile/avatar second-signal availability and uniqueness;
- message-region structure for inbound, outbound, system and timestamp nodes, including repeated equal text;
- composer and send pattern availability, recorded only as candidate capability evidence.

Any field not observed in the real probe remains unknown. It must not be copied from a fixture or inferred from the screenshot.

## First live topology evidence

The initial occluded, non-maximized capture exposed only nine generic panes and no patterns. A second capture recorded by the probe itself as foreground, maximized, and unoccluded exposed 103 nodes: 43 Invoke, 21 Text, 2 Value, and 1 Scroll patterns. This shows that foreground presentation activated the useful Chromium accessibility tree; the optional UIA-event warmup is not needed for the next gate.

The foreground capture still does not certify a selector pack. It found a unique `Pane` whose class contains `recent-contact-list`; that Pane itself reports no patterns. Its three visible direct-child `Group` rows have class `recent-contact-item` and `InvokePattern`. A separate node accounts for the report's one Scroll pattern. The rows have no `AutomationId` and no `SelectionItemPattern`. The original topology compiler misses them because its list role accepts only List/ListBox/Tree/Table and its item role accepts only ListItem/DataItem/TreeItem. The current `vm_driver` also cannot select them because it requires `AutomationId` plus `SelectionItemPattern`.

No conversation was selected during this capture: the right side was an empty panel. The absence of composer and send candidates therefore says nothing yet about their live pattern support. The probe's `is_logged_in=false` is likewise a heuristic false negative caused by the incomplete chat-shell signals; the stored report must remain unchanged rather than being edited to match the screenshot.

The next evidence step is one authorized candidate selection followed by another read-only capture. Only then can the `recent-contact-list`/`recent-contact-item` structures be assessed together with the selected header, conversation type, second identity signal, message region, composer, and send control.

The first selected-candidate attempt showed that a generic main-window lookup can encounter a transient QQ announcement window: that report was foreground but not maximized, had modal state `unknown`, and exposed only 26 nodes. Runtime capture therefore verifies the exact window before reading or writing. The current metadata capture found a unique visible/maximized/foreground `QQ.exe` `Chrome_WidgetWin_1` target (PID 7352, HWND 393974) after a 9-second wait; the earlier timeout was caused by Edge being foreground. This metadata does not certify a private conversation or send capability.

A later hidden, bounded capture waited for the verified QQ PID/HWND to remain foreground and maximized for one second before sampling. It succeeded after 24.824 seconds and exposed 438 nodes. The selected row adds the `recent-contact-item--selected` class token to the base `recent-contact-item` token, so row matching must use a required token instead of exact whole-class equality. The selected conversation is structurally a group: its right pane contains a `group-member-list` scroll pane and repeated member rows. This is evidence for conversation-type classification, not permission to map the group title to a personal contact.

The message hierarchy is an outer `chat-msg-area` Group, an inner `chat-msg-area__vlist` Pane, then one `q-scroll-view ... ml-container ml-root container` Group with Invoke and Scroll patterns. Repeated `message` Groups descend from that inner scroll Group. The 9.9.33 adapter binds `message_region` only to this unique, version-bound inner container; the broad heuristic anchors on the outer shell and unrelated right-side regions are not accepted. The composer is a ProseMirror Group with TextPattern only; it has no ValuePattern, so this read-only report does not establish a writable composer. The send control exposes InvokePattern, but sending remains disabled until identity, type, readback, and idempotency gates pass.
