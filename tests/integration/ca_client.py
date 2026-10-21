"""A small Channel Access client for the container tests, printing JSON.

It runs inside the container (``/opt/scdemo/ca-venv/bin/python``, which has
aioca pinned by ``uv.lock``) with the CA environment of ``/etc/scdemo/ca.env``.
``get`` and ``wait`` give values as text: an mbbi or bi gives its state
string, a char waveform (``LOG``, ``EVENTS``, the ``longStringIn`` texts) its
string. ``get --int`` gives numbers instead (a bi or mbbi as its index).

    ca_client.py get [--int] PV...               {"PV": value, ...}
    ca_client.py put PV VALUE [--wait] [--timeout S]   {"pv", "ok", "error", "t"}
    ca_client.py connect PV...                   ["PV that did not connect", ...]
    ca_client.py wait PV VALUE --timeout S       {"pv", "ok", "value"}
    ca_client.py putwait PUT_PV PUT_VALUE PV VALUE --timeout S
                                {"pv", "ok", "value", "t", "put_ok", "error"}
    ca_client.py hold --for S PV=VALUE...        {"ok", "broken"}

``t`` is this host's ``time.time()`` just before the put, on the same clock
as the tool's event ``ts``. ``putwait`` subscribes to ``PV`` first, so a
transient value caused by the put (``stopping``) is not missed. ``hold``
fails if any PV reads anything else (or disconnects) during ``S`` seconds;
``broken`` names the first such PV and value.

Give ``-`` as the only PV to read whitespace-separated PV names from stdin.
``put`` and ``wait`` exit 1 when they fail, ``get`` when any PV fails.
"""

import argparse
import asyncio
import contextlib
import json
import sys
import time
from collections.abc import Sequence

from aioca import DBR_CHAR_STR, CANothing, caget, camonitor, caput, connect

DEFAULT_TIMEOUT = 5.0


def _pvs(names: Sequence[str]) -> list[str]:
    if list(names) == ["-"]:
        return sys.stdin.read().split()
    return list(names)


async def _get_one(pv: str, limit: float) -> str | None:
    """The value as text, or ``None`` if it could not be read."""
    value = await caget(pv, datatype=str, timeout=limit, throw=False)
    if isinstance(value, CANothing):
        return None
    if isinstance(value, str):
        return str(value)
    # An array: a char waveform, read again as one string.
    text = await caget(pv, datatype=DBR_CHAR_STR, timeout=limit, throw=False)
    return None if isinstance(text, CANothing) else str(text)


async def _get_int(pv: str, limit: float) -> int | None:
    """The value as an integer, or ``None`` if it could not be read."""
    value = await caget(pv, datatype=int, timeout=limit, throw=False)
    return None if isinstance(value, CANothing) else int(value)


async def get(pvs: Sequence[str], limit: float, *, as_int: bool) -> int:
    one = _get_int if as_int else _get_one
    values = await asyncio.gather(*(one(pv, limit) for pv in pvs))
    print(json.dumps(dict(zip(pvs, values, strict=True))))
    return 0 if all(v is not None for v in values) else 1


async def put(pv: str, value: str, *, wait: bool, limit: float) -> int:
    t = time.time()
    result = await caput(pv, value, wait=wait, timeout=limit, throw=False)
    error = None if result.ok else str(result)
    print(json.dumps({"pv": pv, "ok": bool(result.ok), "error": error, "t": t}))
    return 0 if result.ok else 1


async def put_and_wait(
    put_pv: str, put_value: str, pv: str, wanted: str, limit: float
) -> int:
    """Monitor ``pv``, put ``put_value`` to ``put_pv``, then wait for ``wanted``."""
    connected = asyncio.Event()
    matched = asyncio.Event()
    last: list[str | None] = [None]

    def on_value(value: object) -> None:
        if isinstance(value, CANothing):  # a disconnect
            last[0] = None
            return
        connected.set()
        last[0] = str(value)
        if last[0] == wanted:
            matched.set()

    subscription = camonitor(
        pv, on_value, datatype=str, notify_disconnect=True, connect_timeout=limit
    )
    t = time.time()
    put_ok = False
    error: str | None = None
    try:
        async with asyncio.timeout(limit):
            await connected.wait()  # subscribed: the put's effect will be seen
            t = time.time()
            result = await caput(put_pv, put_value, timeout=limit, throw=False)
            put_ok = bool(result.ok)
            error = None if result.ok else str(result)
            if put_ok:
                await matched.wait()
        ok = put_ok
    except TimeoutError:
        ok = False
    finally:
        subscription.close()
    out = {"pv": pv, "ok": ok, "value": last[0], "t": t, "put_ok": put_ok}
    print(json.dumps({**out, "error": error}))
    return 0 if ok else 1


