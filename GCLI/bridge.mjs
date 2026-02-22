/**
 * GCLI Bridge — OAuth-based backend using the Gemini CLI's Code Assist endpoint.
 * No API key required. Uses the OAuth session from `gemini` CLI.
 * Google One AI Pro tier → no free-tier rate limits.
 *
 * Protocol: newline-delimited JSON on stdin/stdout.
 *   OUT: {"ready":true,"email":"...","tier":"...","mode":"oauth"}   (on startup)
 *   IN:  {"id":"1","method":"generate","contents":[...],"model":"...","systemPrompt":"...","tools":[...]}
 *   OUT: {"id":"1","part": {"text": "..."}}
 *   OUT: {"id":"1","part": {"functionCall": {...}}}
 *   OUT: {"id":"1","done": true}
 *   OUT: {"id":"1","error": "..."}
 */
import { createInterface } from 'readline';
import { promises as fs } from 'fs';
import * as path from 'path';
import * as os from 'os';

const HOME = os.homedir();
const APPDATA = process.env['APPDATA'] || path.join(HOME, 'AppData', 'Roaming');
const toFile = p => `file:///${p.replace(/\\/g, '/')}`;

const GEMINI_CLI = path.join(APPDATA, 'npm', 'node_modules', '@google', 'gemini-cli');
const CORE = path.join(GEMINI_CLI, 'node_modules', '@google', 'gemini-cli-core', 'dist', 'src');
const AUTH_LIB = path.join(GEMINI_CLI, 'node_modules', 'google-auth-library', 'build', 'src', 'index.js');

const { OAuth2Client } = await import(toFile(AUTH_LIB));
const { OAuthCredentialStorage } = await import(toFile(path.join(CORE, 'code_assist', 'oauth-credential-storage.js')));
const { CodeAssistServer } = await import(toFile(path.join(CORE, 'code_assist', 'server.js')));
const { setupUser } = await import(toFile(path.join(CORE, 'code_assist', 'setup.js')));

const OAUTH_CLIENT_ID = '681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com';
const OAUTH_CLIENT_SECRET = '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';
const ACCOUNTS_PATH = path.join(HOME, '.gemini', 'google_accounts.json');

function send(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }
async function getEmail() {
    try { return JSON.parse(await fs.readFile(ACCOUNTS_PATH, 'utf-8')).active ?? ''; }
    catch { return ''; }
}

let server = null;
let email = '';
const pendingQueue = [];
let initDone = false;

async function init() {
    const creds = await OAuthCredentialStorage.loadCredentials();
    if (!creds?.refresh_token) {
        send({
            ready: false, error: 'NO_OAUTH_CREDS',
            message: 'No OAuth session. Run `gemini` once to sign in, then restart GCLI.'
        });
        process.exit(1);
    }
    const client = new OAuth2Client({ clientId: OAUTH_CLIENT_ID, clientSecret: OAUTH_CLIENT_SECRET });
    client.setCredentials(creds);
    client.on('tokens', async t => {
        try { await OAuthCredentialStorage.saveCredentials({ ...creds, ...t }); } catch { }
    });
    await client.getAccessToken();

    const userData = await setupUser(client, undefined);
    server = new CodeAssistServer(client, userData.projectId, {}, undefined, userData.userTier, userData.userTierName);
    email = await getEmail();
    initDone = true;
    send({ ready: true, email, tier: userData.userTierName ?? 'free', mode: 'oauth' });

    for (const msg of pendingQueue) await handleMsg(msg);
}

// Map Python snake_case tools -> node.js camelCase
function adaptTools(tools) {
    if (!tools) return undefined;
    return tools.map(t => {
        let fns = t.function_declarations || t.functionDeclarations || [];
        return {
            functionDeclarations: fns.map(fn => ({
                name: fn.name,
                description: fn.description,
                parameters: fn.parameters
            }))
        };
    });
}

async function handleMsg(msg) {
    if (msg.method === 'ping') {
        send({ pong: true, email, ready: initDone, mode: 'oauth' }); return;
    }
    if (msg.method === 'generate') {
        try {
            const config = {};
            if (msg.systemPrompt) config.systemInstruction = { parts: [{ text: msg.systemPrompt }] };
            if (msg.tools && msg.tools.length > 0) {
                config.tools = adaptTools(msg.tools);
                config.toolConfig = { functionCallingConfig: { mode: 'AUTO' } };
            }

            const req = {
                model: msg.model || 'gemini-2.0-flash',
                contents: msg.contents,
                ...(Object.keys(config).length > 0 ? { config } : {})
            };

            const stream = await server.generateContentStream(req);
            for await (const chunk of stream) {
                const parts = chunk.candidates?.[0]?.content?.parts || [];
                for (const p of parts) {
                    send({ id: msg.id, part: p });
                }
            }
            send({ id: msg.id, done: true });
        } catch (err) {
            send({ id: msg.id, error: String(err) });
        }
        return;
    }
    send({ error: `Unknown method: ${msg.method}` });
}

init().catch(err => { send({ ready: false, error: String(err) }); process.exit(1); });

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of rl) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let msg;
    try { msg = JSON.parse(trimmed); } catch { send({ error: 'Invalid JSON' }); continue; }
    if (!initDone) { pendingQueue.push(msg); }
    else { await handleMsg(msg); }
}
