import * as vscode from 'vscode';

// HAL/S-specific editor features that are independent of the language server:
//   * legacy-character decorations (render ASCII placeholders ~ and ` as ¬ and ¢)
//   * member-name display (status bar + Explorer file-decoration tooltip), derived from
//     the first label in an OI member file.
// Revision coloring is handled separately by RevisionDecorator.

const HALS_LANGUAGES = new Set(['hals', 'dfg']);

// ASCII placeholders used in local HAL/S sources for IBM mainframe characters.
const LEGACY_NOT_ASCII = '~';
const LEGACY_CENT_ASCII = '`';

type MemberNameCacheEntry = {
    mtime: number;
    size: number;
    label?: string;
};

function getSingleCharSymbol(value: unknown, fallback: string): string {
    if (typeof value !== 'string') {
        return fallback;
    }
    return value.length > 0 ? value[0] : fallback;
}

function isHalMemberPath(uri: vscode.Uri): boolean {
    if (uri.scheme !== 'file') {
        return false;
    }
    const p = uri.fsPath.replace(/\\/g, '/').toUpperCase();
    return /\/(APPLSRC|SSSRC|INCL80)\//.test(p);
}

function stripCardTrailer(line: string): string {
    return line.replace(/\s+[0-9]{6}[A-Z]{2}\s*$/, '');
}

function extractFirstLabelFromText(text: string): string | undefined {
    const lines = text.split(/\r?\n/);
    for (const raw of lines) {
        if (!raw) {
            continue;
        }
        if (raw[0].toUpperCase() === 'C') {
            continue;
        }
        const content = /^[A-Za-z]/.test(raw) ? raw.slice(1) : raw;
        const trimmed = stripCardTrailer(content);
        const match = /^\s*([A-Z_][A-Z0-9_]*)\s*:/i.exec(trimmed);
        if (match) {
            return match[1].toUpperCase();
        }
    }
    return undefined;
}

class HalsFeatures implements vscode.Disposable {
    private readonly legacyCharDecorationType = vscode.window.createTextEditorDecorationType({
        color: 'transparent',
    });
    private readonly memberNameCache = new Map<string, MemberNameCacheEntry>();
    private readonly memberNameStatusBar = vscode.window.createStatusBarItem(
        vscode.StatusBarAlignment.Right,
        95,
    );
    private readonly fileDecorationEmitter = new vscode.EventEmitter<vscode.Uri | vscode.Uri[]>();

    updateLegacyCharDecorations(editor: vscode.TextEditor): void {
        if (editor.document.languageId !== 'hals') {
            editor.setDecorations(this.legacyCharDecorationType, []);
            return;
        }

        const config = vscode.workspace.getConfiguration('hals');
        if (!config.get<boolean>('legacyCharDecorations.enabled', true)) {
            editor.setDecorations(this.legacyCharDecorationType, []);
            return;
        }

        const notSymbol = getSingleCharSymbol(config.get<string>('legacyCharDecorations.notSymbol'), '¬');
        const centSymbol = getSingleCharSymbol(config.get<string>('legacyCharDecorations.centSymbol'), '¢');
        const decorations: vscode.DecorationOptions[] = [];

        for (let lineNum = 0; lineNum < editor.document.lineCount; lineNum++) {
            const text = editor.document.lineAt(lineNum).text;
            for (let col = 0; col < text.length; col++) {
                const ch = text[col];
                let symbol: string | undefined;
                if (ch === LEGACY_NOT_ASCII) {
                    symbol = notSymbol;
                } else if (ch === LEGACY_CENT_ASCII) {
                    symbol = centSymbol;
                }
                if (!symbol) {
                    continue;
                }
                decorations.push({
                    range: new vscode.Range(lineNum, col, lineNum, col + 1),
                    hoverMessage: `Original source character: ${ch}`,
                    renderOptions: {
                        before: { contentText: symbol, margin: '0 -1ch 0 0' },
                    },
                });
            }
        }

        editor.setDecorations(this.legacyCharDecorationType, decorations);
    }

