"""WebSocket terminal endpoint — PTY-backed shell sessions (cross-platform)."""
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"

if not _WINDOWS:
    import fcntl
    import pty
    import select as sel
    import struct
    import termios


def _available_shells() -> list[str]:
    if _WINDOWS:
        candidates = ["pwsh", "powershell", "cmd"]
    else:
        candidates = ["bash", "zsh", "fish", "sh"]
    found = [sh for sh in candidates if shutil.which(sh)]
    return found or (["cmd"] if _WINDOWS else ["sh"])


def _set_pty_size_unix(fd: int, rows: int, cols: int) -> None:
    try:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except Exception:
        pass


@router.get("/shells")
async def list_shells():
    shells = _available_shells()
    return {"shells": shells, "default": shells[0] if shells else None, "available": True}


@router.websocket("/ws")
async def terminal_ws(websocket: WebSocket, shell: str = ""):
    await websocket.accept()

    available = _available_shells()
    if not shell or shell not in available:
        shell = available[0]

    if _WINDOWS:
        await _terminal_windows(websocket, shell)
    else:
        await _terminal_unix(websocket, shell)


async def _terminal_windows(websocket: WebSocket, shell: str) -> None:
    try:
        import winpty  # pywinpty — ConPTY on Windows
    except ImportError:
        await websocket.send_bytes(
            b"\r\nWindows terminal requires pywinpty.\r\n"
            b"Install it with: pip install pywinpty\r\n"
        )
        await websocket.close()
        return

    loop = asyncio.get_event_loop()
    stop_event = threading.Event()

    # Resolve full executable path so winpty can find it
    exe = shutil.which(shell) or shell
    try:
        pty_proc = winpty.PtyProcess.spawn(exe, dimensions=(24, 80))
    except Exception as exc:
        await websocket.send_bytes(f"\r\nFailed to start {shell}: {exc}\r\n".encode())
        await websocket.close()
        return

    def read_thread() -> None:
        while not stop_event.is_set() and pty_proc.isalive():
            try:
                data = pty_proc.read(4096)
                if data:
                    if isinstance(data, str):
                        data = data.encode("utf-8", errors="replace")
                    asyncio.run_coroutine_threadsafe(websocket.send_bytes(data), loop)
            except EOFError:
                break
            except Exception:
                break

    reader = threading.Thread(target=read_thread, daemon=True)
    reader.start()

    try:
        while True:
            msg = await websocket.receive()
            if "bytes" in msg and msg["bytes"]:
                pty_proc.write(msg["bytes"].decode("utf-8", errors="replace"))
            elif "text" in msg and msg["text"]:
                try:
                    data = json.loads(msg["text"])
                    if data.get("type") == "resize":
                        pty_proc.setwinsize(data.get("rows", 24), data.get("cols", 80))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("terminal_ws (windows) error: %s", exc)
    finally:
        stop_event.set()
        try:
            pty_proc.terminate()
        except Exception:
            pass


async def _terminal_unix(websocket: WebSocket, shell: str) -> None:
    env = {
        **os.environ,
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
        "LANG": "en_US.UTF-8",
    }
    master_fd, slave_fd = pty.openpty()
    _set_pty_size_unix(master_fd, 24, 80)

    proc = subprocess.Popen(
        [shell],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
        env=env,
    )
    os.close(slave_fd)

    loop = asyncio.get_event_loop()
    stop_event = threading.Event()

    def read_thread() -> None:
        while not stop_event.is_set() and proc.poll() is None:
            try:
                r, _, _ = sel.select([master_fd], [], [], 0.05)
                if r:
                    data = os.read(master_fd, 4096)
                    if data:
                        asyncio.run_coroutine_threadsafe(websocket.send_bytes(data), loop)
            except OSError:
                break

    reader = threading.Thread(target=read_thread, daemon=True)
    reader.start()

    try:
        while True:
            msg = await websocket.receive()
            if "bytes" in msg and msg["bytes"]:
                os.write(master_fd, msg["bytes"])
            elif "text" in msg and msg["text"]:
                try:
                    data = json.loads(msg["text"])
                    if data.get("type") == "resize":
                        _set_pty_size_unix(master_fd, data.get("rows", 24), data.get("cols", 80))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("terminal_ws (unix) error: %s", exc)
    finally:
        stop_event.set()
        proc.terminate()
        try:
            os.close(master_fd)
        except OSError:
            pass
