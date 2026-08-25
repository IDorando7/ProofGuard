// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice Deliberately vulnerable synthetic contract for a local ProofGuard demo.
///         Do not deploy this contract with real value.
contract ProofGuardDemoVault {
    mapping(address => uint256) public balances;
    address public admin;

    constructor() {
        admin = msg.sender;
    }

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    // Intentionally missing access control.
    function setAdmin(address newAdmin) external {
        admin = newAdmin;
    }

    // Intentionally updates state after the external call.
    function withdrawAll() external {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "nothing to withdraw");

        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "transfer failed");

        balances[msg.sender] = 0;
    }

    function emergencySweep(address payable receiver) external {
        require(msg.sender == admin, "admin only");
        receiver.transfer(address(this).balance);
    }
}
