> **P5** · **without the skill** (cold: plain `claude --bare -p`, no skill, empty directory)  
> Prompt: *"Order the build of a real-time collaborative document editor."*  
> Generated 2026-10-02 with `claude-opus-5-5` (effort max) in an isolated session. One run per arm, not cherry-picked.  
> Everything below the line is the model's output, verbatim. Judged score: [SCORECARD.md](../SCORECARD.md).

---

For a real-time collaborative editor, settle the hard-to-reverse decisions first. Then prove that edits made at the same time merge correctly before building anything on top. Get a thin end-to-end version working early, and add features only once the things they depend on exist.

### Phase 0: Decide before writing code
- **How edits get merged:** a CRDT (Yjs, Automerge) or OT/a central server that orders edits (ShareDB, prosemirror-collab). Use Yjs by default. Pick OT only if you want one authoritative server and don't need offline editing.
- **Editor framework:** ProseMirror/Tiptap, Lexical or Slate. Choose one that already connects to your sync library.
- **Document schema:** the block types, text formatting and embeds a document can contain. If you change it later, you have to migrate every stored document.
- **Server setup:** send everyone editing the same document to the same server, even if you start with only one server.
- **Targets:** how many editors per document, maximum document size, acceptable delay, and whether you need offline editing and version history.

### Phase 1: Prove the core works
1. **Single-user editor on the real schema.** Every edit goes through the document model, and pasted content is cleaned. *Done when:* you can type, format, paste, save and reload without losing anything.
2. **Merge engine, tested without a network.** Connect the editor to the CRDT/OT library. Then run randomized tests with several in-memory copies of the document, sending edits late, out of order and twice. *Done when:* thousands of random runs all end with identical copies. This is the hardest correctness problem, and it's cheapest to test here.
3. **Basic sync.** A WebSocket server with one "room" per document that loads the current state when someone joins, broadcasts edits and handles reconnects. Use a placeholder user ID for now. *Done when:* two browsers can edit together live, and a dropped connection catches up with nothing lost.
4. **Saving to storage.** Store periodic snapshots plus a log of every edit, and shrink the log from time to time. Load documents when opened and free them from memory when idle. *Done when:* restarting the server loses no edit the server had confirmed.

### Phase 2: Safe to share
5. **Login and permissions.** Check who the user is when they connect. Enforce owner, editor, commenter and viewer roles on the server. Check every incoming edit against the schema, a size limit and a rate limit. *Required* before anyone outside the team uses it.
6. **Presence.** Show other people's cursors and selections and who's online, over a separate channel that isn't saved. This needs the real user identities from step 5.
7. **Undo/redo with collaborators.** Undo should reverse only your own changes. Undo built for one user often gets this wrong once other people's edits arrive, so fix it soon after step 3.

### Phase 3: Reliability and depth
8. **Offline editing.** Keep a local copy in the browser (IndexedDB), queue edits while offline, merge them on reconnect and show the connection status. This is cheap with a CRDT and hard with OT.
9. **Version history.** Snapshots, a view of what changed and restore. A restore should be applied as a new edit, never by rewriting the edit log. With Yjs, its built-in snapshots require turning off garbage collection, which makes documents bigger. That's why history belongs in the Phase 0 decisions.
10. **Comments.** Attach comments to positions that move with the text (for example, Yjs "relative positions") so they stay in place while others edit. Then add threads, resolving and @mentions.
11. **Suggestion mode / track changes.** This comes last among editing features because it touches the schema, permissions, display and history all at once.

### Phase 4: Scale and product features
12. **Scaling and hardening:**
    - Run several servers, routed by document ID or connected through Redis pub/sub.
    - Slow down or limit clients that send too much.
    - Keep large documents fast.
    - Detect when copies drift apart by having clients compare hashes of the document.
    - Run load tests and fault tests (killed servers, dropped messages).
13. **Product features.** Import/export (Markdown, DOCX, PDF), image uploads, search, sharing UI, templates, notifications and mobile.

### Start early, keep going throughout
- **End-to-end tests with several users** (for example, Playwright with multiple browser windows) from step 3 onward.
- **Metrics** such as how long edits take to reach other users, how often clients reconnect and how long documents take to load. Start these at step 3, not step 12.
- **Parallel work:** once step 3 works, steps 4 and 5 can be built at the same time, and so can steps 6 and 7.

**Shortcut:** If you use Yjs + Tiptap + Hocuspocus (a ready-made Yjs server), or a hosted service like Liveblocks, steps 2–4 and 6–8 become mostly integration work. Keep the randomized merge tests anyway.
