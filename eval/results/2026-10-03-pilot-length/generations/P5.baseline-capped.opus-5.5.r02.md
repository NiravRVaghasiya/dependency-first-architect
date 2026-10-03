# Build Order: Real-Time Collaborative Document Editor

The main idea behind this order is to pick the concurrency model first and keep everything else as simple as possible until two clients can reliably end up with the same document. Each phase depends on the ones before it.

---

## Phase 0: Foundational decisions (before writing code)

1. **Pick the concurrency model.** This is the most important choice because most later work depends on it.
   - **CRDT** (Yjs, Automerge, Loro): works peer-to-peer and offline, and the server can be simple. This is the usual choice for new projects.
   - **OT (Operational Transformation)**: needs a central server to order changes. It's well proven (Google Docs) but harder to get right.
   - *Recommendation:* use **Yjs** unless you have a specific reason not to.
2. **Define the document model.** Decide between plain text, rich text (blocks, marks, embeds) or structured content (tables, nested lists). Write a schema.
3. **Set the scope limits.** Decide the most people editing one document at once, the largest document size, whether offline editing is required, and the target delay before others see an edit (for example, under 200 ms).
4. **Choose the editor framework.** ProseMirror/Tiptap, Lexical, Slate, or CodeMirror for code. Make sure it has a well-maintained binding to your CRDT (for example, y-prosemirror).

**Exit criteria:** written decision records for the concurrency model, schema and editor framework.

---

## Phase 1: Single-user editor

5. Build the editor UI using your schema: formatting, lists, headings, undo/redo.
6. Bind the editor to the CRDT document on one machine, so every edit passes through the CRDT even with only one user.
7. Save the CRDT state locally (IndexedDB) so it survives a page reload.

**Why first:** this tests the schema and the editor binding without network problems mixed in.

**Exit criteria:** one user can edit, reload, and lose nothing.

---

## Phase 2: Getting clients to agree (the core)

8. **Test merging with no network.** Create two CRDT documents in one process, apply random edits to both at the same time, swap the updates, and check that both end up identical. Use fuzz/property-based tests.
9. **Choose the transport.** WebSocket is the default. WebRTC is optional for peer-to-peer.
10. **Build a relay server.** It sends updates to every client on the same document. At this stage it doesn't need to understand the content.
11. **Write the sync protocol:**
    - When a client connects, it swaps state summaries with the server and receives only the changes it's missing.
    - After that, updates stream in both directions as they happen.
12. **Reconnect logic:** retry with growing wait times, re-sync after reconnecting, and queue edits made while offline.

**Exit criteria:** two browsers editing the same paragraph at once always end up with the same text, including after network drops you trigger on purpose.

---

## Phase 3: Server-side storage

13. **Store documents on the server.** Keep a log of updates and periodically compress it into a snapshot. Use Postgres (bytea) or object storage for snapshots, and optionally Redis for recent updates.
14. **Load documents.** When the first client opens a document, the server loads the latest snapshot plus any later updates.
15. **Compact and clean up.** Merge old updates into a new snapshot so load times don't keep growing.
16. **Guarantee durability.** Confirm an update to the client only after it's safely stored. Decide how much data loss is acceptable if the server crashes (for example, at most 1 second of edits).

**Exit criteria:** restarting the server loses no confirmed edits.

---

## Phase 4: Identity and access

17. **Login:** sign-in with OAuth/OIDC, and authenticate the WebSocket connection itself with a token checked when the connection is set up.
18. **Permissions:** owner, editor, commenter and viewer roles per document. **Enforce them on the server**: reject updates from users who only have read access.
19. **Sharing:** invite links, link visibility settings, revoking access (this must disconnect people who are currently connected).
20. **Document list:** create, rename, delete, browse documents.

**Why here and not earlier:** auth wraps the sync channel. Adding it before sync works makes debugging harder. Adding it after collaboration features means rewriting them.

**Exit criteria:** someone without access can't read or change a document, even by sending messages directly to the server.

---

## Phase 5: Seeing other people (presence)

21. **Presence channel:** cursor positions, selections, user name and color. Keep this separate from the document so it's never saved (Yjs Awareness does this).
22. **Show other people's cursors and selections**, the list of avatars of who's online, and "user is typing" indicators.
23. **Cursor positions that stay correct:** anchor cursors to CRDT-relative positions so they don't jump when someone else edits.

**Exit criteria:** cursors stay in the right place while several people type at once.

---

## Phase 6: Editing features users expect

24. **Per-user undo/redo.** Undoing should reverse only *your* changes, not your collaborators' (Yjs UndoManager with tracked origins).
25. **Copy/paste:** clean up pasted content from Word, Google Docs and HTML so it fits your schema.
26. **Embeds and images:** upload to object storage and keep only a reference in the document.
27. **Comments and suggestions:** comment threads anchored to a range of text using relative positions, plus a suggestion (track changes) mode if needed.

---

## Phase 7: History and recovery

28. **Version snapshots:** automatic and named versions, and a timeline view.
29. **Compare and restore:** show differences between versions and restore an old version *as a new change* (never rewrite history).
30. **Track who wrote what:** optionally record which user made each change, for "who wrote this" views.

---

## Phase 8: Scaling

31. **Running more than one server:** make sure all clients editing a document reach the same server (consistent hashing, keyed by document ID), *or* share updates between servers with Redis pub/sub or NATS.
32. **Backpressure:** batch and group updates, and limit traffic from noisy clients.
33. **Large documents:** load content lazily, split very large documents, and set a size limit.
34. **Load testing:** simulate N clients per document and M documents per server, and measure p99 delay and memory use.

**Exit criteria:** the system meets the targets from Phase 0 at 2–3× the expected peak load.

---

## Phase 9: Production readiness

35. **Monitoring:** delay from an edit to other clients seeing it, how often clients reconnect, sync errors, compaction lag, and an alert if two clients' copies ever disagree.
36. **Security hardening:** validate incoming updates against size and schema limits, rate-limit, protect against XSS in rendered content, and encrypt data in transit and at rest.
37. **Backups and disaster recovery:** back up snapshots, practice restoring them, and add per-document export (Markdown, DOCX, PDF).
38. **Accessibility:** screen reader announcements for other people's edits, keyboard navigation, and color contrast for cursor colors.
39. **Mobile and slow networks:** responsive layout and testing on high-delay connections.

---

## Phase 10: Optional extras (only after the core is stable)

- Offline-first for desktop/mobile (Electron or native wrappers)
- AI features (summarize, rewrite) applied as normal CRDT changes
- End-to-end encryption (CRDTs make this possible, since the relay can forward encrypted updates without reading them)
- Live comment notifications, @mentions, email digests
- Public API and webhooks

---

## Summary: dependency chain

```
Decisions → Single-user editor → Clients agree (no network → network)
   → Server storage → Auth/permissions → Presence
   → Editing features → History → Scaling → Production readiness
```

## Three rules for the whole build

1. **Don't build features on top of sync you haven't proven.** Phase 2's fuzz tests are the base everything else depends on. Keep them running in CI from then on.
2. **Keep presence separate from document data.** Mixing them makes the document bigger and leaks temporary state into the saved history.
3. **The server enforces access, the client only displays it.** The CRDT will accept any update, so the server must check permissions and validate content before forwarding anything.
