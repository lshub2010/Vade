# vade-sdk (Python)

Minimal module SDK for Vade: connect to core, register tools, answer calls.
The frozen interface is documented in [`../PYTHON_SDK.md`](../PYTHON_SDK.md) —
read that first; this file is only the packaging note.

```bash
pip install ./sdk/python                # standalone + manifest only
pip install './sdk/python[core]'        # + websockets + cryptography (connect to core)
pytest sdk/python/tests -q
```

Zero hard dependencies. `--standalone` runs on a bare Python 3.10+; connecting
additionally needs the `vade_protocol` binding (C-01) built with
`cargo build --features uniffi` in `protocol/core` and the resulting
`libvade_protocol.so` next to `vade_protocol.py`.

Layout: `app.py` (lifecycle + dispatch), `spec.py` (signature → JSON Schema),
`transport.py` (WS + Noise + reconnect), `standalone.py` (local CLI),
`logging.py` (JSON lines), `_pairing.py` (C-15 normalisation/PSK, KAT-verified).

`tests/e2e_real_core.py` is the G1 end-to-end check against a real core; it is
not collected by pytest (needs uvicorn, the binding and two ports):

```bash
PYTHONPATH=core VADE_PROTOCOL_PY=<dir with vade_protocol.py + .so> \
  python3 sdk/python/tests/e2e_real_core.py
```
