# Build order for a real-time collaborative document editor

I ordered these phases so each one rests on the one before it, and so the riskiest decisions come first.

## Phase 0: Decisions that are expensive to change later
1. **Scope the document model.** Choose plain text, rich text, or structured blocks. Rich text with blocks is the usual target.
2. **Choose the concurrency algorithm.** You can use CRDTs (Yjs, Automerge, Loro) or OT (ShareDB, or a custom server-authoritative design).
   - CRDTs are easier for offline support and peer-to-peer. The cost is metadata growth.
   - OT with a central server is leaner, but the transform logic is harder to get right.
   - Adopting an existing library is usually better than writing your own.
3. **Choose the editor framework.** ProseMirror, TipTap, Lexical, Slate, or CodeMirror for code. Pick one with a maintained binding to your algorithm, such as y-prosemirror.
4. **Set non-functional targets.** These are the maximum concurrent editors per document, the latency budget (typically under 100 ms perceived), offline needs, and document size limits.

## Phase 1: Single-user editor (no networking)
5. Build the document schema: paragraphs, headings, lists, marks, and so on.
6. Build the editing basics: input, selection, undo/redo, clipboard, and keyboard shortcuts.
7. Build the toolbar and formatting commands.
8. Add local persistence, such as IndexedDB or a plain save endpoint.

*Milestone: a usable offline editor.*

## Phase 2: Core sync engine
9. Bind the editor state to the CRDT/OT document model, so every edit becomes an operation or update.
10. Build a minimal sync server. A WebSocket server relays updates between two clients on one document.
11. Add the initial sync handshake (state vector or snapshot exchange), then incremental updates.
12. Handle reconnection and resync, and make applying an update idempotent.
13. Write convergence tests. Run randomized concurrent edits against the model and check that all replicas end up identical. **Do this before adding features.**

*Milestone: two browsers edit the same document and converge.*

## Phase 3: Persistence and server architecture
14. Store updates durably: an append-only log plus periodic snapshots and compaction.
15. Route each document to one server instance, or use a pub/sub layer such as Redis so multiple servers can share a document.
16. Load documents on demand and unload them when idle.
17. Add the document metadata database: title, owner, timestamps, and a list of documents.

## Phase 4: Identity and access
18. Add authentication.
19. Add authorization: owner, editor, commenter, and viewer roles. Enforce them on the WebSocket, not just in the UI.
20. Add share links and invitations.

*Put this before the social features, because presence and comments depend on knowing who the user is.*

## Phase 5: Collaboration experience
21. Presence: who is online, with colored cursors and selections (awareness protocol).
22. Offline editing with a local queue, and a clear "syncing / offline / saved" indicator.
23. Undo/redo scoped to each user's own changes. This is a common bug source.
24. Comments and suggestions, anchored to relative positions so they survive concurrent edits.

## Phase 6: History and recovery
25. Version history: named snapshots, diffing, and restore.
26. Activity and audit log.
27. Export and import (Markdown, PDF, DOCX).

## Phase 7: Hardening
28. Load and soak tests: many clients on one document, large documents, and flaky networks.
29. Security: rate limiting, input and update validation, payload size caps, and protection against malicious or malformed updates.
30. Observability: metrics for sync latency, connection counts, and update sizes, plus error tracking.
31. Performance work: lazy rendering of large documents, update batching, and garbage collection of CRDT metadata.
32. Accessibility and mobile or IME input (composition events often break collaborative editors).

## Phase 8: Polish and scale
33. Slash commands, embeds, tables, and images with an upload pipeline.
34. Search across documents, plus notifications and mentions.
35. Horizontal scaling, multi-region deployment, and backups and disaster recovery drills.

## Suggested team split
- **Track A (client):** 5–8, 21, 23, 24
- **Track B (sync and backend):** 9–17
- **Track C (platform):** 18–20, 28–30

Phase 0 and Phase 2 carry the most risk. If you can't get the convergence tests in step 13 passing, nothing after it is reliable.

If you tell me your stack, team size, and whether you need offline support, I can turn this into a concrete timeline or a specific tech stack.
