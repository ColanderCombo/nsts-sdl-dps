import * as vscode from 'vscode';

// "git-blame style" revision coloring for the punched-card source formats
// (HAL/S, DFG, AP-101 assembler, CONCARD, MMU build). Every such card ends with an
// 8-column trailer: a 6-digit sequence number followed by a 2-letter revision code.
// We color each line's trailer by its revision so changes from the same CR stand out.
//

// Palette of foreground colors for revision highlighting
const REVISION_COLORS = [
    '#d19a66',  // Tan-Orange
    '#61afef',  // Light blue
    '#98c379',  // Green
    '#c678dd',  // Purple
    '#e5c07b',  // Yellow-gold
    '#56b6c2',  // Cyan
    '#e06c75',  // Pinkish Red
    '#be5046',  // Rust
    '#7ec699',  // Mint
    '#d4bfff',  // Lavender
    '#ffb86c',  // Peach
    '#8be9fd',  // Sky blue
];

// Sequence number (6 digits) + revision (2 letters) at end of line.
const CARD_TRAILER_PATTERN = /([0-9]{6})([A-Z]{2})\s*$/;

export class RevisionDecorator implements vscode.Disposable {
    // Cache of decoration types, keyed by `${revision}-${colorIndex}`.
    private readonly decorationTypes = new Map<string, vscode.TextEditorDecorationType>();
    // Per-document assignment of revision code -> color index (stable within a file).
    private readonly colorMaps = new Map<string, Map<string, number>>();

    constructor(private readonly languageIds: ReadonlySet<string>) {}

    activate(context: vscode.ExtensionContext): void {
        for (const editor of vscode.window.visibleTextEditors) {
            this.update(editor);
        }

        context.subscriptions.push(
            this,
            vscode.window.onDidChangeActiveTextEditor(editor => {
                if (editor) {
                    this.update(editor);
                }
            }),
            vscode.workspace.onDidChangeTextDocument(event => {
                for (const editor of vscode.window.visibleTextEditors) {
                    if (editor.document === event.document) {
                        this.update(editor);
                    }
                }
            }),
            vscode.workspace.onDidCloseTextDocument(document => {
                this.colorMaps.delete(document.uri.toString());
            }),
        );
    }

    private decorationType(revision: string, colorIndex: number): vscode.TextEditorDecorationType {
        const key = `${revision}-${colorIndex}`;
        let decorationType = this.decorationTypes.get(key);
        if (!decorationType) {
            const color = REVISION_COLORS[colorIndex % REVISION_COLORS.length];
            decorationType = vscode.window.createTextEditorDecorationType({
                color,
                overviewRulerColor: color,
                overviewRulerLane: vscode.OverviewRulerLane.Right,
            });
            this.decorationTypes.set(key, decorationType);
        }
        return decorationType;
    }

    update(editor: vscode.TextEditor): void {
        if (!this.languageIds.has(editor.document.languageId)) {
            return;
        }

        const documentKey = editor.document.uri.toString();
        let colorMap = this.colorMaps.get(documentKey);
        if (!colorMap) {
            colorMap = new Map();
            this.colorMaps.set(documentKey, colorMap);
        }

        const decorationsByRevision = new Map<string, vscode.DecorationOptions[]>();

        for (let lineNum = 0; lineNum < editor.document.lineCount; lineNum++) {
            const text = editor.document.lineAt(lineNum).text;
            // Need at least seq (6) + rev (2) = 8 characters to carry a trailer.
            if (text.length < 8) {
                continue;
            }
            const match = CARD_TRAILER_PATTERN.exec(text);
            if (!match) {
                continue;
            }

            const revision = match[2];
            if (!colorMap.has(revision)) {
                colorMap.set(revision, colorMap.size);
            }

            const range = new vscode.Range(
                new vscode.Position(lineNum, match.index),
                new vscode.Position(lineNum, text.length),
            );
            let list = decorationsByRevision.get(revision);
            if (!list) {
                list = [];
                decorationsByRevision.set(revision, list);
            }
            list.push({ range, hoverMessage: `Revision: ${revision}` });
        }

        for (const [revision, colorIndex] of colorMap) {
            editor.setDecorations(
                this.decorationType(revision, colorIndex),
                decorationsByRevision.get(revision) ?? [], 
            );
        }
    }

    dispose(): void {
        for (const decorationType of this.decorationTypes.values()) {
            decorationType.dispose();
        }
        this.decorationTypes.clear();
        this.colorMaps.clear();
    }
}