    private async getMemberLabelForUri(uri: vscode.Uri, openText?: string): Promise<string | undefined> {
        if (!isHalMemberPath(uri)) {
            return undefined;
        }

        if (openText !== undefined) {
            const label = extractFirstLabelFromText(openText);
            this.memberNameCache.set(uri.toString(), { mtime: Date.now(), size: openText.length, label });
            return label;
        }

        try {
            const stat = await vscode.workspace.fs.stat(uri);
            const key = uri.toString();
            const cached = this.memberNameCache.get(key);
            if (cached && cached.mtime === stat.mtime && cached.size === stat.size) {
                return cached.label;
            }
            const bytes = await vscode.workspace.fs.readFile(uri);
            const text = Buffer.from(bytes).toString('utf8');
            const label = extractFirstLabelFromText(text);
            this.memberNameCache.set(key, { mtime: stat.mtime, size: stat.size, label });
            return label;
        } catch {
            return undefined;
        }
    }

    async updateMemberNameStatusBar(editor: vscode.TextEditor | undefined): Promise<void> {
        if (
            !editor ||
            !HALS_LANGUAGES.has(editor.document.languageId) ||
            !isHalMemberPath(editor.document.uri)
        ) {
            this.memberNameStatusBar.hide();
            return;
        }

        const label = await this.getMemberLabelForUri(editor.document.uri, editor.document.getText());
        if (!label) {
            this.memberNameStatusBar.hide();
            return;
        }
        this.memberNameStatusBar.text = label;
        this.memberNameStatusBar.tooltip = label;
        this.memberNameStatusBar.show();
    }

    activate(context: vscode.ExtensionContext): void {
        const fileDecorationProvider: vscode.FileDecorationProvider = {
            onDidChangeFileDecorations: this.fileDecorationEmitter.event,
            provideFileDecoration: async (uri: vscode.Uri) => {
                if (!isHalMemberPath(uri)) {
                    return;
                }
                const label = await this.getMemberLabelForUri(uri);
                if (!label) {
                    return;
                }
                // Tooltip-only decoration (no badge/color) for Explorer metadata.
                return new vscode.FileDecoration(undefined, `\n${label}`);
            },
        };

        if (vscode.window.activeTextEditor) {
            this.updateLegacyCharDecorations(vscode.window.activeTextEditor);
            void this.updateMemberNameStatusBar(vscode.window.activeTextEditor);
        }

        context.subscriptions.push(
            this,
            vscode.window.registerFileDecorationProvider(fileDecorationProvider),
            this.memberNameStatusBar,
            this.fileDecorationEmitter,
            vscode.window.onDidChangeActiveTextEditor(editor => {
                if (editor) {
                    this.updateLegacyCharDecorations(editor);
                }
                void this.updateMemberNameStatusBar(editor);
            }),
            vscode.workspace.onDidChangeTextDocument(event => {
                for (const editor of vscode.window.visibleTextEditors) {
                    if (editor.document === event.document) {
                        this.updateLegacyCharDecorations(editor);
                    }
                }
                if (
                    vscode.window.activeTextEditor &&
                    event.document === vscode.window.activeTextEditor.document
                ) {
                    void this.updateMemberNameStatusBar(vscode.window.activeTextEditor);
                }
            }),
            vscode.workspace.onDidChangeConfiguration(event => {
                if (event.affectsConfiguration('hals.legacyCharDecorations')) {
                    for (const editor of vscode.window.visibleTextEditors) {
                        this.updateLegacyCharDecorations(editor);
                    }
                }
            }),
            vscode.workspace.onDidCloseTextDocument(document => {
                this.memberNameCache.delete(document.uri.toString());
                this.fileDecorationEmitter.fire(document.uri);
                if (vscode.window.activeTextEditor?.document === document) {
                    this.memberNameStatusBar.hide();
                }
            }),
            vscode.workspace.onDidSaveTextDocument(document => {
                this.memberNameCache.delete(document.uri.toString());
                this.fileDecorationEmitter.fire(document.uri);
                if (vscode.window.activeTextEditor?.document === document) {
                    void this.updateMemberNameStatusBar(vscode.window.activeTextEditor);
                }
            }),
            vscode.workspace.onDidRenameFiles(event => {
                for (const f of event.files) {
                    this.memberNameCache.delete(f.oldUri.toString());
                    this.memberNameCache.delete(f.newUri.toString());
                    this.fileDecorationEmitter.fire([f.oldUri, f.newUri]);
                }
            }),
            vscode.workspace.onDidDeleteFiles(event => {
                for (const uri of event.files) {
                    this.memberNameCache.delete(uri.toString());
                }
                this.fileDecorationEmitter.fire([...event.files]);
            }),
        );
    }

    dispose(): void {
        this.legacyCharDecorationType.dispose();
        this.memberNameCache.clear();
    }
}

export function activateHalsFeatures(context: vscode.ExtensionContext): void {
    new HalsFeatures().activate(context);
}
