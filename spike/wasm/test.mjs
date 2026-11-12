// C0b T2 acceptance test.
//
// Proves, in Node (the same V8 that hosts Compose/Wasm on the browser side):
//   1. load the wasm-bindgen ES module from disk (no bundler, no server);
//   2. run a real Noise IK handshake where the *transport is JS* — Rust asks
//      JS to send bytes and awaits a JS Promise for the reply;
//   3. actually carry those bytes over a real WebSocket (ws server on loopback);
//   4. AEAD-encrypt a frame, receive the echo, decrypt it, and fire host callbacks.
//
// The responder lives in the same wasm module but is a separate Peer object, so
// both sides of the Noise state machine run the real Rust code.

import { readFile, stat } from 'node:fs/promises';
import { gzipSync } from 'node:zlib';
import { once } from 'node:events';
import { WebSocketServer, WebSocket } from 'ws';

import init, {
  handshake,
  encrypt,
  decrypt,
  on_event,
  selftest,
  last_error,
  Peer,
  Role,
} from './protocol-wasm/pkg/protocol_wasm.js';

const WASM = new URL('./protocol-wasm/pkg/protocol_wasm_bg.wasm', import.meta.url);
const GLUE = new URL('./protocol-wasm/pkg/protocol_wasm.js', import.meta.url);

// 000102...1f, generated so the literal cannot be normalised by tooling.
const PSK_HEX = Array.from({ length: 32 }, (_, i) => i.toString(16).padStart(2, '0')).join('');
const dec = new TextDecoder();

