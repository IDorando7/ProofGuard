# Reentrancy Agent Prompt Notes

Find candidate reentrancy issues where a function makes an external call before updating internal accounting.

Focus on:
- `withdraw`, `redeem`, and `claim` flows.
- `.call`, `.send`, and `.transfer` before balance, share, credit, deposit, or reward updates.
- Absence of `nonReentrant`.

Return candidate findings only. Do not validate or execute exploits.

