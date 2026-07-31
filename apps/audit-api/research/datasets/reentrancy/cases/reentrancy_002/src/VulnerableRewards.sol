// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract VulnerableRewards {
    mapping(address => uint256) public rewards;

    function claim() public {
        uint256 amount = rewards[msg.sender];
        require(amount > 0, "no rewards");

        payable(msg.sender).transfer(amount);
        rewards[msg.sender] = 0;
    }
}

