"""Tkinter window: choose services, connect, launch browsers, create shortcuts."""

from __future__ import annotations

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import __version__, actions, browsers, shortcuts, tunnel
from .config import APP_TITLE, LOGIN_MODES, MACOS, OPEN_ACTIONS, SERVICE_TYPES, WINDOWS, Config, Service, load, presets
from .platforms import bring_to_front, find_ssh, ssh_install_hint

CHECK, UNCHECK = "☑", "☐"


class ServiceDialog(tk.Toplevel):
    def __init__(self, master, cfg: Config, service: Service | None):
        super().__init__(master)
        self.title("Edit service" if service else "Add service")
        self.transient(master)
        self.resizable(False, False)
        self.result: Service | None = None
        self.cfg, self.orig = cfg, service
        s = service or Service("", "", "forward", 0)

        self.v = {
            "name": tk.StringVar(value=s.name),
            "type": tk.StringVar(value=s.type),
            "local_port": tk.StringVar(value=str(s.local_port or "")),
            "remote_host": tk.StringVar(value=s.remote_host),
            "remote_port": tk.StringVar(value=str(s.remote_port or "")),
            "via": tk.StringVar(value=s.via),
            "open": tk.StringVar(value=s.open),
            "url": tk.StringVar(value=s.url),
        }
        f = ttk.Frame(self, padding=12)
        f.grid(sticky="nsew")
        row = 0

        def add(label, widget, hint=""):
            nonlocal row
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=3)
            widget.grid(row=row, column=1, sticky="ew", pady=3)
            if hint:
                ttk.Label(f, text=hint, foreground="gray").grid(row=row, column=2, sticky="w", padx=6)
            row += 1
            return widget

        add("Name", ttk.Entry(f, textvariable=self.v["name"], width=34))
        add("Type", ttk.Combobox(f, textvariable=self.v["type"], values=list(SERVICE_TYPES),
                                 state="readonly", width=12), "socks / forward / ssh")
        add("Local port", ttk.Entry(f, textvariable=self.v["local_port"], width=8), "1024-65535 on this computer")
        self.w_host = add("Remote host", ttk.Entry(f, textvariable=self.v["remote_host"]), "e.g. ra.psi.ch")
        self.w_port = add("Remote port", ttk.Entry(f, textvariable=self.v["remote_port"], width=8),
                          "22 for SSH, 3389 for RDP")
        vias = ["gateway"] + [x.id for x in cfg.services if x.type == "ssh" and (not service or x.id != service.id)]
        self.w_via = add("Runs through", ttk.Combobox(f, textvariable=self.v["via"], values=vias,
                                                      state="readonly"), "gateway = directly from hopx")
        self.w_open = add("When opened", ttk.Combobox(f, textvariable=self.v["open"], values=list(OPEN_ACTIONS),
                                                      state="readonly"), "none / url / rdp")
        self.w_url = add("URL", ttk.Entry(f, textvariable=self.v["url"]), "{port} = local port")
        self.x11 = tk.BooleanVar(value=s.x11)
        self.w_x11 = add("Graphics", ttk.Checkbutton(f, text="allow graphical programs (X11)", variable=self.x11),
                         "SSH only; needs an X server")

        b = ttk.Frame(f)
        b.grid(row=row, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(b, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(b, text="OK", command=self.ok).pack(side="right", padx=6)
        self.v["type"].trace_add("write", lambda *_: self.update_state())
        self.v["open"].trace_add("write", lambda *_: self.update_state())
        self.update_state()
        self.bind("<Return>", lambda e: self.ok())
        self.bind("<Escape>", lambda e: self.destroy())
        self.grab_set()

    def update_state(self):
        t, o = self.v["type"].get(), self.v["open"].get()
        for w, on in ((self.w_host, t != "socks"), (self.w_port, t != "socks"), (self.w_x11, t == "ssh"),
                      (self.w_open, t == "forward"), (self.w_url, t == "forward" and o == "url")):
            w.configure(state=("readonly" if isinstance(w, ttk.Combobox) else "normal") if on else "disabled")

    def ok(self):
        v = {k: x.get().strip() for k, x in self.v.items()}
        try:
            lp = int(v["local_port"])
            rp = int(v["remote_port"] or 0)
        except ValueError:
            messagebox.showerror(APP_TITLE, "Ports must be numbers.", parent=self)
            return
        if not v["name"]:
            messagebox.showerror(APP_TITLE, "Please enter a name.", parent=self)
            return
        sid = self.orig.id if self.orig else self.cfg.new_id(v["name"])
        s = Service(sid, v["name"], v["type"], lp, v["remote_host"], rp,
                    via=v["via"] or "gateway", x11=self.x11.get() if v["type"] == "ssh" else False,
                    open=v["open"] if v["type"] == "forward" else "none", url=v["url"],
                    enabled=self.orig.enabled if self.orig else True)
        others = [x for x in self.cfg.services if x.id != sid]
        clash = [x.name for x in others if x.local_port == lp]
        probs = Config(user="x", services=others + [s]).problems([s])
        if clash:
            probs.append(f"Local port {lp} is already used by: {', '.join(clash)}")
        if probs:
            messagebox.showerror(APP_TITLE, "\n".join(probs), parent=self)
            return
        self.result = s
        self.destroy()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load()
        root.title(f"{APP_TITLE} {__version__}")
        root.minsize(760, 520)

        self.user = tk.StringVar(value=self.cfg.user)
        self.gateway = tk.StringVar(value=self.cfg.gateway)
        self.status = tk.StringVar()
        self.tunnel_state = tk.StringVar(value="...")
        self.pac = tk.BooleanVar(value=self.cfg.pac_only_psi)
        self.sysproxy = tk.BooleanVar(value=self.cfg.system_proxy)
        if not tunnel.read_state():
            from .sysproxy import cleanup_leftovers
            cleanup_leftovers()  # a crashed session may have left the Mac's proxy setting behind
        self.login = tk.StringVar(value=LOGIN_MODES.get(self.cfg.login_mode, LOGIN_MODES["auto"]))
        self.was_running = bool(tunnel.read_state())
        self.pending = 0.0  # time of a start request whose runner has not reported yet
        self.found = browsers.detect()
        default_browser = self.cfg.browser if self.cfg.browser in self.found else next(iter(self.found), "")
        self.browser = tk.StringVar(value=browsers.NAMES.get(default_browser, ""))

        main = ttk.Frame(root, padding=10)
        main.pack(fill="both", expand=True)

        # account
        acc = ttk.LabelFrame(main, text="PSI account", padding=8)
        acc.pack(fill="x")
        ttk.Label(acc, text="User name").grid(row=0, column=0, sticky="w")
        self.user_entry = ttk.Entry(acc, textvariable=self.user, width=18)
        self.user_entry.grid(row=0, column=1, padx=6)
        ttk.Label(acc, text="Gateway").grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Entry(acc, textvariable=self.gateway, width=22).grid(row=0, column=3, padx=6)
        ssh = find_ssh()
        ttk.Label(acc, text="Ask password/OTP in").grid(row=0, column=4, sticky="w", padx=(12, 0))
        ttk.Combobox(acc, textvariable=self.login, values=list(LOGIN_MODES.values()), state="readonly",
                     width=15).grid(row=0, column=5, padx=6)
        ttk.Label(acc, text=f"ssh: {ssh}" if ssh else "ssh: NOT FOUND",
                  foreground="gray" if ssh else "red").grid(row=1, column=0, columnspan=6, sticky="w", pady=(4, 0))

        # services
        sv = ttk.LabelFrame(main, text="Services (tick the ones to connect)", padding=8)
        sv.pack(fill="both", expand=True, pady=8)
        cols = ("on", "name", "type", "port", "target", "state")
        self.tree = ttk.Treeview(sv, columns=cols, show="headings", height=8, selectmode="browse")
        for c, text, w, anchor in (("on", "", 34, "center"), ("name", "Name", 230, "w"), ("type", "Type", 70, "w"),
                                   ("port", "Local port", 80, "center"), ("target", "Target", 220, "w"),
                                   ("state", "State", 80, "center")):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor=anchor, stretch=c in ("name", "target"))
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<Double-1>", lambda e: self.edit())
        self.tree.bind("<space>", lambda e: self.toggle(self.tree.focus()))
        side = ttk.Frame(sv)
        side.pack(side="left", fill="y", padx=(8, 0))
        for text, cmd in (("Add...", self.add), ("Edit...", self.edit), ("Remove", self.remove),
                          ("Reset list", self.reset)):
            ttk.Button(side, text=text, command=cmd, width=12).pack(pady=2)

        # tunnel
        tn = ttk.LabelFrame(main, text="Connection", padding=8)
        tn.pack(fill="x")
        self.state_label = ttk.Label(tn, textvariable=self.tunnel_state, width=14, font=("TkDefaultFont", 10, "bold"))
        self.state_label.pack(side="left")
        ttk.Button(tn, text="Connect", command=self.start).pack(side="left", padx=4)
        ttk.Button(tn, text="Disconnect", command=self.stop).pack(side="left", padx=4)
        self.hint = tk.StringVar()
        ttk.Label(tn, textvariable=self.hint, foreground="gray").pack(side="left", padx=10)
        self.update_hint()
        self.login.trace_add("write", lambda *_: self.update_hint())

        # use
        use = ttk.LabelFrame(main, text="Use the tunnel", padding=8)
        use.pack(fill="x", pady=8)
        ttk.Label(use, text="Browser").grid(row=0, column=0, sticky="w")
        names = [browsers.NAMES[n] for n in self.found] or ["(none found)"]
        ttk.Combobox(use, textvariable=self.browser, values=names, state="readonly", width=16).grid(row=0, column=1, padx=6)
        ttk.Checkbutton(use, text="Only PSI addresses through the tunnel", variable=self.pac).grid(row=0, column=2, padx=6)
        ttk.Button(use, text="Open browser", command=self.open_browser).grid(row=0, column=3, padx=6)
        ttk.Button(use, text="Open selected service", command=self.open_selected).grid(row=1, column=1, columnspan=2,
                                                                                         sticky="w", padx=6, pady=(6, 0))
        if MACOS:
            ttk.Checkbutton(use, text="PSI addresses for Safari and all Mac apps (system proxy) ...",
                            variable=self.sysproxy, command=self.toggle_sysproxy).grid(
                row=2, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))

        # shortcuts
        sc = ttk.LabelFrame(main, text="Desktop icons and SSH aliases", padding=8)
        sc.pack(fill="x")
        for text, cmd in (("Icon: this window", lambda: self.shortcut("gui")),
                          ("Icon: connect", lambda: self.shortcut("tunnel")),
                          ("Icon: browser", lambda: self.shortcut("browser")),
                          ("Icon: selected service", lambda: self.shortcut("service")),
                          ("SSH aliases (advanced)", self.write_ssh_config)):
            ttk.Button(sc, text=text, command=cmd).pack(side="left", padx=3)

        ttk.Label(main, textvariable=self.status, foreground="gray").pack(fill="x", pady=(6, 0))
        if not ssh:
            self.root.after(300, lambda: messagebox.showwarning(APP_TITLE, ssh_install_hint()))
        elif not self.cfg.user:
            self.status.set("Enter your PSI user name to begin.")
            self.user_entry.focus_set()
        self.refresh_tree()
        self.poll()
        self.root.after(500, self.offer_install)

    def offer_install(self):
        from . import install
        from .platforms import translocated
        if translocated():
            messagebox.showwarning(APP_TITLE, "Please move Hoppla into your Applications folder and start "
                                              "it from there. Desktop icons only work from there.")
            return
        if install.is_installed():
            return
        if messagebox.askyesno(APP_TITLE, "Install Hoppla for your user account?\n\n"
                                          f"It is copied to {install.target()} and added to the "
                                          "start menu and desktop, so icons keep working after you "
                                          "delete the download. (Recommended)"):
            try:
                made = install.install()
            except OSError as e:
                self.error(e)
                return
            messagebox.showinfo(APP_TITLE, "Installed:\n" + "\n".join(map(str, made)) +
                                "\n\nYou can delete the downloaded file.")

    # ---------------------------------------------------------- helpers -----
    def save(self) -> Config:
        self.cfg.user = self.user.get().strip()
        self.cfg.gateway = self.gateway.get().strip()
        self.cfg.pac_only_psi = self.pac.get()
        self.cfg.system_proxy = self.sysproxy.get()
        self.cfg.login_mode = {v: k for k, v in LOGIN_MODES.items()}.get(self.login.get(), "auto")
        inv = {v: k for k, v in browsers.NAMES.items()}
        self.cfg.browser = inv.get(self.browser.get(), self.cfg.browser)
        self.cfg.save()
        return self.cfg

    def selected_service(self) -> Service | None:
        sid = self.tree.focus()
        if not sid:
            messagebox.showinfo(APP_TITLE, "Select a service in the list first.")
            return None
        return self.cfg.service(sid)

    def error(self, e) -> None:
        messagebox.showerror(APP_TITLE, str(e).strip("'\""))

    def background(self, func, done_msg: str = "") -> None:
        def run():
            try:
                func()
                if done_msg:
                    self.root.after(0, lambda: self.status.set(done_msg))
            except Exception as e:  # noqa: BLE001 - show anything to the user
                err = e
                self.root.after(0, lambda: (self.status.set(""), self.error(err)))
        threading.Thread(target=run, daemon=True).start()

    # ----------------------------------------------------------- list -------
    def refresh_tree(self) -> None:
        focus = self.tree.focus()
        self.tree.delete(*self.tree.get_children())
        for s in self.cfg.services:
            self.tree.insert("", "end", iid=s.id, values=(CHECK if s.enabled else UNCHECK, s.name, s.type,
                                                         s.local_port, self.target_text(s), ""))
        if focus in self.tree.get_children():
            self.tree.focus(focus)
            self.tree.selection_set(focus)

    def target_text(self, s: Service) -> str:
        text = s.target()
        if s.via != "gateway":
            try:
                text = text.replace(f"via {s.via}", f"via {self.cfg.service(s.via).remote_host}")
            except KeyError:
                pass
        return text

    def on_click(self, event):
        if self.tree.identify_column(event.x) == "#1" and self.tree.identify_region(event.x, event.y) == "cell":
            self.toggle(self.tree.identify_row(event.y))

    def toggle(self, sid):
        if not sid:
            return
        s = self.cfg.service(sid)
        s.enabled = not s.enabled
        self.save()
        self.refresh_tree()

    def add(self):
        d = ServiceDialog(self.root, self.cfg, None)
        self.root.wait_window(d)
        if d.result:
            self.cfg.services.append(d.result)
            self.save()
            self.refresh_tree()

    def edit(self):
        s = self.selected_service()
        if not s:
            return
        d = ServiceDialog(self.root, self.cfg, s)
        self.root.wait_window(d)
        if d.result:
            self.cfg.services[self.cfg.services.index(s)] = d.result
            self.save()
            self.refresh_tree()

    def remove(self):
        s = self.selected_service()
        if s and messagebox.askyesno(APP_TITLE, f"Remove '{s.name}'?"):
            self.cfg.services.remove(s)
            self.save()
            self.refresh_tree()

    def reset(self):
        if messagebox.askyesno(APP_TITLE, "Replace the service list with the default list?"):
            self.cfg.services = presets()
            self.save()
            self.refresh_tree()

    def toggle_sysproxy(self):
        from .sysproxy import EXPLANATION
        if self.sysproxy.get():
            if not messagebox.askyesno(APP_TITLE, EXPLANATION, icon="question"):
                self.sysproxy.set(False)
        self.save()
        if tunnel.read_state():
            self.status.set("Takes effect at the next Connect.")

    # --------------------------------------------------------- tunnel -------
    def poll(self):
        running = set(tunnel.running_services())
        for s in self.cfg.services:
            if not self.tree.exists(s.id):
                continue
            if s.id in running:
                state = "up" if tunnel.listening(s.local_port) else "login..."
            else:
                state = ""
            self.tree.set(s.id, "state", state)
        up = bool(running) and all(tunnel.listening(p) for p in self.ports_of(running))
        if running:
            self.pending = 0.0
        elif self.pending and (tunnel.last_result().get("message") or time.time() - self.pending > 30):
            self.pending = 0.0
            self.session_ended()
        if up:
            self.tunnel_state.set("● Connected")
        elif running or self.pending:
            self.tunnel_state.set("◔ Logging in...")
        else:
            self.tunnel_state.set("○ Disconnected")
        self.state_label.configure(foreground="green" if up else "orange" if running or self.pending else "gray")
        if self.was_running and not running:
            self.session_ended()
        self.was_running = bool(running)
        self.root.after(1500, self.poll)

    def ports_of(self, ids) -> list[int]:
        return [s.local_port for s in self.cfg.services if s.id in ids]

    def session_ended(self):
        res = tunnel.last_result()
        msg = res.get("message") or "Disconnected."
        self.status.set(msg)
        if res.get("failed") and msg != "Login cancelled.":
            messagebox.showerror(APP_TITLE, msg)

    def update_hint(self):
        mode = {v: k for k, v in LOGIN_MODES.items()}.get(self.login.get(), "auto")
        if mode == "auto":
            mode = tunnel.login_mode(Config(login_mode="auto"))
        self.hint.set("Enter your PSI password, then approve the MFA request (or type the code)." if mode == "app"
                      else "A terminal opens for the login (password, then MFA). Keep it open.")

    def start(self):
        cfg = self.save()
        if tunnel.read_state():
            messagebox.showinfo(APP_TITLE, "The tunnel is already running. Disconnect first to change services.")
            return
        if not cfg.selected():
            messagebox.showerror(APP_TITLE, "Tick at least one service.")
            return
        try:
            mode = tunnel.start(cfg)
        except tunnel.TunnelError as e:
            self.error(e)
            return
        self.pending = time.time()
        self.status.set("Connecting..." if mode == "app" else "Log in in the terminal window that just opened.")

    def stop(self):
        self.status.set("Disconnected." if tunnel.stop() else "Not connected.")

    # ------------------------------------------------------------ use -------
    def open_browser(self):
        cfg = self.save()
        if not cfg.browser or cfg.browser not in self.found:
            messagebox.showerror(APP_TITLE, "No supported browser found (Firefox, Chrome, Edge, Chromium, Brave).")
            return
        from .cli import _pick_socks
        try:
            socks = _pick_socks(cfg, None)
        except tunnel.TunnelError as e:
            self.error(e)
            return
        self.status.set(f"Starting {browsers.NAMES[cfg.browser]} via '{socks.name}'...")

        def run():
            tunnel.ensure_service(cfg, socks.id)
            browsers.launch(cfg, cfg.browser, socks.local_port)
        self.background(run, f"{browsers.NAMES[cfg.browser]} started (separate profile using the tunnel).")

    def open_selected(self):
        s = self.selected_service()
        if not s:
            return
        cfg = self.save()
        if s.type == "socks":
            messagebox.showinfo(APP_TITLE, f"Use 'Open browser' for proxies, or set SOCKS5 127.0.0.1:{s.local_port} "
                                           "in any program.")
            return
        if s.type == "forward" and s.open == "none":
            messagebox.showinfo(APP_TITLE, f"Connect your program to 127.0.0.1:{s.local_port} "
                                           f"(forwarded to {s.target()}).")
            return
        self.status.set(f"Opening {s.name}...")

        def run():
            tunnel.ensure_service(cfg, s.id)
            actions.open_service(cfg, s)
        self.background(run, f"Opened {s.name}.")

    # ------------------------------------------------------ shortcuts -------
    def shortcut(self, what: str):
        cfg = self.save()
        try:
            if what == "gui":
                made = shortcuts.create(APP_TITLE, [], "gui")
            elif what == "tunnel":
                made = shortcuts.create("Hoppla - connect", ["start"], "tunnel")
            elif what == "browser":
                if not cfg.browser:
                    raise FileNotFoundError("No browser selected.")
                made = shortcuts.create(f"PSI {browsers.NAMES[cfg.browser]}", ["browser", cfg.browser], "browser")
            else:
                s = self.selected_service()
                if not s:
                    return
                if s.type == "ssh":
                    made = shortcuts.create(f"PSI {s.name}", ["ssh", s.id], "ssh")
                elif s.type == "forward" and s.open != "none":
                    made = shortcuts.create(f"PSI {s.name}", ["open", s.id], s.open)
                else:
                    raise ValueError(f"'{s.name}' has nothing to open; use the browser or connect icon.")
        except Exception as e:  # noqa: BLE001
            self.error(e)
            return
        messagebox.showinfo(APP_TITLE, "Created:\n" + "\n".join(str(p) for p in made))

    def write_ssh_config(self):
        cfg = self.save()
        if not cfg.user:
            self.error("Enter your PSI user name first.")
            return
        block = actions.ssh_config_block(cfg)
        if not messagebox.askyesno(APP_TITLE, f"Add this block to {actions.ssh_config_path()}?\n"
                                              "(a backup is made; the block is replaced on later updates)\n\n" + block):
            return
        try:
            actions.write_ssh_config(cfg)
        except OSError as e:
            self.error(e)
            return
        self.status.set("SSH aliases: " + ", ".join(actions.ssh_aliases(cfg)) + " (while connected)")


def run_gui() -> int:
    if WINDOWS:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    bring_to_front()
    style = ttk.Style(root)
    for theme in ("vista", "aqua", "clam"):  # first available native-looking theme
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    App(root)
    root.mainloop()
    return 0
