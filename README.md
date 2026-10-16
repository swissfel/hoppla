# Hoppla

*Hop into PSI from anywhere.*

Reach PSI-internal machines and web pages from your own laptop (Windows, macOS, Linux)
through the `hopx.psi.ch` gateway: a browser that goes through PSI, remote desktop, Jupyter,
SSH logins, and desktop icons that do it with one click. No SSH knowledge needed.

You log in **once** per session (PSI password + MFA); all selected services share that connection.

> **Unofficial tool, use at your own risk.** Hoppla is a personal project, not a PSI IT service. It comes
> without warranty or support (see [LICENSE](LICENSE), MIT). It only automates what you could do with
> `ssh` yourself and does not bypass any access rule: you need a valid PSI account, MFA, and access
> rights to the targets (e.g. the Ra cluster). Follow PSI's computing rules when using it.

## Download

| System | Download |
|---|---|
| Windows 10/11 | [hoppla-windows-x86_64.exe](https://github.com/swissfel/hoppla/releases/latest/download/hoppla-windows-x86_64.exe) |
| macOS, Apple chip (M1…) | [Hoppla-macos-arm64.zip](https://github.com/swissfel/hoppla/releases/latest/download/Hoppla-macos-arm64.zip) |
| macOS, Intel | [Hoppla-macos-x86_64.zip](https://github.com/swissfel/hoppla/releases/latest/download/Hoppla-macos-x86_64.zip) |
| Linux (x86_64) | [hoppla-linux-x86_64](https://github.com/swissfel/hoppla/releases/latest/download/hoppla-linux-x86_64) |
| Any system with Python 3.9+ | [hoppla.pyz](https://github.com/swissfel/hoppla/releases/latest/download/hoppla.pyz) |

Older versions: [all releases](https://github.com/swissfel/hoppla/releases).

## Install

- **Windows**: run the downloaded `.exe`. SmartScreen may warn because the program is not signed:
  click *More info > Run anyway*. On first start, Hoppla offers to install itself for your user
  (start menu + desktop icon); say yes, then you can delete the download.
- **macOS**: unzip, drag *Hoppla* into *Applications*, open it. The first time macOS blocks it:
  go to *System Settings > Privacy & Security* and click *Open Anyway*.
- **Linux**: `chmod +x hoppla-linux-x86_64 && ./hoppla-linux-x86_64`, and accept the offer to
  install (copies to `~/.local/bin/hoppla` and adds a menu entry). Needs a desktop session.
  Find it (and the icons you create) via the Super key by typing "Hoppla"; right-click > *Pin to Dash*
  to keep it in the dock. Icons on the GNOME desktop need right-click > *Allow Launching* once.

Hoppla uses the OpenSSH client that is part of Windows 10/11, macOS and Linux. If Windows says ssh
is missing: *Settings > System > Optional features > Add a feature > OpenSSH Client*.

## Use

1. Enter your **PSI user name**.
2. Tick the services you need (click the box in the first column).
3. Click **Connect**, type your **PSI password**, then approve the **MFA** request on your phone
   (or type the code if asked). If you have access to a cluster (e.g. Ra), log in there too when asked.
   The indicator turns green when connected.
4. Click **Open browser** for a separate browser window that goes through PSI,
   or select a service and click **Open selected service** (remote desktop, Jupyter, SSH login).

"Open browser", "Open selected service" and the desktop icons connect automatically if needed.
To change the selection: **Disconnect**, change, **Connect** (a new MFA login is needed).
If the laptop sleeps or the network changes, the connection drops; just connect again.

### Desktop icons

The buttons at the bottom create icons on the desktop and in the start menu / Applications:
*this window*, *connect*, *browser* (e.g. "PSI Firefox": asks for password+MFA if needed, then opens
the browser), and *selected service* (e.g. "PSI Windows terminal server (RDP)").

### Browser

The browser is started with its own profile (own bookmarks, logins), so your normal browsing is not affected.
- Default: everything in that browser window goes through PSI.
- *Only PSI addresses through the tunnel*: `*.psi.ch`, short host names and `129.129.*` go through PSI, everything else directly.

Other programs can use the SOCKS5 proxy `127.0.0.1:8989` (or the port shown in the list) while connected.

External users reach PSI through a cluster they have access to (by default Ra): the web proxy and
Jupyter run *through* Ra. Entries marked *(PSI staff)* run directly on hopx and only work for PSI staff.

### Login in a terminal instead

*Ask password/OTP in: Terminal window* shows the login in a terminal instead of dialogs. Use it if the
dialogs do not appear (older OpenSSH, e.g. early Windows 10 builds; *Automatic* picks this by itself).

## Troubleshooting

- **"Local ports already in use"**: another program uses that port. Edit the service and pick another local port.
- **"Login failed"**: user name, password or MFA code wrong. Wait for a fresh code before retrying:
  hopx blocks your internet address for a while after several failed or frequent logins, so Hoppla
  makes you wait between attempts.
- **"The gateway closed the connection"**: only one hopx session per user is allowed (close other
  hopx windows, e.g. your own `ssh hopx.psi.ch`), and sessions end after 12 hours.
- **"The gateway refused a connection to one of the selected targets"**: you have no access to that
  host/port. External users need cluster access (e.g. Ra) requested by their PSI contact.
- **"The gateway does not allow one of the selected targets"**: a service points to a host/port the gateway refuses.
- **Windows: no password dialog appears / connection ends at once**: choose *Ask password/OTP in:
  Terminal window* and connect again (please report it, the dialog mode is less tested on Windows).
- **Remote desktop on Linux**: install Remmina or FreeRDP. On macOS: install *Windows App* from the App Store.
- **`https://localhost:PORT` shows a certificate warning**: expected for forwarded web pages; the PSI browser avoids it.

Settings, browser profiles and logs are in `%APPDATA%\hoppla` (Windows),
`~/Library/Application Support/hoppla` (macOS), `~/.config/hoppla` (Linux); `session.log` there
shows what ssh reported.

---

## For maintainers

Pure Python standard library (Tkinter for the windows), no runtime dependencies.

```
hoppla/
  config.py     settings, default service list (presets())
  tunnel.py     ssh command lines, session runner (background or terminal), state, error texts
  askpass.py    password/OTP dialog that ssh calls (SSH_ASKPASS)
  browsers.py   browser discovery, private profiles, PAC
  actions.py    open RDP/URL/SSH, optional ~/.ssh/config aliases
  shortcuts.py  .lnk (Windows), .app (macOS), .desktop (Linux)
  install.py    copy the binary to a permanent place on first start
  gui.py, cli.py
```

**Design**
- hopx is proxy-only: an interactive *setup connection* (password + MFA, with a terminal) must stay
  open, only one per user, max. 12 h. Hoppla's first ssh is that session (`-tt`, no `-N`); it carries
  only `-L` forwards to the SSH port of internal hosts (Ra). Everything else (SOCKS `-D`, web/Jupyter
  `-L`) runs on a second ssh to such a host *through* that forward, like `ssh -J` (`HostKeyAlias` keeps
  host keys apart). Services with *runs through: gateway* are forwarded on hopx itself (PSI staff only).
- ssh runs with `-F none`, `StrictHostKeyChecking=accept-new` and its own known_hosts file, so users'
  ssh configs neither matter nor get touched.
- hopx blocks addresses after failed or frequent logins: one password attempt per connect
  (`NumberOfPasswordPrompts=1`), never an automatic reconnect, a growing pause between attempts
  (`tunnel.cooldown_left`), and tunnels are checked by binding the local port, never by connecting
  through them.
- Login mode *app* (OpenSSH ≥ 8.4): ssh runs hidden and calls this program as `SSH_ASKPASS`
  (`HOPPLA_ASKPASS=1` in the environment switches `main()` to the dialog); yes/no questions of the
  hopx session get a dialog too. Mode *terminal*: ssh in a visible terminal. The runner records PIDs
  in `state.json` and the outcome in `last-session.json`; GUI and icons watch those and the local ports.

**Adapt the default services** in `config.presets()`. Users can also add/edit services in the window.

### Test on Linux

```
python3 -m unittest discover -s tests           # unit tests
python3 -m hoppla                           # run the window from source
HOPPLA_HOME=/tmp/pt-test python3 -m hoppla   # same, with a throw-away settings folder
python3 -m hoppla status | stop             # CLI, see --help
```

**Simulated hopx** (no PSI account needed): `tools/fake_hopx.py` imitates hopx (password + MFA code,
setup connection needing a terminal, SSH-only targets, lockout, one session per user) and a "Ra" behind
it; `tools/simulate.py` runs a real Hoppla session against it with scripted dialog answers.

```
python3 -m venv /tmp/pt-venv && /tmp/pt-venv/bin/pip install paramiko
/tmp/pt-venv/bin/python tools/fake_hopx.py &    # user tester, password secret, code 123456
python3 tools/simulate.py                        # HOPPLA_SIM_PASSWORD=wrong for a failed login
```
The simulator models what is known about hopx; a real login remains the final test.

Binary like the users get it:

```
python3 -m venv /tmp/pt-venv && /tmp/pt-venv/bin/pip install "pyinstaller>=6.10"
/tmp/pt-venv/bin/python build.py binary        # -> dist/hoppla-linux-x86_64
python3 build.py zipapp                         # -> dist/hoppla.pyz
```

### Release

Push a tag: `git tag v0.2.0 && git push origin v0.2.0`. GitHub Actions
(`.github/workflows/build.yml`) runs the tests on all three systems, builds Windows, macOS (arm64 + Intel),
Linux and the `.pyz`, and publishes them as a GitHub Release. Bump `__version__` in
`hoppla/__init__.py` first. Keep the asset file names unchanged; the download links above rely on them.

Code signing (Apple Developer ID + notarization, Windows Authenticode) would remove the first-start
warnings; it can be added to the workflow once certificates exist.

## License

MIT, see [LICENSE](LICENSE). Provided as is, without warranty or support; use at your own risk.
Hoppla is not an official PSI product.
