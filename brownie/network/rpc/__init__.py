#!/usr/bin/python3

import atexit
import inspect
import socket
import time
import warnings
from typing import Any, Callable, Dict, Tuple, Union
from urllib.parse import urlparse

import psutil

from brownie._singleton import _Singleton
from brownie.exceptions import RPCConnectionError, RPCProcessError
from brownie.network.state import Chain
from brownie.network.web3 import web3

from . import ganache, geth

chain = Chain()

ATTACH_BACKENDS = {
    "ethereumjs testrpc": ganache,
    "geth": geth,
}

LAUNCH_BACKENDS = {
    "ganache": ganache,
    "ethnode": geth,
    "geth": geth,
}


def internal(fn: Callable) -> Callable:
    """
    Decorator that warns when a user calls directly to Rpc methods.
    """

    def wrapped(*args: Any, **kwargs: Any) -> Callable:
        if inspect.stack()[1].frame.f_locals.get("self", None) != chain:
            warnings.warn(
                f"rpc.{fn.__name__} should not be called directly, use chain.{fn.__name__} instead",
                FutureWarning,
                stacklevel=2,
            )
        return fn(*args, **kwargs)

    return wrapped


class Rpc(metaclass=_Singleton):
    def __init__(self):
        self.process = None
        self.backend = ganache
        atexit.register(self._at_exit)

    def _at_exit(self) -> None:
        if not self.is_active():
            return
        if self.process.parent() == psutil.Process():
            if getattr(self.process, "stdout", None) is not None:
                self.process.stdout.close()
            if getattr(self.process, "stderr", None) is not None:
                self.process.stderr.close()
            self.kill(False)

    def launch(self, cmd: str, **kwargs: Dict) -> None:
        if self.is_active():
            raise SystemError("RPC is already active.")

        for key, module in LAUNCH_BACKENDS.items():
            if cmd.lower().startswith(key):
                self.backend = module
                break

        self.process = self.backend.launch(cmd, **kwargs)

        # check that web3 can connect
        if not web3.provider:
            chain._network_disconnected()
            return
        uri = web3.provider.endpoint_uri if web3.provider else None
        for i in range(100):
            if web3.isConnected():
                chain._network_connected()
                self.backend.on_connection()
                return
            time.sleep(0.1)
            if isinstance(self.process, psutil.Popen):
                self.process.poll()
            if not self.process.is_running():
                self.kill(False)
                raise RPCProcessError(cmd, uri)
        self.kill(False)
        raise RPCConnectionError(cmd, self.process, uri)

    def attach(self, laddr: Union[str, Tuple]) -> None:
        """Attaches to an already running RPC client subprocess.

        Args:
            laddr: Address that the client is listening at. Can be supplied as a
                   string "http://127.0.0.1:8545" or tuple ("127.0.0.1", 8545)"""
        if self.is_active():
            raise SystemError("RPC is already active.")
        if isinstance(laddr, str):
            o = urlparse(laddr)
            if not o.port:
                raise ValueError("No RPC port given")
            laddr = (o.hostname, o.port)

        ip = socket.gethostbyname(laddr[0])
        resolved_addr = (ip, laddr[1])
        pid = _find_rpc_process_pid(resolved_addr)

        print(f"Attached to local RPC client listening at '{laddr[0]}:{laddr[1]}'...")
        self.process = psutil.Process(pid)

        for key, module in ATTACH_BACKENDS.items():
            if web3.clientVersion.lower().startswith(key):
                self.backend = module
                break

        chain._network_connected()
        self.backend.on_connection()

    def kill(self, exc: bool = True) -> None:
        """Terminates the RPC process and all children with SIGKILL.

        Args:
            exc: if True, raises SystemError if subprocess is not active."""
        if not self.is_active():
            if not exc:
                return
            raise SystemError("RPC is not active.")

        try:
            print("Terminating local RPC client...")
        except ValueError:
            pass
        for child in self.process.children():
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        self.process.kill()
        self.process.wait()
        self.process = None
        chain._network_disconnected()

    def is_active(self) -> bool:
        """Returns True if Rpc client is currently active."""
        if not self.process:
            return False
        if isinstance(self.process, psutil.Popen):
            self.process.poll()
        return self.process.is_running()

    def is_child(self) -> bool:
        """Returns True if the Rpc client is active and was launched by Brownie."""
        if not self.is_active():
            return False
        return self.process.parent() == psutil.Process()

    @internal
    def sleep(self, seconds: int) -> int:
        return self.backend.sleep(seconds)

    @internal
    def mine(self, blocks: int = 1) -> int:
        self.backend.mine(blocks)
        return web3.eth.blockNumber

    @internal
    def snapshot(self) -> int:
        return self.backend.snapshot()

    @internal
    def revert(self, snapshot_id) -> int:
        self.backend.revert(snapshot_id)
        return web3.eth.blockNumber

    def unlock_account(self, address: str) -> None:
        self.backend.unlock_account(address)


def _find_rpc_process_pid(laddr: Tuple) -> int:
    try:
        proc = next(i for i in psutil.process_iter() if _check_proc_connections(i, laddr))
        return proc.pid
    except StopIteration:
        try:
            proc = next(
                i for i in psutil.net_connections(kind="tcp") if _check_net_connections(i, laddr)
            )
            return proc.pid
        except StopIteration:
            raise ProcessLookupError(
                "Could not attach to RPC process. If this issue persists, try killing "
                "the RPC and let Brownie launch it as a child process."
            ) from None


def _check_proc_connections(proc: psutil.Process, laddr: Tuple) -> bool:
    try:
        return laddr in [i.laddr for i in proc.connections()]
    except psutil.AccessDenied:
        return False
    except psutil.ZombieProcess:
        return False
    except psutil.NoSuchProcess:
        return False


def _check_net_connections(connection: Any, laddr: Tuple) -> bool:
    if connection.pid is None:
        return False
    if connection.laddr == laddr:
        return True
    elif connection.raddr == laddr:
        return True
    else:
        return False
