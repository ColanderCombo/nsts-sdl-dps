# TatSu EBNF Grammar Syntax Highlighter

VS Code syntax highlighting extension for [TatSu](https://github.com/neogeny/TatSu) EBNF grammar files.

## Features

- Syntax highlighting for TatSu EBNF grammar files (`.ebnf`, `.tatsu`)
- Highlights:
  - **Directives**: `@@grammar`, `@@whitespace`, etc.
  - **Rule definitions**: Rule names and their bodies
  - **Named captures**: `name+:`, `name:`
  - **String literals**: Single and double quoted strings
  - **Regular expressions**: `/regex/` patterns
  - **Operators**: `|` (alternation), `$` (end of input), `*` (current position)
  - **Brackets**: `{ }` (repetition), `[ ]` (optional), `( )` (grouping)
  - **Comments**: `# line comment`

## Installation

### From Source (Development)

1. Copy this folder to your VS Code extensions directory:
   - **macOS**: `~/.vscode/extensions/`
   - **Windows**: `%USERPROFILE%\.vscode\extensions\`
   - **Linux**: `~/.vscode/extensions/`

2. Restart VS Code

### Using Symlink (Development)

```bash
# macOS/Linux
ln -s /path/to/tatsu-ebnf-vscode ~/.vscode/extensions/tatsu-ebnf

# Windows (Run as Administrator)
mklink /D "%USERPROFILE%\.vscode\extensions\tatsu-ebnf" "C:\path\to\tatsu-ebnf-vscode"
```

## Usage

Open any `.ebnf` or `.tatsu` file and syntax highlighting will be applied automatically.

## TatSu Grammar Syntax Reference

```ebnf
# Directives
@@grammar :: mygrammar
@@whitespace :: None

# Rule definition
ruleName = expression ;

# Alternation
rule = option1 | option2 | option3 ;

# Optional (zero or one)
rule = [ optional ] ;

# Repetition (zero or more)
rule = { repeated } ;

# Grouping
rule = ( grouped expression ) ;

# Named captures
rule = name+: expression ;
rule = name: expression ;

# String literals
rule = 'literal' | "another literal" ;

# Regular expressions
rule = /[A-Z][a-z]+/ ;

# End of input
rule = expression $ ;
```

## License

MIT
