# Python 绑定（C1 T5）

生成物：`vade_protocol.py`（UniFFI 0.32.2 生成），加载同目录的 `libvade_protocol.so`。

重建：

```bash
cd protocol/core
CARGO_PROFILE_DEV_DEBUG=0 cargo build --features uniffi -j 1 --lib --bin uniffi-bindgen
./target/debug/uniffi-bindgen generate --library target/debug/libvade_protocol.so \
  --language python --out-dir python
cp target/debug/libvade_protocol.so python/
PYTHONPATH=python python3 -c "import vade_protocol as v; print(v.version(), v.protocol_version())"
```

注意：
- 本机 5GB 内存，必须 `-j 1` + `CARGO_PROFILE_DEV_DEBUG=0`，否则 `uniffi_bindgen` 会被 OOM 杀掉。
- `.so` 不入库（见 `.gitignore`），只提交生成的 `.py`。
- wheel 打包未做（C4 再处理）。
