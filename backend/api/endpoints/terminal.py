"""WebSocket terminal endpoint — PTY-backed shell sessions."""
import asyncio
import fcntl
import json
import logging
import os
import pty
import select as sel
import shutil
import struct
import subprocess
import termios
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger(__name__)


def _available_shells() -> list[str]:
    found = []
    for sh in ['bash', 'zsh', 'fish', 'sh']:
        if shutil.which(sh):
            found.append(sh)
    return found or ['sh']


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    try:
        size = struct.pack('HHHH', rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except Exception:
        pass


@router.get("/shells")
async def list_shells():
    shells = _available_shells()
    return {"shells": shells, "default": shells[0]}


@router.websocket("/ws")
async def terminal_ws(websocket: WebSocket, shell: str = ""):
    await websocket.accept()

    available = _available_shells()
    if not shell or shell not in available:
        shell = available[0]

    master_fd, slave_fd = pty.openpty()
    _set_pty_size(master_fd, 24, 80)

    env = {
        **os.environ,
        'TERM': 'xterm-256color',
        'COLORTERM': 'truecolor',
        'LANG': 'en_US.UTF-8',
    }
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

    def read_thread():
        while not stop_event.is_set() and proc.poll() is None:
            try:
                r, _, _ = sel.select([master_fd], [], [], 0.05)
                if r:
                    data = os.read(master_fd, 4096)
                    if data:
                        asyncio.run_coroutine_threadsafe(
                            websocket.send_bytes(data), loop
                        )
            except OSError:
                break

    reader = threading.Thread(target=read_thread, daemon=True)
    reader.start()

    try:
        while True:
            msg = await websocket.receive()
            if 'bytes' in msg and msg['bytes']:
                os.write(master_fd, msg['bytes'])
            elif 'text' in msg and msg['text']:
                try:
                    data = json.loads(msg['text'])
                    if data.get('type') == 'resize':
                        _set_pty_size(master_fd, data.get('rows', 24), data.get('cols', 80))
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("terminal_ws error: %s", e)
    finally:
        stop_event.set()
        proc.terminate()
        try:
            os.close(master_fd)
        except OSError:
            pass
