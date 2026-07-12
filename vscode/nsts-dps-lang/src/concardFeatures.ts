import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';

// CONCARD-specific navigation helpers: clickable INCLUDE CONCARDS(member) links and
// hovers for INSERT / OVERLAY operands.

// INCLUDE CONCARDS(member) or LIBRARY SYSLIBL1(member,member)
const INCLUDE_PATTERN = /\b(INCLUDE|LIBRARY)\s+(\w+)\(([^)]+)\)/gi;

class ConcardDocumentLinkProvider implements vscode.DocumentLinkProvider {
    provideDocumentLinks(
        document: vscode.TextDocument,
        _token: vscode.CancellationToken,
    ): vscode.ProviderResult<vscode.DocumentLink[]> {
        const links: vscode.DocumentLink[] = [];
        const text = document.getText();
        const documentDir = path.dirname(document.uri.fsPath);

        INCLUDE_PATTERN.lastIndex = 0;
        let match: RegExpExecArray | null;
        while ((match = INCLUDE_PATTERN.exec(text)) !== null) {
            const dataset = match[2];   // CONCARDS, SYSLIBL1, etc.
            const members = match[3];   // member name(s)

            // Only handle CONCARDS references for now.
            if (dataset.toUpperCase() !== 'CONCARDS') {
                continue;
            }

            const parenIndex = match[0].indexOf('(');
            const memberStart = match.index + parenIndex + 1;
            const memberEnd = memberStart + members.length;
            const range = new vscode.Range(
                document.positionAt(memberStart),
                document.positionAt(memberEnd),
            );

            // CONCARDS members are typically in the same directory (CON80), with a couple
            // of sibling-directory fallbacks.
            const candidates = [
                path.join(documentDir, members),
                path.join(documentDir, '..', 'CON80', members),
                path.join(documentDir, '..', 'CONCARDS', members),
            ];
            const target = candidates.find(p => fs.existsSync(p));
            if (target) {
                const link = new vscode.DocumentLink(range, vscode.Uri.file(target));
                link.tooltip = `Open ${members}`;
                links.push(link);
            }
        }

        return links;
    }
}

class ConcardHoverProvider implements vscode.HoverProvider {
    provideHover(
        document: vscode.TextDocument,
        position: vscode.Position,
        _token: vscode.CancellationToken,
    ): vscode.ProviderResult<vscode.Hover> {
        const range = document.getWordRangeAtPosition(position, /[A-Za-z0-9#$@_]+/);
        if (!range) {
            return null;
        }
        const word = document.getText(range);
        const line = document.lineAt(position.line).text;

        if (/^\s+INSERT\s+/i.test(line)) {
            return new vscode.Hover(
                new vscode.MarkdownString(
                    `**Module/CSECT**: \`${word}\`\n\n` +
                    `This control section will be linked into the current overlay.`,
                ),
            );
        }

        if (/\bOVERLAY\s+/i.test(line)) {
            return new vscode.Hover(
                new vscode.MarkdownString(
                    `**Overlay**: \`${word}\`\n\n` +
                    `Memory overlay region. Modules INSERTed after this directive ` +
                    `will be loaded into this overlay.`,
                ),
            );
        }

        return null;
    }
}

export function activateConcardFeatures(context: vscode.ExtensionContext): void {
    context.subscriptions.push(
        vscode.languages.registerDocumentLinkProvider(
            { language: 'concard' },
            new ConcardDocumentLinkProvider(),
        ),
        vscode.languages.registerHoverProvider(
            { language: 'concard' },
            new ConcardHoverProvider(),
        ),
    );
}
