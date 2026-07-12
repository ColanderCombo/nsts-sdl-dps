# Third-Party Components

This extension (`nsts-dps-lang`) consolidates several Space Shuttle DPS language
tools.  The HAL/S language-server portion is vendored from a third-party project 
licensed under the Apache License 2.0.

## HAL/S Language Server — Apache License 2.0

- **Copyright:** © Zane Hambly
- **Upstream:** https://github.com/Zaneham/hals-lsp
- **Vendored from commit:** `d8700a5` ("Fix sample code based on compiler validation")
- **Local modifications:** The vendored sources have had several extensions: 
    support for DFG format files, handling of PDS sequence and revision fields.
    It's maintained here rather than as a git submodule.
- **License text:** `LICENSE`
- **Upstream notice:**

  > Copyright 2025 Zane Hambly
  >
  > Licensed under the Apache License, Version 2.0 (the "License");
  > you may not use this file except in compliance with the License.
  > You may obtain a copy of the License at
  >
  >     http://www.apache.org/licenses/LICENSE-2.0
  >
  > Unless required by applicable law or agreed to in writing, software
  > distributed under the License is distributed on an "AS IS" BASIS,
  > WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  > See the License for the specific language governing permissions and
  > limitations under the License.

Files originating from this project:

- `server/hals_lsp_server.py`
- `server/hals_semantic_parser.py`
- `server/profile_lsp.py`
- `syntaxes/hals.tmLanguage.json`
- `language-configuration/hals.json`

## Everything else — Apache License 2.0

All components not licensed under other terms are licences under
the Apache License 2.0

