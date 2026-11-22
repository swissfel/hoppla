# Hoppla web page (test build)

`index.html` is published to <https://swissfel.github.io/hoppla/> by `.github/workflows/pages.yml`
on every push to `main` that touches `web/`. The local bridge it talks to is `hoppla/bridge.py`,
started with `hoppla serve`.

Goal: open one PSI page (main example `https://jupytera.psi.ch/`, reached from Ra through hopx) from a
web page, in that tab only, installing as little as possible. A saved link (`#p-<id>` local,
`#s-<code>` portable) restores the setup. Passwords are never stored or put in links.

hopx allows **one** session per user (12 h, and repeated failed logins block the address), so the page
reuses a session whenever it can: 1. desktop Hoppla's, via `hoppla serve`; 2. another tab of the page
(BroadcastChannel); 3. only then its own login (not built yet).

## What is real

| Part | State |
| --- | --- |
| Bridge: `/hello`, `/status`, WebSocket relay to ports Hoppla forwards | real, unit-tested (`tests/test_bridge.py`) |
| Page finds the bridge, pairs, shows the session and its time left | real |
| “Open … through Hoppla” opens the forward Hoppla already runs (Jupytera: `https://localhost:9789/`) | real, needs that forward running |
| “Test channel to cluster”: browser → bridge → Hoppla’s forward → Ra, reads the SSH banner | real |
| Cross-tab reuse | simulated sign-in, real tab-to-tab handover |
| Sign-in flow and the framed page | **simulation** |
| SSH client in the browser (WASM) and HTTP(S) over SSH channels | **not built** |

## Test list

1. Open <https://swissfel.github.io/hoppla/>.
2. Connect in Hoppla (SSH to Ra plus “Jupyter on Ra”, i.e. a forward to `jupytera.psi.ch:443`).
3. Run `hoppla serve`. It prints a pairing code.
4. In the page: paste the code, press **Find Hoppla**. Expect: session open, services listed, time left.
5. **Test channel to cluster**: expect Ra’s `SSH-2.0-…` banner; Hoppla stays connected.
6. Set the page to `https://jupytera.psi.ch/`, press **Open … through Hoppla**: Jupytera opens in a new
   tab (accept the localhost certificate warning).
7. **Save in this browser**, close the tab, open the saved link: setup restored, Hoppla found again.

Note per browser (Chrome, Firefox, Safari, Edge): bridge found? Local-network prompt? Banner test ok?
Browsers are tightening access from public sites to local programs; a prompt or block is expected.

## Rules for changes

- Bridge: standard library only, 127.0.0.1 only, Origin allowlist, pairing code on everything except
  `/hello`, `/ws` only to running and listening ssh/forward ports of Hoppla services. Never add a path
  that starts a gateway login or reaches arbitrary ports.
- Page: one self-contained `index.html`, light and dark theme, works on a phone. Shared links are
  untrusted input: sanitize host names, quote commands, show who a link points to. Keep `__BUILD__`
  (the Pages workflow stamps it). Typographic apostrophes and quotes in visible text.

## Open questions

- Which browsers let an https page reach `http://127.0.0.1` (fetch and WebSocket), and which prompt?
- Does hopx count extra connections through Hoppla’s forward as sessions? (should not; verify)
- Which SSH client library runs in the browser with keyboard-interactive (password + MFA)?

## Next steps

1. Run the test list with a real session; record results per browser here.
2. End-to-end test the bridge against `tools/fake_hopx.py`.
3. Browser SSH client over the bridge channel, then a service worker that loads one page through it.
4. A bridge command that asks Hoppla to connect (Hoppla shows its own password dialog).
