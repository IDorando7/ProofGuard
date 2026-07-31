// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract VulnerableTreasury {
    address public owner;
    address public treasury;

    constructor(address initialTreasury) {
        owner = msg.sender;
        treasury = initialTreasury;
    }

    function setTreasury(address newTreasury) external {
        treasury = newTreasury;
    }
}

