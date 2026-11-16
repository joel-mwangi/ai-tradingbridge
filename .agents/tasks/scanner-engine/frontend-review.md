# Multi-market scanner and auto-execute for DigitStrategyPanel

The change adds an always-on market scanner that watches ten volatility index symbols simultaneously and ranks them by digit-frequency signal strength, then optionally fires trades on the best-scoring result automatically. It is spread across two commits: the initial implementation (`2f12616`) and a five-finding fix pass (`3ee9365`). The scanner section is rendered above the Contract type selector inside `DigitStrategyPanel`, wired through `TradingWorkspace` with correct `isDemoAccount` and `onAutoExecute` props. Auto-execute is session-only opt-in and hard-disabled for non-demo or disconnected sessions. TypeScript passes clean (`npx tsc --noEmit` exit 0, recorded in both commit messages).

**Watch for:** *(confirmed)* The scanner's WebSocket message listener reattaches whenever `autoScanEnabled` or `socketRef` changes, but `socketRef` is a stable React ref object — React never re-runs effects on `.current` mutation. The result is that after a reconnect, the listener is not reattached to the new socket unless the user also toggles the scanner. The `retryCount` dependency was added to the subscription send effect but not to the message listener effect. *(confirmed)* The auto-execute cooldown uses `Date.now()` and a module-level ref; the ref resets to 0 on every component unmount/remount (e.g., navigating away from Strategy view and back), which can bypass the 60-second gate immediately on return. *(possible)* `onAutoExecute` in `TradingWorkspace` only maps `DIGITOVER` / `DIGITUNDER`; if the scanner ever surfaces `DIGITEVEN`, `DIGITODD`, `DIGITMATCH`, or `DIGITDIFF`, execution is silently skipped — the `console.warn` will fire but the user sees no UI feedback.

**Verdict**: NEEDS_CHANGES

---

## High-level view

