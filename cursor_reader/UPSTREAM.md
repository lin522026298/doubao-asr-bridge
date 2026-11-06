# OpenLess cursor reader

This standalone program adapts the Windows cursor-context implementation from
[Open-Less/openless v2.0.1-tauri](https://github.com/Open-Less/openless/tree/v2.0.1-tauri),
commit `33d17a4e`. It is licensed separately under **AGPL-3.0-only**; see
`LICENSE-AGPL-3.0.txt`. The surrounding original bridge remains MIT licensed.

Source responsibilities retained:

- `openless-all/app/src-tauri/src/host_document/windows.rs`: CUIAutomation8 on an
  MTA thread; 200 ms UIA connection/transaction timeouts; TextPattern2 active caret
  first; one collapsed TextPattern selection fallback; no ValuePattern/full-document
  fallback; process/password gates; range-movement and focus revalidation.
- `openless-all/app/src-tauri/src/host_document/uia_text.rs`: reject saturated reads
  and invalid UTF-16 rather than silently accepting a truncated prefix.
- `openless-all/app/crates/openless-core/src/host_document/window.rs`: Unicode
  scalar budget, default 600, approximately 80/20 before/after with redistribution.
- `openless-all/app/crates/openless-core/src/prompts.rs`: caret marker, context
  envelope, boundary-tag neutralization and reference-only defense.

Adaptations: C#/.NET Framework is used to retain the existing installer toolchain.
The program calls the same native COM interfaces directly, including TextPattern2,
rather than substituting the older managed TextPattern API. COM ABI slots/GUIDs
come from Microsoft's `UIAutomationClient.h`. The bridge binds each invocation to
a foreground HWND/PID/process-creation identity. The separate tool excludes
OpenLess/its own executable, matching the original host's self-exclusion.
The bridge imposes a one-second lifetime; an additional 1.1-second process watchdog
prevents a reader from remaining after an abrupt caller exit, including while
waiting for its stdin request.

This is an algorithm/contract port, not a call into the installed OpenLess binary.
Capture timing can differ because the external-ASR interface exposes no original
insertion-target element. Equal capture rules do not establish equal model accuracy.

Input/output use anonymous pipes only. The executable reads no audio, opens no
network connection, logs no document text, changes no selection, and exits after
one request. Its `--self-test` path uses synthetic strings and initializes the native
COM interface without reading a user control.
`--fixture-test` and `--fixture-context` use only this program's own synthetic,
transparent, nonactivating controls and verify that the foreground window did not
change. No external HWND or document is accepted by these fixture modes.
