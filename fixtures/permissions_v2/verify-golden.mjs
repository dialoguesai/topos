// Independent Node/TypeScript-compatible verification of Python wire bytes.
// This verifies trusted fixture files; JSON.parse alone is NOT a production
// parser (it loses duplicate object keys). Production decoding must reject them.
import { readFileSync } from 'node:fs';
import { createHash, createPublicKey, verify } from 'node:crypto';
import assert from 'node:assert/strict';

function quote(value) {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) throw new Error('json_surrogate');
    } else if (code >= 0xdc00 && code <= 0xdfff) throw new Error('json_surrogate');
  }
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, char => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
}

export function canonical(value, depth = 0) {
  if (depth > 40) throw new Error('json_depth');
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'string') return quote(value);
  if (typeof value === 'number' && Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) return `[${value.map(item => canonical(item, depth + 1)).join(',')}]`;
  if (typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    const keys = Object.keys(value).sort();
    if (keys.some(key => /[^\x00-\x7f]/.test(key))) throw new Error('json_key');
    // Serialize directly. JSON.stringify on an object would reorder numeric keys.
    return `{${keys.map(key => `${quote(key)}:${canonical(value[key], depth + 1)}`).join(',')}}`;
  }
  throw new Error('json_type');
}

const hash = value => createHash('sha256').update(canonical(value), 'ascii').digest('hex');
const golden = JSON.parse(readFileSync(new URL('./golden-v1.json', import.meta.url), 'utf8'));
assert.equal(canonical(golden.policy), golden.policy_canonical);
assert.equal(hash(golden.policy), golden.policy_hash);
assert.equal(hash({request_type: golden.request.request_type, payload: golden.payload}), golden.envelope.request_hash);
const { signature, ...body } = golden.envelope;
const signing = `topos-grantee-envelope/v2\n${canonical(body)}`;
assert.equal(signing, golden.signing_text);
const publicKey = createPublicKey({
  key: Buffer.concat([Buffer.from('302a300506032b6570032100', 'hex'), Buffer.from(golden.public_key_hex, 'hex')]),
  type: 'spki', format: 'der',
});
assert(verify(null, Buffer.from(signing, 'ascii'), publicKey, Buffer.from(signature, 'base64url')));
assert.equal(canonical({'2': 'é', '10': '📚', 'x': '\x7f'}), '{"10":"\\ud83d\\udcda","2":"\\u00e9","x":"\\u007f"}');
assert.throws(() => canonical({value: 1.5}));
assert.throws(() => canonical({value: Number.MAX_SAFE_INTEGER + 1}));
assert.throws(() => canonical({value: '\ud800'}));
console.log('P2a canonical policy, request hash and Ed25519 golden verified in Node');

const protocol = JSON.parse(readFileSync(new URL('./protocol-golden-v1.json', import.meta.url), 'utf8'));
for (const name of ['mutation', 'status_request', 'ack']) {
  const {signature: sig, ...body} = protocol[name];
  const text = `${body.version}\n${canonical(body)}`;
  assert.equal(text, protocol.signing_text[name]);
  const hex = name === 'ack' ? protocol.node_public_key_hex : protocol.cp_public_key_hex;
  const key = createPublicKey({key: Buffer.concat([Buffer.from('302a300506032b6570032100','hex'), Buffer.from(hex,'hex')]), type:'spki',format:'der'});
  assert(verify(null, Buffer.from(text,'ascii'), key, Buffer.from(sig,'base64url')));
}
const {version: _v, kid: _kid, issued_at: _iat, expires_at: _exp, signature: _sig, ...commandCore} = protocol.mutation;
assert.equal(hash(commandCore), protocol.command_hash);
assert.equal(hash(protocol.mutation), protocol.ack.response_to);
console.log('Mutation/status/ACK signatures and semantic command hash verified in Node');
