// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract VulnerableVault {
    address public treasury;

    function setTreasury(address newTreasury) external {
        treasury = newTreasury;
    }
}

