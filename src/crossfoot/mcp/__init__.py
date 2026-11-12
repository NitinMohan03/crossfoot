"""The MCP servers the adjudication agent calls, one per source (plan §3.3).

`erp/` is the reference implementation. Sessions 2.2-2.4 build `mcp-bank`, `mcp-processor` and
`mcp-remittance` by reading it, so the pattern below is as much the deliverable as the server.

**The pattern, in seven rules.**

1. **`tools.py` holds every decision; `server.py` only wires.** The tool layer is plain Python
   that takes validated arguments and returns `CanonicalRecord`s, so it is unit-tested directly
   with no transport in the way. `server.py` registers those functions, turns their results into
   payloads and maps errors to the envelope -- and contains no logic of its own, so one
   integration test over real stdio is enough to prove the wiring.
2. **One error envelope with a documented code table**, exactly as `mock_processor/app.py` does
   it for the REST source. Errors are *returned*, not raised: an agent working inside a 4-step
   budget has to branch on a stable code, and an MCP error result carries only prose. Raising
   also loses the reason entirely -- the SDK replaces an unexpected exception with "Error
   executing tool X". A failed call therefore answers with `{"error": {"code", "message"}}`, the
   session survives, and the next call still works.
3. **`<server>/canonical.py` is that source's mapper**, and the only place that knows how the
   source spells things. `models/canonical.py` does not change when a source is added: a new
   source adds a `detail` variant.
4. **Settings are a frozen dataclass with `from_env()`** and module-level defaults, so a server
   runs with no configuration and every knob has one name.
5. **Record ids are spec 01 §5.3 ids, unchanged**, so results join the ground-truth manifest
   with no translation step.
6. **Read-only is enforced at the source, not by convention.** The ERP server connects as a
   database role holding SELECT and nothing else, *and* opens its transactions read-only, *and*
   is asserted to advertise only read-only tools. Each later server does the equivalent for its
   own source. The agent's only write path is the escalation queue.
7. **`Thresholds` are imported, never restated.** Likewise entity resolution: a server resolves
   a vendor name through `crossfoot.entities`, it does not fuzzy-match on its own.

Source access is deliberately *not* abstracted into `common/`: a Postgres role, a bearer token
and a read-only file handle have nothing useful in common, and pretending otherwise would buy an
interface no caller wants. `common/` holds what is genuinely shared -- the error envelope and the
payload shape.
"""
