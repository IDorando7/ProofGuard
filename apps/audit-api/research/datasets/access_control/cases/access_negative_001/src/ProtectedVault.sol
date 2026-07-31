// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract ProtectedVault {
    address public owner;
    address public treasury;

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    function setTreasury(address newTreasury) external onlyOwner {
        treasury = newTreasury;
    }
}

