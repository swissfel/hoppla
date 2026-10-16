"""SSH_ASKPASS helper: ssh runs this program once per prompt; the answer goes to stdout.

Used in "app" login mode so that users type password and OTP into a small window
instead of a terminal.
"""

from __future__ import annotations

import os
import sys

from .config import APP_TITLE

LOCAL = "@127.0.0.1"


def classify(prompt: str) -> str:
    p = prompt.lower()
    if "(yes/no" in p or "(y/n)" in p or "[y/n]" in p:
        return "confirm"
    if "passphrase" in p or "password" in p:
        return "secret"
    return "code"  # OTP / verification code / anything else from keyboard-interactive


def friendly(prompt: str, label: str) -> str:
    text = prompt.strip()
    if LOCAL in text and label:  # inner hosts are reached through localhost
        text = text.replace(LOCAL, "@" + label)
    kind = classify(text)
    if kind == "secret" and "passphrase" not in text.lower():
        return f"{text}\n\n(your PSI password)"
    if kind == "code":
        return f"{text}\n\n(the MFA code from Microsoft Authenticator)"
    return text


def ask(prompt: str, label: str = "", root=None) -> str | None:
    """Show the dialog; returns the answer or None when cancelled."""
    import tkinter as tk
    from tkinter import ttk

    own_root = root is None
    root = root or tk.Tk()
    if own_root:
        from .platforms import bring_to_front
        bring_to_front()
    root.title(f"{APP_TITLE} login" + (f" - {label}" if label else ""))
    root.resizable(False, False)
    root.attributes("-topmost", True)
    result: dict = {"value": None}
    kind = classify(prompt)

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=friendly(prompt, label), wraplength=380, justify="left").pack(anchor="w")

    def done(value):
        result["value"] = value
        root.quit()

    buttons = ttk.Frame(frame)
    if kind == "confirm":
        ttk.Button(buttons, text="No", command=lambda: done("no")).pack(side="right")
        ttk.Button(buttons, text="Yes", command=lambda: done("yes")).pack(side="right", padx=6)
    else:
        var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=var, width=36, show="•" if kind == "secret" else "")
        entry.pack(fill="x", pady=(10, 0))
        entry.focus_force()
        if kind == "secret":
            shown = tk.BooleanVar(value=False)
            ttk.Checkbutton(frame, text="Show", variable=shown,
                            command=lambda: entry.configure(show="" if shown.get() else "•")).pack(anchor="w")
        ttk.Button(buttons, text="Cancel", command=lambda: done(None)).pack(side="right")
        ttk.Button(buttons, text="OK", command=lambda: done(var.get())).pack(side="right", padx=6)
        root.bind("<Return>", lambda e: done(var.get()))
    buttons.pack(fill="x", pady=(12, 0))
    root.bind("<Escape>", lambda e: done(None))
    root.protocol("WM_DELETE_WINDOW", lambda: done(None))

    root.update_idletasks()  # center on screen
    w, h = root.winfo_reqwidth(), root.winfo_reqheight()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
    root.lift()
    root.after(200, root.focus_force)
    root.mainloop()
    if own_root:
        root.destroy()
    return result["value"]


def main(argv: list[str]) -> int:
    prompt = " ".join(argv) or "Password:"
    try:
        answer = ask(prompt, os.environ.get("HOPPLA_LOGIN_LABEL", ""))
    except Exception as e:  # noqa: BLE001 - no display etc.
        print(f"hoppla askpass failed: {e}", file=sys.stderr)
        return 1
    if answer is None:
        from .tunnel import cancel_flag
        try:
            cancel_flag().touch()
        except OSError:
            pass  # the session runner sees the flag and stops ssh
        return 1
    data = answer.encode("utf-8") + b"\n"  # not the console code page (umlauts in passwords)
    if sys.stdout is not None and hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    else:
        os.write(1, data)
    return 0
