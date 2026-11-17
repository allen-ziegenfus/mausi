"""mausi tab picker: a kitten listing every kitty tab in every OS window.

Type to filter (every word must match the title, folder or tab number),
↑/↓ or Ctrl-p/Ctrl-n to move, Enter jumps there, Esc cancels. ⏳ marks a tab
whose Claude session is waiting on you (user var claude_wait, set by
mausi-mark).

In kitty.conf (mausi.conf does this):
    map alt+g kitten /path/to/mausi/kitty/picker.py
"""
import json
import os
import subprocess
import sys
import termios
import tty

HOME = os.path.expanduser("~")


def tabs():
    try:
        data = json.loads(subprocess.run(["kitty", "@", "ls"], capture_output=True, text=True).stdout or "[]")
    except ValueError:
        return []
    rows = []
    n = 0
    for os_win in data:
        for tab in os_win.get("tabs", []):
            n += 1
            wins = tab.get("windows", [])
            active = next((w for w in wins if w.get("is_active")), wins[0] if wins else {})
            waiting = any(w.get("user_vars", {}).get("claude_wait") == "1" for w in wins)
            busy = any(w.get("user_vars", {}).get("claude_busy") == "1" for w in wins)
            title = tab.get("title", "").removeprefix("⏳ ").removeprefix("▶ ")
            cwd = active.get("cwd", "")
            if cwd.startswith(HOME):
                cwd = "~" + cwd[len(HOME):]
            rows.append({"id": tab["id"], "label": f"{'⏳ ' if waiting else '▶ ' if busy else ''}{n}: {title}", "cwd": cwd})
    return rows


def read_key(fd):
    ch = os.read(fd, 1)
    if ch == b"\x1b":
        import select
        if select.select([fd], [], [], 0.05)[0]:
            seq = os.read(fd, 2)
            return {b"[A": "up", b"[B": "down"}.get(seq, "esc")
        return "esc"
    return {b"\r": "enter", b"\n": "enter", b"\x7f": "back", b"\x08": "back", b"\x10": "up", b"\x0e": "down",
            b"\x03": "esc", b"\x15": "clear"}.get(ch, ch.decode(errors="ignore"))


def pick(rows):
    out = sys.stdout
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    query, cur = "", 0
    try:
        tty.setraw(fd)
        out.write("\033[?1049h\033[?25l")
        while True:
            words = query.lower().split()
            shown = [r for r in rows if all(w in f"{r['label']} {r['cwd']}".lower() for w in words)]
            cur = max(0, min(cur, len(shown) - 1))
            height = os.get_terminal_size().lines - 3
            width = os.get_terminal_size().columns
            top = max(0, cur - height + 1)
            out.write("\033[H\033[2J")
            out.write(f"\033[1mtab >\033[0m {query}\r\n\033[2menter: jump · esc: cancel\033[0m\r\n")
            for i, r in enumerate(shown[top:top + height], start=top):
                line = f"{r['label']}  \033[2m{r['cwd']}\033[0m"
                out.write(("\033[7m" if i == cur else "") + line[:width + 8] + "\033[0m\r\n")
            out.flush()
            k = read_key(fd)
            if k == "esc":
                return None
            if k == "enter":
                return shown[cur]["id"] if shown else None
            if k == "up":
                cur -= 1
            elif k == "down":
                cur += 1
            elif k == "back":
                query = query[:-1]
            elif k == "clear":
                query = ""
            elif len(k) == 1 and k.isprintable():
                query, cur = query + k, 0
    finally:
        out.write("\033[?25h\033[?1049l")
        out.flush()
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main(args):
    rows = tabs()
    if not rows:
        return None
    return pick(rows)


def handle_result(args, answer, target_window_id, boss):
    if answer is not None:
        # The same as `kitty @ focus-tab`, which also raises another OS window.
        boss.call_remote_control(None, ("focus-tab", "--match", f"id:{answer}"))

if __name__ == "__main__":
    print(main(sys.argv))
