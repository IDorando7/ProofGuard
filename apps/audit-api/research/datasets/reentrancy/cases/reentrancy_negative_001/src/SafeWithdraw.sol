// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract SafeWithdraw {
    mapping(address => uint256) public balances;
    bool private locked;

    modifier nonReentrant() {
        require(!locked, "locked");
        locked = true;
        _;
        locked = false;
    }

    function withdraw() external nonReentrant {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "no balance");

        balances[msg.sender] = 0;
        payable(msg.sender).transfer(amount);
    }
}