async def hold(pairs: Sequence[str], seconds: float) -> int:
    """Check every ``PV=VALUE`` keeps its value for ``seconds``."""
    wanted = dict(pair.split("=", 1) for pair in pairs)
    pvs = list(wanted)
    broken: list[dict[str, str | None]] = []
    first = [False] * len(pvs)
    done = asyncio.Event()

    def on_value(value: object, index: int) -> None:
        pv = pvs[index]
        text = None if isinstance(value, CANothing) else str(value)
        first[index] = True
        if text != wanted[pv] and not broken:
            broken.append({"pv": pv, "value": text})
            done.set()

    subscriptions = camonitor(
        pvs, on_value, datatype=str, notify_disconnect=True, connect_timeout=5.0
    )
    try:
        with contextlib.suppress(TimeoutError):  # the window ran out: held
            async with asyncio.timeout(seconds):
                await done.wait()
    finally:
        for sub in subscriptions:
            sub.close()
    if not broken and not all(first):
        missing = pvs[first.index(False)]
        broken.append({"pv": missing, "value": None})
    print(json.dumps({"ok": not broken, "broken": broken[0] if broken else None}))
    return 0 if not broken else 1


async def connect_all(pvs: Sequence[str], limit: float) -> int:
    results = await connect(list(pvs), timeout=limit, throw=False)
    print(json.dumps([r.name for r in results if not r.ok]))
    return 0


async def wait_for(pv: str, wanted: str, limit: float) -> int:
    """Wait until ``pv`` reads ``wanted`` (as text) or ``limit`` seconds pass."""
    matched = asyncio.Event()
    last: list[str | None] = [None]

    def on_value(value: object) -> None:
        if isinstance(value, CANothing):  # a disconnect
            last[0] = None
            return
        last[0] = str(value)
        if last[0] == wanted:
            matched.set()

    subscription = camonitor(
        pv, on_value, datatype=str, notify_disconnect=True, connect_timeout=limit
    )
    try:
        async with asyncio.timeout(limit):
            await matched.wait()
        ok = True
    except TimeoutError:
        ok = False
    finally:
        subscription.close()
    print(json.dumps({"pv": pv, "ok": ok, "value": last[0]}))
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ca_client.py")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("get", "connect"):
        sub = commands.add_parser(name)
        sub.add_argument("pvs", nargs="+")
        sub.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
        if name == "get":
            sub.add_argument("--int", action="store_true", dest="as_int")
    put_cmd = commands.add_parser("put")
    put_cmd.add_argument("pv")
    put_cmd.add_argument("value")
    put_cmd.add_argument("--wait", action="store_true")
    put_cmd.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    wait_cmd = commands.add_parser("wait")
    wait_cmd.add_argument("pv")
    wait_cmd.add_argument("value")
    wait_cmd.add_argument("--timeout", type=float, required=True)
    putwait_cmd = commands.add_parser("putwait")
    for name in ("put_pv", "put_value", "pv", "value"):
        putwait_cmd.add_argument(name)
    putwait_cmd.add_argument("--timeout", type=float, required=True)
    hold_cmd = commands.add_parser("hold")
    hold_cmd.add_argument("pairs", nargs="+", metavar="PV=VALUE")
    hold_cmd.add_argument("--for", type=float, required=True, dest="seconds")
    args = parser.parse_args(argv)
    if args.command == "get":
        return asyncio.run(get(_pvs(args.pvs), args.timeout, as_int=args.as_int))
    if args.command == "connect":
        return asyncio.run(connect_all(_pvs(args.pvs), args.timeout))
    if args.command == "put":
        return asyncio.run(put(args.pv, args.value, wait=args.wait, limit=args.timeout))
    if args.command == "putwait":
        return asyncio.run(
            put_and_wait(args.put_pv, args.put_value, args.pv, args.value, args.timeout)
        )
    if args.command == "hold":
        return asyncio.run(hold(args.pairs, args.seconds))
    return asyncio.run(wait_for(args.pv, args.value, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
