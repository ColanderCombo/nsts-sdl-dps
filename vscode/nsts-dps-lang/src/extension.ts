import * as path from 'path';
import * as fs from 'fs';
import { execFileSync } from 'child_process';
import * as vscode from 'vscode';
import {
    LanguageClient,
    LanguageClientOptions,
    ServerOptions,
    TransportKind,
} from 'vscode-languageclient/node';

import { RevisionDecorator } from './revisionDecorations';
import { activateHalsFeatures } from './halsFeatures';
import { activateConcardFeatures } from './concardFeatures';

// Card source formats that carry a 6-digit sequence + 2-letter revision trailer.
const CARD_LANGUAGES = new Set(['hals', 'dfg', 'ap101asm', 'concard', 'mmubuild']);

let halsClient: LanguageClient | undefined;
let ap101Client: LanguageClient | undefined;

// ---------------------------------------------------------------------------
// HAL/S language server:
// ---------------------------------------------------------------------------

// Resolve a working Python 3 interpreter. An explicit `hals.pythonPath` wins;
// otherwise probe the usual command names so the extension works out-of-the-box
// on machines that ship only `python3` (most Linux/macOS) or `py` (Windows).
function resolvePython(config: vscode.WorkspaceConfiguration): string {
    const explicit = (config.get<string>('pythonPath', '') || '').trim();
    if (explicit) {
        return explicit;
    }
    const candidates = process.platform === 'win32'
        ? ['py', 'python3', 'python']
        : ['python3', 'python'];
    for (const cmd of candidates) {
        try {
            execFileSync(cmd, ['--version'], { stdio: 'ignore' });
            return cmd;
        } catch {
            // not on PATH / not runnable — try the next candidate
        }
    }
    // Nothing found; fall back to a sensible default so the error message names it.
    return process.platform === 'win32' ? 'py' : 'python3';
}

function startHalsClient(context: vscode.ExtensionContext): void {
    const config = vscode.workspace.getConfiguration('hals');
    const pythonPath = resolvePython(config);
    let serverPath = config.get<string>('serverPath', '');
    if (!serverPath) {
        serverPath = context.asAbsolutePath(path.join('server', 'hals_lsp_server.py'));
    }

    const serverOptions: ServerOptions = {
        command: pythonPath,
        args: [serverPath],
        transport: TransportKind.stdio,
    };

    const clientOptions: LanguageClientOptions = {
        documentSelector: [
            { scheme: 'file', language: 'hals' },
            { scheme: 'file', language: 'dfg' },
        ],
        initializationOptions: {
            definitionEnableWorkspaceIndex: config.get<boolean>('definition.enableWorkspaceIndex', true),
            definitionSearchSubdirs: config.get<string[]>('definition.searchSubdirs', ['SSSRC', 'APPLSRC', 'INCL80']),
        },
        synchronize: {
            fileEvents: vscode.workspace.createFileSystemWatcher(
                '{**/*.{hal,hals},**/SSSRC/CD*,**/APPLSRC/CG[0-9]*}',
            ),
        },
        outputChannelName: 'HAL/S Language Server',
    };

    halsClient = new LanguageClient(
        'halsLanguageServer',
        'HAL/S Language Server',
        serverOptions,
        clientOptions,
    );
    halsClient.start();

    context.subscriptions.push(
        vscode.commands.registerCommand('hals.restartServer', async () => {
            if (!halsClient) {
                return;
            }
            await halsClient.stop();
            halsClient.start();
            vscode.window.showInformationMessage('HAL/S Language Server restarted');
        }),
    );
}

// ---------------------------------------------------------------------------
// AP-101 assembler language server.
//
// Default: the bundled pure-Python *browse* server (outline, go-to-definition,
// find-references, COPY/macro navigation) — portable, no binary. If
// `ap101asm.serverPath` points at an external binary (e.g. the optional OCaml
// HLASM server with real diagnostics), that is launched instead.
// ---------------------------------------------------------------------------
function startAp101Client(context: vscode.ExtensionContext): void {
    const config = vscode.workspace.getConfiguration('ap101asm');
    const documentSelector = [{ scheme: 'file', language: 'ap101asm' }];

    const configuredBinary = config.get<string>('serverPath', '');
    let serverOptions: ServerOptions;
    let clientOptions: LanguageClientOptions;

    if (configuredBinary && fs.existsSync(configuredBinary)) {
        // Advanced: external assembler LSP binary.
        const args: string[] = [];
        const macroLibs = config.get<string[]>('macroLibraries') ?? [];
        for (const lib of macroLibs) {
            if (fs.existsSync(lib)) {
                args.push('--macro-dir', lib);
            }
        }
        serverOptions = { command: configuredBinary, args };
        clientOptions = { documentSelector };
    } else {
        // Default: bundled Python browse server, launched like the HAL/S one.
        const pythonPath = resolvePython(vscode.workspace.getConfiguration('hals'));
        const browseServer = context.asAbsolutePath(
            path.join('server', 'ap101asm_lsp_server.py'),
        );
        serverOptions = {
            command: pythonPath,
            args: [browseServer],
            transport: TransportKind.stdio,
        };
        clientOptions = {
            documentSelector,
            initializationOptions: {
                definitionEnableWorkspaceIndex: config.get<boolean>(
                    'definition.enableWorkspaceIndex', true),
                definitionSearchSubdirs: config.get<string[]>(
                    'definition.searchSubdirs', ['SSSRC', 'MLIB80', 'APPLSRC', 'INCL80']),
            },
            outputChannelName: 'AP-101 Assembler Language Server',
        };
    }

    ap101Client = new LanguageClient(
        'ap101asm-lsp',
        'AP-101 Assembler Language Server',
        serverOptions,
        clientOptions,
    );
    ap101Client.start().catch(err => console.error('AP-101 LSP failed to start:', err));
    context.subscriptions.push({ dispose: () => void ap101Client?.stop() });
}

export function activate(context: vscode.ExtensionContext): void {
    startHalsClient(context);
    startAp101Client(context);

    new RevisionDecorator(CARD_LANGUAGES).activate(context);

    activateHalsFeatures(context);
    activateConcardFeatures(context);
}

export async function deactivate(): Promise<void> {
    await Promise.all([
        halsClient?.stop(),
        ap101Client?.stop(),
    ]);
}
