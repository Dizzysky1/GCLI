/**
 * Reads the Gemini CLI's stored API key from Windows Credential Manager
 * and prints it to stdout. Exit 0 = success, Exit 1 = not found.
 */
import * as path from 'path';
import * as os from 'os';

const HOME = os.homedir();
const APPDATA = process.env['APPDATA'] || path.join(HOME, 'AppData', 'Roaming');
const GEMINI_CLI = path.join(APPDATA, 'npm', 'node_modules', '@google', 'gemini-cli');

// The internal path structure can vary by version, but usually it's here:
const CORE = path.join(GEMINI_CLI, 'node_modules', '@google', 'gemini-cli-core', 'dist', 'src');

try {
    const { loadApiKey } = await import(`file:///${CORE.replace(/\\/g, '/')}/core/apiKeyCredentialStorage.js`);
    const key = await loadApiKey();
    if (key) {
        process.stdout.write(key);
        process.exit(0);
    }
} catch (e) {
    // If import fails or key not found
}
process.exit(1);