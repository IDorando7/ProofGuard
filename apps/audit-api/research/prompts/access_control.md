# Access Control Agent Prompt Notes

Find candidate access control issues where privileged functions are externally callable without authorization.

Focus on:
- Admin setters such as `setTreasury`, `setOracle`, `setOwner`, and configuration updates.
- Minting, pausing, upgrade, treasury, and owner/admin functions.
- Missing `onlyOwner`, `onlyRole`, `hasRole`, or equivalent `msg.sender` authorization.

Return candidate findings only. Do not validate or execute exploits.

