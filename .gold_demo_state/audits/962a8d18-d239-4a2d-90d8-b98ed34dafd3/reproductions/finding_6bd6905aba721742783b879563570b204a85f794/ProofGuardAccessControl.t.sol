// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "../src/ProofGuardDemoVault.sol";

contract UnprivilegedAdminCaller {
    function setAdmin(ProofGuardDemoVault vault, address newAdmin) external {
        vault.setAdmin(newAdmin);
    }
}

contract ProofGuardAccessControlTest {
    function testUnauthorizedSetAdmin() public {
        ProofGuardDemoVault vault = new ProofGuardDemoVault();
        UnprivilegedAdminCaller caller = new UnprivilegedAdminCaller();
        address unauthorizedAdmin = address(0xBEEF);

        caller.setAdmin(vault, unauthorizedAdmin);

        require(vault.admin() == unauthorizedAdmin, "unauthorized update failed");
    }
}
