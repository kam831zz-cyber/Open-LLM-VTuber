"""Process-local trial restrictions; no persistent settings or credentials."""
import os
from pathlib import Path
import sys


def install(runtime, *, port=None, ollama=False):
    runtime = Path(runtime).resolve()
    def inside(value):
        try:
            return Path(value).resolve().is_relative_to(runtime)
        except (TypeError, ValueError):
            return False
    def audit(event, args):
        if event == 'open':
            path, mode, flags = args
            writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
            if writing and not isinstance(path, int) and not inside(path) and str(path).lower() not in ('nul', os.devnull):
                raise PermissionError('trial_write_outside_runtime')
        if event in ('os.remove', 'os.rmdir', 'os.mkdir') and not inside(args[0]):
            raise PermissionError('trial_mutation_outside_runtime')
        if event in ('os.rename', 'os.replace') and not all(inside(p) for p in args[:2]):
            raise PermissionError('trial_move_outside_runtime')
        if event == 'socket.connect':
            frame = sys._getframe()
            while frame:
                if frame.f_code.co_name == 'socketpair' and Path(frame.f_code.co_filename).name == 'socket.py':
                    return
                frame = frame.f_back
            address = args[1]
            if not (ollama and isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1') and address[1] == 11434):
                raise PermissionError('trial_network_forbidden')
        if event == 'socket.bind':
            address = args[1]
            # Windows asyncio creates its private socketpair via loopback port 0.
            if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1') and address[1] == 0:
                return
            if not (port and isinstance(address, tuple) and address == ('127.0.0.1', port)):
                raise PermissionError('trial_bind_forbidden')
        if event == 'subprocess.Popen':
            command = args[1]
            if not (port and isinstance(command, (list, tuple)) and len(command) >= 6
                    and Path(command[0]).resolve() == Path(sys.executable).resolve()
                    and command[1:3] == ['-I', '-B']
                    and Path(command[3]).resolve() == Path(__file__).resolve()
                    and command[4] == '--child'):
                raise PermissionError('trial_subprocess_forbidden')
        if event == 'os.system':
            raise PermissionError('trial_shell_forbidden')
    sys.addaudithook(audit)


if __name__ == '__main__':
    # Stdio handshake/list-tools only. No tool requests are sent by the trial.
    import runpy
    runtime, kind, target = sys.argv[2:5]
    os.chdir(runtime)
    install(runtime)
    if kind == 'script':
        runpy.run_path(target, run_name='__main__')
    elif kind == 'ddg':
        from duckduckgo_mcp_server.server import main
        main()
    else:
        raise SystemExit('unknown_child_entry')