The `useMarketScanner` hook attaches a message handler to the already-open `DigitStrategyPanel` WebSocket by reading `socketRef.current` at effect-run time. This avoids opening a second connection, which is the right call. The tradeoff is that React does not observe `.current` mutations, so the effect dependency on `socketRef` does not cause a re-run when the socket is replaced. After a reconnect, the subscription sends go out (because `retryCount` is in that effect's deps) but the message listener stays attached to the old closed socket. `scanner_update` messages from the new socket are therefore silently dropped until `autoScanEnabled` changes.

The auto-execute guard stack in `DigitStrategyPanel` correctly requires `autoExecuteEnabled`, `isDemoAccount`, `sessionConnected`, no active execution state, and a 60-second cooldown before firing. The cooldown ref (`lastAutoExecuteAtRef`) is local to the component instance. Navigating away from the Strategy tab unmounts the panel, which resets the ref to 0 on remount — the next auto-execute fires immediately regardless of when the last one ran.

The `onAutoExecute` wiring in `TradingWorkspace` maps `contract_type` through an explicit `DIGITOVER` / `DIGITUNDER` dictionary. This is clean, but the scanner backend can produce `DIGITEVEN`, `DIGITODD`, `DIGITMATCH`, and `DIGITDIFF` signals; those contract types hit the `console.warn` path and are silently skipped with no user-visible explanation.

The auto-execute toggle is disabled (both `disabled` attribute and grayed styling) when the account is not demo or the session is not connected, so the guard is enforced at the UI level in addition to the effect guard.

---

<details>
<summary>Issues (3)</summary>

1. **Scanner listener lost after reconnect** — The `useMarketScanner` message listener effect depends on `[socketRef, autoScanEnabled]`. Because `socketRef` is a stable ref object, the effect does not re-run when the WebSocket instance inside it is replaced after a reconnect. Add `retryCount` (already threaded through for the subscription effect) to the message listener effect's dependency array, or read and reattach inside the effect using `socketRef.current` with the `onopen` callback pattern.

2. **60-second cooldown resets on unmount** — `lastAutoExecuteAtRef` is initialized to `0` on component creation. Navigating from the Strategy tab and back mounts a fresh panel instance, clearing the cooldown regardless of when the last auto-execute fired. Persist the last-execute timestamp to `sessionStorage` (keyed by account ID or session ID) so the gate survives remounts within the same browser session.

3. **Unrecognised contract types silently skip execution** — The `onAutoExecute` handler in `TradingWorkspace` only maps `DIGITOVER` and `DIGITUNDER`. All other contract types (EVEN/ODD/MATCHES/DIFFERS) fall into the `console.warn` path with no visible feedback. Either expand the mapping to cover all six digit contract types, or surface a toast/notification in the UI when a skip occurs so the user knows why a high-confidence signal was not executed.

</details>

---

<details>
<summary>Details</summary>

## Scanner listener detaches silently after reconnect

`useMarketScanner` attaches its `handleMessage` listener inside a `useEffect` with `[socketRef, autoScanEnabled]` as dependencies. `socketRef` is a `RefObject<WebSocket | null>` — a plain object whose identity never changes. When `DigitStrategyPanel`'s socket effect creates a new `WebSocket` on reconnect, it updates `socketRef.current` but does not cause the scanner's effect to re-run. The old listener, registered on the closed socket, is gone. New `scanner_update` messages on the replacement socket are never processed.

The subscription send effect in `DigitStrategyPanel` already has `retryCount` in its deps precisely to handle this case. The same fix applies to `useMarketScanner`: accept `retryCount` as an option (or as a dependency signal) and include it in the effect's dependency array so the listener is re-registered after every reconnect.

## Cooldown bypass on remount

```ts
const lastAutoExecuteAtRef = useRef<number>(0);
```

This ref is created fresh at `0` every time `DigitStrategyPanel` mounts. The Strategy view is a conditional render branch inside `TradingWorkspace` — switching to Analyse or Charts and back unmounts and remounts the panel. On remount, `Date.now() - 0 > 60_000` is immediately true, so the first qualifying scanner result after navigation fires an auto-execute regardless of when the previous one was sent. `sessionStorage` keyed to `tradebridge_auto_exec_last_at:<accountId>` would survive the unmount within the same tab session while still clearing across sign-outs or account switches.

## Silent skip for non-OVER/UNDER contract types

```ts
const directionMap: Record<string, "over" | "under"> = {
  DIGITOVER: "over",
  DIGITUNDER: "under",
};
const direction = directionMap[contractTypeUpper];
if (!direction) {
  console.warn("[TradeBridge] auto-execute received unrecognised contract_type:", params.contract_type);
  return;
}
```

`trading.startStrategy` only accepts `"over" | "under"`, so the skip for EVEN/ODD/MATCHES/DIFFERS is structurally necessary — `startStrategy` can't route those today. The issue is the silent failure: the user has enabled auto-execute, a HIGH-confidence signal arrives, and nothing happens. The fix is a user-visible notification (the same `autoExecNotification` state already exists) explaining the skip: "Signal skipped: EVEN contracts not yet supported for auto-execute."

</details>

---

<details>
<summary>File map</summary>

- `types.ts` — `ScannerResult` interface added with nine fields
- `useMarketScanner.ts` — New hook: scanner message listener, scanner ON/OFF toggle with localStorage persistence, session-only auto-execute toggle, ten-symbol subscription list
- `DigitStrategyPanel.tsx` — Scanner section added above Contract type selector: ON/OFF toggle, top-3 results display, auto-execute toggle with demo+session guards, 60s cooldown, 5s notification banner; `retryCount` added to scanner subscription effect deps
- `TradingWorkspace.tsx` — `isDemoAccount` and `onAutoExecute` wired into `DigitStrategyPanel` render; `onAutoExecute` maps `DIGITOVER`/`DIGITUNDER` to `startStrategy` direction; unrecognised contract types log a warning and skip

Full diffs: `git show 2f12616` (feature) and `git show 3ee9365` (fix pass)

</details>
