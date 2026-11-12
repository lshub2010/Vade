// C0b · Kotlin/Wasm 的 JS 适配层（T3）
//
// 契约只有一条：**default 导出必须是一个函数**。Kotlin/Wasm 的 `@JsModule`
// 外部函数编译成 `_ref.default(args)`（具名导出根本不会被读取），所以这里
// 不做“API 对象”，只做一次真实的 Noise IKpsk2 握手 + 一轮 AEAD 往返，
// 结果作为对象返回，Kotlin 侧用属性读回。
//
// 传输放在内存里：T2 已用真实 WebSocket 证明宿主传输；T3 要证明的是
// Kotlin/Wasm → JS → Rust 这条调用链，不是再证一次 socket。
//
// vendor/ 是 wasm-bindgen 产物的副本：不依赖 node_modules，干净检出即可跑。

import init, { Peer, Role } from './vendor/protocol-wasm/protocol_wasm.js';
import { WASM_BASE64 } from './vendor/protocol-wasm/wasm-b64.js';

let ready = null;

function ensure() {
  if (ready === null) {
    const bin = atob(WASM_BASE64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
    ready = init({ module_or_path: bytes });
  }
  return ready;
}

const enc = new TextEncoder();
const dec = new TextDecoder();

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

// Kotlin/Wasm 调用的就是这个函数本身（module.default）。
export default async function runHandshake(pskHex) {
  console.log('BRIDGE_ENTER');
  await ensure();

  const psk =
    pskHex ??
    Array.from({ length: 32 }, (_, i) => i.toString(16).padStart(2, '0')).join('');

  const responder = new Peer(Role.Responder, undefined, psk);
  const initiator = new Peer(Role.Initiator, hexToBytes(responder.static_public_key_hex()), psk);

  const m1 = initiator.write_handshake();
  responder.read_handshake(m1);
  const m2 = responder.write_handshake();
  initiator.read_handshake(m2);

  const ciphertext = initiator.encrypt(enc.encode('hello from kotlin/wasm'));
  const plaintext = dec.decode(responder.decrypt(ciphertext));
  const echo = responder.encrypt(enc.encode(plaintext));
  const roundtrip = dec.decode(initiator.decrypt(echo));

  console.log('BRIDGE_HANDSHAKE_DONE', plaintext, roundtrip);

  return {
    text: JSON.stringify({
      ok: plaintext === 'hello from kotlin/wasm' && roundtrip === plaintext,
      echo: roundtrip,
      handshake_bytes: m1.length + m2.length,
      pattern: 'IKpsk2',
    }),
  };
}
