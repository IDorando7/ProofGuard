// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract VulnerableToken {
    mapping(address => uint256) public balanceOf;
    uint256 public totalSupply;

    function mint(address account, uint256 amount) public {
        balanceOf[account] += amount;
        totalSupply += amount;
    }
}