let failures = 0;
function check(name, ok, detail = '') {
  if (ok) {
    console.log(`PASS ${name}${detail ? ' ' + detail : ''}`);
  } else {
    failures += 1;
    console.log(`FAIL ${name}${detail ? ' ' + detail : ''}`);
  }
}

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i += 1) {
    out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

await init({ module_or_path: await readFile(WASM) });

// ---------------------------------------------------------------- 0. selftest
{
  const s = JSON.parse(selftest());
  check('selftest_in_module', s.ok === true, JSON.stringify(s));
}

// ------------------------------------------------- 1. IKpsk2 both directions
{
  const responder = new Peer(Role.Responder, undefined, PSK_HEX);
  const initiator = new Peer(Role.Initiator, hexToBytes(responder.static_public_key_hex()), PSK_HEX);
  const m1 = initiator.write_handshake();
  responder.read_handshake(m1);
  const m2 = responder.write_handshake();
  initiator.read_handshake(m2);
  const ct = initiator.encrypt(new TextEncoder().encode('psk-frame'));
  const pt = dec.decode(responder.decrypt(ct));
  check('psk2_roundtrip', pt === 'psk-frame', `pt=${pt} m1=${m1.length}B m2=${m2.length}B`);
}

// ------------------------------------------------- 2. wrong PSK must not open
// Noise IKpsk2 mixes the PSK in the *responder's* message (the `psk2` modifier
// appends Token::Psk to msg2), so msg1 is identical whatever the PSK is — a
// mismatch can only surface when the exchange completes. What must hold is the
// security property, not the timing of the error: the peer holding the wrong PSK
// must fail to reach transport mode.
{
  const responder = new Peer(Role.Responder, undefined, PSK_HEX);
  const badPsk = 'ff'.repeat(32);
  const initiator = new Peer(Role.Initiator, hexToBytes(responder.static_public_key_hex()), badPsk);
  const m1 = initiator.write_handshake();
  let rejected = false;
  try {
    responder.read_handshake(m1);
    const m2 = responder.write_handshake();
    initiator.read_handshake(m2);
  } catch {
    rejected = true;
  }
  check(
    'psk_mismatch_rejected',
    rejected && !initiator.is_established(),
    `rejected=${rejected} initiator_established=${initiator.is_established()}`,
  );
}

// ------------------------------------------------------- 3. WebSocket handshake
const responder = new Peer(Role.Responder, undefined, PSK_HEX);
const responderPub = responder.static_public_key_hex();

const wss = new WebSocketServer({ host: '127.0.0.1', port: 0 });
await once(wss, 'listening');
const port = wss.address().port;

let serverError = null;
wss.on('connection', (ws) => {
  ws.on('message', (data) => {
    try {
      const bytes = new Uint8Array(data);
      if (!responder.is_established()) {
        responder.read_handshake(bytes);
        if (!responder.is_established()) ws.send(responder.write_handshake());
        return;
      }
      const plaintext = responder.decrypt(bytes);
      ws.send(responder.encrypt(plaintext)); // echo
    } catch (e) {
      serverError = String(e);
    }
  });
});

const events = [];
on_event((ev) => {
  events.push(ev.name);
  console.log(`EVENT ${ev.name} ${ev.payload}`);
});

const ws = new WebSocket(`ws://127.0.0.1:${port}`);
await once(ws, 'open');

const inbox = [];
const waiters = [];
ws.on('message', (data) => {
  const bytes = new Uint8Array(data);
  if (waiters.length) waiters.shift()(bytes);
  else inbox.push(bytes);
});

function recv() {
  return new Promise((resolve, reject) => {
    if (inbox.length) return resolve(inbox.shift());
    const t = setTimeout(() => reject(new Error('transport.recv timed out')), 5000);
    waiters.push((b) => {
      clearTimeout(t);
      resolve(b);
    });
  });
}

const transport = {
  remoteStaticKey: () => hexToBytes(responderPub),
  send: (bytes) => ws.send(bytes),
  recv,
};

const t0 = performance.now();
let info;
try {
  info = await handshake(transport, PSK_HEX);
} catch (e) {
  console.log('handshake threw:', e, 'last_error:', last_error());
  throw e;
}
const hsMs = performance.now() - t0;
check('HANDSHAKE_OK', JSON.parse(info).ok === true, info);
console.log(`HANDSHAKE_MS ${hsMs.toFixed(2)}`);

// ------------------------------------------------ 4. AEAD frame over the socket
const plaintext = new TextEncoder().encode('hello vade');
const ciphertext = encrypt(plaintext);
ws.send(ciphertext);
const echo = await recv();
const roundtrip = dec.decode(decrypt(echo));
check('ECHO_OK', roundtrip === 'hello vade', `ct=${ciphertext.length}B`);

// -------------------------------------------------- 5. tamper must be rejected
{
  const tampered = Uint8Array.from(ciphertext);
  tampered[tampered.length - 1] ^= 0x01;
  let rejected = false;
  try {
    decrypt(tampered);
  } catch {
    rejected = true;
  }
  check('tamper_rejected', rejected);
}

// ------------------------------------------------------- 6. callback plumbing
check(
  'js_callbacks_fired',
  events.includes('handshake.done') && events.includes('frame.received'),
  `events=${JSON.stringify(events)}`,
);

// --------------------------------------------------------------- 7. timing
// Measured on a dedicated session. The tamper check above deliberately performs a
// failed open, and Noise advances the receive nonce even when authentication
// fails (nonce reuse must be impossible), so the WebSocket session is intentionally
// unusable afterwards — a real peer tears the connection down at that point.
{
  const budget = 1000;
  const pt = new Uint8Array(budget).fill(7);
  const n = 2000;
  const r = new Peer(Role.Responder, undefined, PSK_HEX);
  const i = new Peer(Role.Initiator, hexToBytes(r.static_public_key_hex()), PSK_HEX);
  r.read_handshake(i.write_handshake());
  i.read_handshake(r.write_handshake());
  const t = performance.now();
  for (let k = 0; k < n; k += 1) {
    r.decrypt(i.encrypt(pt));
  }
  const per = (performance.now() - t) / n;
  console.log(`AEAD_1KB_MS ${per.toFixed(3)} (${budget}-byte payload, ${n} iterations)`);
}

// ---------------------------------------------------------------- 8. sizes
{
  const wasmStat = await stat(WASM);
  const glueStat = await stat(GLUE);
  const wasmBytes = await readFile(WASM);
  const gz = gzipSync(wasmBytes, { level: 9 }).length;
  console.log(
    `SIZE wasm=${wasmStat.size}B wasm_gzip=${gz}B glue=${glueStat.size}B total=${wasmStat.size + glueStat.size}B`,
  );
}

// ----------------------------------------------------------- 9. error surface
{
  const before = last_error();
  check('no_stray_errors', serverError === null, serverError ?? `last_error=${before ?? 'none'}`);
}

ws.close();
wss.close();

if (failures > 0) {
  console.log(`RESULT FAIL (${failures} failing checks)`);
  process.exit(1);
}
console.log('RESULT OK');
